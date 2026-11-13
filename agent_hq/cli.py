"""CLI entry point for Agent HQ."""

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, eventlog
from .actions import handle_fzf_result, jump_to_pane, rename_session
from .cache import get_agents as get_agents_cached
from .discovery import get_all_panes, is_in_tmux
from .display import (
    format_agents_for_fzf,
    format_for_fzf,
    format_json,
    format_status_line,
    format_tree,
    get_terminal_width,
    print_no_agents_message,
    run_fzf,
)
from .enrichment import compute_content_fingerprint
from .grouping import filter_agents, group_by_worktree, sort_agents_by_priority
from .runtime_dir import ensure_dir
from .state import (
    cleanup_inactive_entries,
    cleanup_stale_entries,
    load_access_times,
    load_inactive_state,
    toggle_inactive,
)


def _prune_mutes_of_closed_panes() -> None:
    """Drop mutes only for panes that no longer exist (#303).

    Pruned against every live tmux pane, as the daemon does, never against
    the agents discovered this tick: an agent restarting in its pane, or a
    missed discovery, must not silently unmute it. An empty pane set is
    treated as a tmux hiccup and skipped.
    """
    live_pane_ids = {p["pane_id"] for p in get_all_panes()}
    if live_pane_ids:
        cleanup_inactive_entries(live_pane_ids)


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="agent-hq",
        description="Monitor and manage AI coding agents running in tmux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  agent-hq                         # interactive fzf picker (default)\n"
            "  agent-hq --list                  # plain list of agents\n"
            "  agent-hq --dashboard             # persistent Textual TUI\n"
            "  agent-hq --watch 2               # refresh every 2 seconds\n"
            "  agent-hq --agent claude          # only Claude Code agents\n"
            "  agent-hq --status-line           # one-liner for tmux status bar\n"
            "  agent-hq daemon start            # start background discovery daemon\n"
            "\n"
            "Run `agent-hq <command> -h` for command-specific help."
        ),
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    output_group = parser.add_argument_group("Output format")
    output_group.add_argument(
        "--list",
        action="store_true",
        help="List agents without fzf (plain text)",
    )
    output_group.add_argument(
        "--tree",
        action="store_true",
        help="Show agents in tree format",
    )
    output_group.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON for scripting",
    )
    output_group.add_argument(
        "--status-line",
        action="store_true",
        help="One-liner summary for tmux status bar",
    )

    filter_group = parser.add_argument_group("Filters")
    filter_group.add_argument(
        "--agent",
        metavar="TYPE",
        help="Filter by agent type (claude, codex, aider, etc.)",
    )
    filter_group.add_argument(
        "--repo",
        metavar="NAME",
        help="Filter by repository name (partial match)",
    )

    mode_group = parser.add_argument_group("Modes")
    mode_group.add_argument(
        "--watch",
        type=int,
        metavar="N",
        help="Refresh every N seconds",
    )
    mode_group.add_argument(
        "--jump",
        metavar="PANE",
        help="Jump directly to pane (skip fzf)",
    )
    mode_group.add_argument(
        "--dashboard",
        action="store_true",
        help="Launch persistent TUI dashboard (requires textual)",
    )

    internal_group = parser.add_argument_group("Internal (used by fzf bindings)")
    internal_group.add_argument(
        "--width",
        type=int,
        metavar="COLS",
        help="Override terminal width for formatting (used by fzf reload)",
    )
    internal_group.add_argument(
        "--toggle-inactive",
        metavar="PANE",
        help="Toggle inactive state for a pane (used by fzf Tab binding)",
    )
    internal_group.add_argument(
        "--rename-session",
        metavar="PANE",
        help="Rename tmux session for a pane (used by fzf F2 binding)",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="subcommand", metavar="<command>")

    codex_parser = subparsers.add_parser(
        "codex", help="Launch Codex with authoritative live status",
        description="Run the ordinary Codex TUI with a private per-pane app server. "
                    "Pass native Codex arguments after 'agent-hq codex'.",
    )
    codex_parser.add_argument("arguments", nargs=argparse.REMAINDER)

    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Manage background discovery daemon",
        description="Start, stop, or check the background discovery daemon.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The daemon polls tmux panes on an interval and populates the\n"
            "agent cache so fzf and dashboard queries return instantly.\n"
            "\n"
            "Examples:\n"
            "  agent-hq daemon start                # start with default 2s interval\n"
            "  agent-hq daemon start --interval 1   # poll every second\n"
            "  agent-hq daemon status               # is it running? where?\n"
            "  agent-hq daemon stop                 # stop the daemon"
        ),
    )
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_action", metavar="<action>")

    start_parser = daemon_sub.add_parser(
        "start",
        help="Start background daemon",
        description=(
            "Spawn the background discovery daemon. Agent state is written "
            "to the shared cache so other commands can read it without "
            "touching tmux directly."
        ),
    )
    start_parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="Polling interval in seconds (default: 2)",
    )

    daemon_sub.add_parser(
        "stop",
        help="Stop background daemon",
        description="Stop the running background discovery daemon, if any.",
    )
    daemon_sub.add_parser(
        "status",
        help="Show daemon status",
        description="Report whether the background discovery daemon is running, and its PID.",
    )

    web_parser = subparsers.add_parser(
        "web",
        help="Launch the mobile-first web dashboard",
        description=(
            "Start the mobile-first kanban dashboard. Auto-detects the "
            "Tailscale IP for phone access and prints a QR code on startup; "
            "scan from your phone to log in without typing the token. "
            "Auto-spawns the discovery daemon unless --no-daemon is given."
        ),
    )
    web_parser.add_argument(
        "--bind",
        default=None,
        metavar="ADDR",
        help=(
            "Bind address. Defaults to the Tailscale IPv4 if Tailscale is "
            "installed, otherwise 127.0.0.1. Pass an explicit IP or "
            "0.0.0.0 to override."
        ),
    )
    web_parser.add_argument(
        "--port",
        type=int,
        default=8765,
        metavar="PORT",
        help="Port to bind (default: 8765)",
    )
    web_parser.add_argument(
        "--token-file",
        type=Path,
        default=Path.home() / ".config" / "agent-hq" / "web-token",
        help="Path to the shared-secret token file (default: ~/.config/agent-hq/web-token)",
    )
    web_parser.add_argument(
        "--no-daemon",
        action="store_true",
        help=(
            "Don't auto-start the discovery daemon. If no daemon is "
            "running, the dashboard exits with a hint to launch one."
        ),
    )
    web_parser.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG-level logging",
    )
    web_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the token/QR banner (used by the launchd service)",
    )
    # `agent-hq web service <action>` — manage the persistent LaunchAgent.
    # Not required, so bare `agent-hq web` still falls through to run_web.
    web_sub = web_parser.add_subparsers(dest="web_action", metavar="<command>")
    service_parser = web_sub.add_parser(
        "service",
        help="Manage the persistent launchd LaunchAgent (macOS)",
        description=(
            "Install, remove, or inspect the launchd LaunchAgent that keeps "
            "`agent-hq web` running in your login session. It restarts on "
            "crash and at login so the dashboard stays reachable from your "
            "phone over Tailscale without leaving a terminal open. macOS-only."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  agent-hq web service install     # write plist + load it\n"
            "  agent-hq web service status      # loaded? running? logs + URL\n"
            "  agent-hq web service restart     # force-restart (kickstart -k)\n"
            "  agent-hq web service uninstall   # unload, remove, stop daemon"
        ),
    )
    service_sub = service_parser.add_subparsers(dest="service_action", metavar="<action>")

    svc_install = service_sub.add_parser(
        "install",
        help="Write the LaunchAgent plist and load it",
        description=(
            "Generate the plist, write it to ~/Library/LaunchAgents, and "
            "bootstrap it into your gui/$UID domain. Idempotent — re-running "
            "updates and reloads. Requires `agent-hq` on PATH "
            "(run `./run install-global` first)."
        ),
    )
    # default=SUPPRESS so these don't clobber a value passed on the parent
    # `web` parser before the subcommand (e.g. `web --bind X service install`).
    # The parent's defaults (None / 8765 / token path) always populate the
    # namespace, so the dispatch can read args.bind/port/token_file safely.
    svc_install.add_argument(
        "--bind",
        default=argparse.SUPPRESS,
        metavar="ADDR",
        help=(
            "Bind address baked into the plist. Defaults to the detected "
            "Tailscale IPv4 (install fails if Tailscale isn't up — pass an "
            "explicit address like 0.0.0.0 to override)."
        ),
    )
    svc_install.add_argument(
        "--port",
        type=int,
        default=argparse.SUPPRESS,
        metavar="PORT",
        help="Port to bind (default: 8765)",
    )
    svc_install.add_argument(
        "--token-file",
        type=Path,
        default=argparse.SUPPRESS,
        help="Token file path baked into the plist",
    )

    service_sub.add_parser(
        "uninstall",
        help="Unload the job, remove the plist, and stop the daemon",
        description="bootout the job, delete the plist, and stop the discovery daemon.",
    )
    service_sub.add_parser(
        "status",
        help="Show loaded/running state, log paths, and the dashboard URL",
        description="Report whether the LaunchAgent is loaded and running, plus its logs and URL.",
    )
    service_sub.add_parser(
        "restart",
        help="Force-restart the job (launchctl kickstart -k)",
        description="Restart the running LaunchAgent via launchctl kickstart -k.",
    )

    from .remote.cli import add_remote_parser

    add_remote_parser(subparsers)

    idea_parser = subparsers.add_parser(
        "idea",
        help="File a free-text idea via the running web app",
        description=(
            "Post a free-text idea to the running `agent-hq web` instance. "
            "The classifier picks the right repo+project and files a GitHub "
            "issue. Provide the text as an argument, on stdin, or leave it "
            "blank to open $EDITOR."
        ),
    )
    idea_parser.add_argument(
        "text",
        nargs="?",
        default=None,
        help="The idea text (optional; falls back to stdin then $EDITOR)",
    )
    idea_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host of the running agent-hq web app (default: 127.0.0.1)",
    )
    idea_parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port of the running agent-hq web app (default: 8765)",
    )
    idea_parser.add_argument(
        "--token-file",
        type=Path,
        default=Path.home() / ".config" / "agent-hq" / "web-token",
        help="Path to the bearer token file (default: ~/.config/agent-hq/web-token)",
    )

    recover_parser = subparsers.add_parser(
        "recover",
        help="List agents missing since the last snapshot, with resume commands",
        description=(
            "Diff the last durable agent snapshot (written by the daemon to "
            "~/.config/agent-hq/agent-snapshots.jsonl) against the agents that "
            "are live right now, and print each missing agent with a "
            "ready-to-run `claude --resume <id>` in its working directory. Use "
            "after a crash, reboot, or tmux restart killed agents."
        ),
    )
    recover_parser.add_argument(
        "--json",
        action="store_true",
        # Shares the top-level --json dest; SUPPRESS so it doesn't reset a
        # top-level `--json recover` back to False when absent. #125
        default=argparse.SUPPRESS,
        help="Emit the missing-agent list as JSON instead of a human report",
    )
    recover_baseline_group = recover_parser.add_mutually_exclusive_group()
    recover_baseline_group.add_argument(
        "--since",
        metavar="TIME",
        help=(
            "Baseline at the newest snapshot at or before TIME — a duration "
            "like '45m', '2h', '1d' or an ISO datetime like '2026-06-07 14:30' "
            "— instead of auto-detecting the pre-crash roster"
        ),
    )
    recover_baseline_group.add_argument(
        "--baseline",
        dest="baseline_back",
        type=int,
        metavar="N",
        help="Baseline at the snapshot N records back from the latest (0 = latest)",
    )

    gh_stats_parser = subparsers.add_parser(
        "gh-stats",
        help="Report GitHub API calls agent-hq made, and remaining rate-limit headroom",
        description=(
            "Summarize the gh calls agent-hq recorded over a trailing window, "
            "broken down by rate-limit bucket and call kind, with estimated "
            "GraphQL points from the measured per-call costs, plus the headroom "
            "left in each bucket. Use to measure call volume before and after a "
            "change, or to check whether a process is refreshing more than expected."
        ),
    )
    gh_stats_parser.add_argument(
        "--hours",
        type=float,
        default=1.0,
        metavar="N",
        help="Window to summarize, in hours (default: 1)",
    )
    gh_stats_parser.add_argument(
        "--json",
        action="store_true",
        # Shares the top-level --json dest; SUPPRESS so it doesn't reset a
        # top-level `--json gh-stats` back to False when absent (cf. recover).
        default=argparse.SUPPRESS,
        help="Emit the summary as JSON",
    )

    hooks_parser = subparsers.add_parser(
        "hooks",
        help="Manage agent-hq hook integration for agent harnesses",
    )
    hooks_sub = hooks_parser.add_subparsers(dest="hooks_action", metavar="<action>")
    install_codex_parser = hooks_sub.add_parser(
        "install-codex",
        help="Install the agent-hq hook into ~/.codex/hooks.json (re-trust via /hooks)",
        description=(
            "Merge agent-hq hook entries into ~/.codex/hooks.json (backing up any "
            "existing file). Codex trusts hooks by content hash, so after installing "
            "you must open codex and re-trust via /hooks — until then the hooks "
            "silently do not run."
        ),
    )
    install_codex_parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print the merged hooks.json without writing",
    )
    install_codex_parser.add_argument(
        "--hooks-file",
        type=Path,
        default=None,
        help="Override the hooks.json path (for testing)",
    )

    triage_parser = subparsers.add_parser("triage", help="AI triage worker (#248)")
    triage_sub = triage_parser.add_subparsers(dest="triage_action", metavar="<action>")
    triage_sub.required = True
    triage_run = triage_sub.add_parser(
        "run",
        help="Propose value/effort/fitness for untriaged issues via headless claude -p",
        description=(
            "Batch untriaged (and body-changed proposed) issues per repo into one "
            "claude -p call each and write proposed triage records. Confirm them in "
            "the web Triage inbox."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  agent-hq triage run --dry-run\n"
            "  agent-hq triage run --repo ben-ralston/agent-hq --cap 100"
        ),
    )
    triage_run.add_argument("--dry-run", action="store_true", help="Print proposals, write nothing")
    triage_run.add_argument("--repo", default=None, metavar="OWNER/NAME", help="Only this repo")
    triage_run.add_argument(
        "--cap",
        type=int,
        default=None,
        metavar="N",
        help="Max issues per repo this run (default: triage.ai_batch_cap)",
    )

    hygiene_parser = subparsers.add_parser("hygiene", help="Backlog hygiene sweep (#249)")
    hygiene_sub = hygiene_parser.add_subparsers(dest="hygiene_action", metavar="<action>")
    hygiene_sub.required = True
    hygiene_run = hygiene_sub.add_parser(
        "run",
        help="Find duplicates, stale issues, sentinel clusters and value mismatches",
        description=(
            "Stale detection in code plus one cross-repo claude -p call; findings land in "
            "the data repo's hygiene/findings.json and are reviewed in the web Hygiene mode."
        ),
        epilog="Example:\n  agent-hq hygiene run --dry-run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    hygiene_run.add_argument("--dry-run", action="store_true", help="Print findings, write nothing")

    autopilot_parser = subparsers.add_parser("autopilot", help="Nightly autopilot (#302)")
    autopilot_sub = autopilot_parser.add_subparsers(dest="autopilot_action", metavar="<action>")
    autopilot_sub.required = True
    autopilot_run = autopilot_sub.add_parser(
        "run",
        help="Dispatch opted-in, confirmed issues to goal-mode agents (stops at open PRs)",
        description=(
            "Pick issues flagged autopilot with a CONFIRMED triage record, in rank order, "
            "throttled by autopilot.per_night / max_open, and launch each via launch.sh "
            "--auto with the goal-mode dispatch condition. Never merges. Exit 1 on any "
            "failed or uncertain launch."
        ),
        epilog="Example:\n  agent-hq autopilot run --dry-run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    autopilot_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selection and goal conditions, write nothing",
    )

    return parser


def detect_tailnet_ip() -> str:
    """Return the Tailscale IPv4 if Tailscale is installed, else ``127.0.0.1``."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "127.0.0.1"
    first = result.stdout.strip().splitlines()[0] if result.stdout else ""
    return first or "127.0.0.1"


def run_web(
    *,
    bind: str,
    port: int,
    token_file: Path,
    no_daemon: bool,
    verbose: bool,
    quiet: bool = False,
) -> int:
    """Print QR + token, ensure daemon, then run uvicorn single-process.

    ``quiet`` suppresses the token/QR banner — used by the launchd service so
    the secret never lands in the persistent stdout log.
    """
    import uvicorn

    from .daemon import ensure_running, is_daemon_running
    from .web.app import create_app
    from .web.auth import TokenManager

    if no_daemon:
        if not is_daemon_running():
            print(
                "No daemon is running and --no-daemon was given. Start it with:\n"
                "  agent-hq daemon start",
                file=sys.stderr,
            )
            return 1
    else:
        if not ensure_running():
            print("Failed to start daemon. Check the logs.", file=sys.stderr)
            return 1

    tm = TokenManager(token_file)
    tm.ensure()
    token = token_file.read_text().strip()

    url = f"http://{bind}:{port}/"
    print(f"Agent HQ web dashboard: {url}")
    if not quiet:
        import qrcode

        print(f"Token: {token}")
        print(f"Token file: {token_file}")
        print()
        print("Scan to sign in from your phone:")
        qr = qrcode.QRCode(border=1)
        qr.add_data(f"{url}login?token={token}")
        qr.print_ascii(invert=True)
        print()

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    eventlog.set_process_role("web")
    eventlog.emit("info", "web service started", {"bind": bind, "port": port})

    app = create_app(token_file=token_file)
    uvicorn.run(
        app,
        host=bind,
        port=port,
        workers=1,
        reload=False,
        log_level="debug" if verbose else "info",
    )
    return 0


def get_agents(agent_filter: str | None = None, repo_filter: str | None = None):
    """Discover and enrich all agents, with optional filtering."""
    agents, _ = get_agents_cached()
    return filter_agents(agents, agent_type=agent_filter, repo=repo_filter)


def main() -> int:
    """Main entry point."""
    # Native Codex options belong to Codex, including flags argparse doesn't know.
    if sys.argv[1:2] == ["codex"] and sys.argv[2:] not in (["--help"], ["-h"]):
        from .codex_runtime import main as codex_main

        return codex_main(sys.argv[2:])
    if os.environ.get("AGENT_HQ_DEBUG"):
        log_file = Path("/tmp/agent-hq/debug.log")
        ensure_dir(log_file.parent)
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            filename=str(log_file),
        )

    parser = create_parser()
    args = parser.parse_args()

    # Handle daemon subcommand
    if getattr(args, "subcommand", None) == "daemon":
        from .daemon import daemon_status, start_daemon, stop_daemon

        if args.daemon_action == "start":
            return start_daemon(interval=args.interval)
        elif args.daemon_action == "stop":
            return stop_daemon()
        elif args.daemon_action == "status":
            return daemon_status()
        else:
            parser.parse_args(["daemon", "--help"])
            return 1

    if getattr(args, "subcommand", None) == "remote":
        from .remote.cli import run_remote

        return run_remote(args)

    # Handle web subcommand
    if getattr(args, "subcommand", None) == "web":
        if getattr(args, "web_action", None) == "service":
            from . import service

            try:
                if args.service_action == "install":
                    return service.install_service(
                        bind=args.bind,
                        port=args.port,
                        token_file=args.token_file,
                    )
                elif args.service_action == "uninstall":
                    return service.uninstall_service()
                elif args.service_action == "status":
                    return service.service_status()
                elif args.service_action == "restart":
                    return service.restart_service()
                else:
                    parser.parse_args(["web", "service", "--help"])
                    return 1
            except service.ServiceError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                eventlog.emit(
                    "error",
                    "launchctl service operation failed",
                    {"command": args.service_action, "error": str(exc)},
                )
                return 1

        bind = args.bind or detect_tailnet_ip()
        return run_web(
            bind=bind,
            port=args.port,
            token_file=args.token_file,
            no_daemon=args.no_daemon,
            verbose=args.verbose,
            quiet=args.quiet,
        )

    # Handle idea subcommand
    if getattr(args, "subcommand", None) == "idea":
        from .idea.cli import run_idea

        return run_idea(
            text=args.text,
            host=args.host,
            port=args.port,
            token_file=args.token_file,
        )

    # Handle recover subcommand (must run post-crash, possibly outside tmux)
    if getattr(args, "subcommand", None) == "recover":
        from .recover import run_recover

        return run_recover(as_json=args.json, since=args.since, baseline_back=args.baseline_back)

    # Handle gh-stats subcommand (diagnostics — must work outside tmux)
    if getattr(args, "subcommand", None) == "gh-stats":
        from .gh_stats import run_gh_stats

        return run_gh_stats(hours=args.hours, as_json=getattr(args, "json", False))

    # Handle hooks subcommand (setup task — must work outside tmux)
    if getattr(args, "subcommand", None) == "hooks":
        if getattr(args, "hooks_action", None) == "install-codex":
            from .codex_hooks import install_codex_hooks

            return install_codex_hooks(args.hooks_file, print_only=args.print_only)
        parser.parse_args(["hooks", "--help"])
        return 1

    # Handle triage subcommand (headless worker — must work outside tmux)
    if getattr(args, "subcommand", None) == "triage":
        if getattr(args, "triage_action", None) == "run":
            from .triage_cli import run_triage_cli

            return run_triage_cli(dry_run=args.dry_run, repo=args.repo, cap=args.cap)
        # argparse already rejects a missing/unknown triage_action (required subparsers)
        return 1

    # Handle hygiene subcommand (headless sweep — must work outside tmux)
    if getattr(args, "subcommand", None) == "hygiene":
        if getattr(args, "hygiene_action", None) == "run":
            from .hygiene_cli import run_hygiene_cli

            return run_hygiene_cli(dry_run=args.dry_run)
        return 1

    # Handle autopilot subcommand (nightly launchd job — must work outside tmux)
    if getattr(args, "subcommand", None) == "autopilot":
        if getattr(args, "autopilot_action", None) == "run":
            from .autopilot_cli import run_autopilot_cli

            return run_autopilot_cli(dry_run=args.dry_run)
        return 1

    # Check if we're in tmux (unless using --status-line which can work outside)
    if not is_in_tmux() and not args.status_line:
        print("Error: agent-hq must be run from within tmux", file=sys.stderr)
        print("", file=sys.stderr)
        print("Start a tmux session first:", file=sys.stderr)
        print("  tmux new-session", file=sys.stderr)
        return 1

    # Handle --dashboard mode
    if args.dashboard:
        from .dashboard import run_dashboard

        eventlog.set_process_role("tui")
        run_dashboard(agent_filter=args.agent, repo_filter=args.repo)
        return 0

    # Handle --jump directly
    if args.jump:
        success = jump_to_pane(args.jump)
        return 0 if success else 1

    # Handle --toggle-inactive (used by fzf Tab binding)
    if args.toggle_inactive:
        pane_id = args.toggle_inactive
        fingerprint = compute_content_fingerprint(pane_id)
        toggle_inactive(pane_id, fingerprint)
        return 0

    # Handle --rename-session (used by fzf F2 binding)
    if args.rename_session:
        success = rename_session(args.rename_session)
        return 0 if success else 1

    # Handle --watch mode
    if args.watch:
        try:
            while True:
                # Clear screen
                print("\033[2J\033[H", end="")

                agents = get_agents(args.agent, args.repo)
                if not agents:
                    print_no_agents_message()
                else:
                    groups = group_by_worktree(agents)
                    if args.tree:
                        print(format_tree(groups))
                    else:
                        print(format_for_fzf(groups))

                print(f"\n[Refreshing every {args.watch}s. Press Ctrl-C to exit]")
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0

    # Get agents
    agents = get_agents(args.agent, args.repo)

    # Handle --status-line (works even with no agents)
    if args.status_line:
        groups = group_by_worktree(agents)
        print(format_status_line(groups))
        return 0

    # Handle case of no agents found
    if not agents:
        print_no_agents_message()
        return 0

    groups = group_by_worktree(agents)

    # Handle output modes
    if args.json:
        print(format_json(agents))
        return 0

    if args.tree:
        print(format_tree(groups))
        return 0

    if args.list:
        # Mute is sticky (#303): only an explicit unmute clears it.
        inactive_records = load_inactive_state()
        _prune_mutes_of_closed_panes()

        inactive_pane_ids = set(inactive_records.keys())

        # Sort by priority for --list mode (used by fzf reload)
        access_times = load_access_times()
        sorted_agents = sort_agents_by_priority(agents, access_times, inactive_pane_ids)
        # Use explicit width if provided (fzf reload passes this), otherwise auto-detect
        content = format_agents_for_fzf(
            sorted_agents, width=args.width, inactive_pane_ids=inactive_pane_ids
        )
        print(content)
        return 0

    # Default: interactive fzf mode with priority sorting

    # Mute is sticky (#303): only an explicit unmute clears it.
    inactive_records = load_inactive_state()
    valid_pane_ids = {a.pane_id for a in agents}

    inactive_pane_ids = set(inactive_records.keys())

    # Sort with inactive awareness
    access_times = load_access_times()
    sorted_agents = sort_agents_by_priority(agents, access_times, inactive_pane_ids)

    # Clean up stale entries from access times and inactive files
    cleanup_stale_entries(valid_pane_ids)
    _prune_mutes_of_closed_panes()

    # Calculate width for initial load based on preview mode
    # Wide (≥80): side preview at 50%, so list gets ~45%
    # Narrower: bottom/no preview, list gets full width
    term_width = get_terminal_width()
    if term_width >= 80:  # WIDTH_NORMAL - has side preview
        fzf_list_width = int(term_width * 0.45)
    else:
        fzf_list_width = term_width
    content = format_agents_for_fzf(
        sorted_agents, width=fzf_list_width, inactive_pane_ids=inactive_pane_ids
    )
    result = run_fzf(content)

    if result:
        success = handle_fzf_result(result)
        return 0 if success else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
