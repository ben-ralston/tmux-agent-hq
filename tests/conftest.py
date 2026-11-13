"""Suite-wide test isolation (#133).

The live ``agent-hq web`` launchd service (and the daemon it spawns)
rewrites ``/tmp/agent-hq/cache.json`` every ~2 s and writes per-pane
hook state under ``/tmp/agent-hq/states/``. Tests that reach those
paths — directly or through a deep call chain like
``Watcher.refresh_pane → resolve_phase_snapshot → read_hook_revision``
— race against live machine state: green on a quiet machine, red when
the daemon's content collides with a test's fixtures. The same applies
to user-owned ``$HOME`` state (``~/.config/agent-hq/``,
``~/.cache/agent-hq/``, ``~/.claude/``): tests must neither depend on
it nor clobber it.

This autouse fixture redirects every module-level real-path constant
into the test's ``tmp_path``, mirroring the ``_DEFAULT_SNAPSHOTS_FILE``
pattern from #125. Individual tests may still monkeypatch the same
constants (their patch applies on top), and constructor/parameter
injection (``SnapshotStore(path=...)``, ``create_app(github_cache=...)``,
``Watcher(cache_file=...)``) remains the preferred wiring for new code.

Path redirection is not enough on its own: tmux is an *external
service*, so a helper that shells out to ``tmux new-window`` reaches the
user's real sessions no matter where the isolated paths point. #212 was
exactly that — an unmocked ``POST /personal/dispatch`` spawned a real
Claude session in the user's ``personal`` tmux session on every
full-suite run. A second autouse fixture therefore blocks mutating tmux
subcommands at the ``subprocess`` boundary. It also blocks the real
``pgrep`` (#333): the daemon's orphan cleanup SIGTERMs whatever daemon
pgrep finds, and with ``PID_FILE`` redirected that was the user's live one.
A third fixture guards ``os.kill`` itself, so no test signals an agent-hq
process it did not spawn, whatever found the PID.

``tests/test_isolation.py`` pins both contracts — add new real-path
constants (and new mutating tmux subcommands) both there and here.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# tmux subcommands that change live server state. Read-only ones
# (``list-*``, ``display-message``, ``capture-pane``, ``has-session``,
# ``show-*``) are deliberately absent — tests may still call those. Every
# mutating subcommand the app actually shells out to
# (``grep -rhoE '"tmux", "[a-z-]+"' agent_hq/``) MUST be here. ``paste-buffer``
# and ``load-buffer`` backed the personal-dispatch seed path (removed in #345),
# which wrote into a live pane with no ``send-keys`` at all; they stay blocked.
MUTATING_TMUX_SUBCOMMANDS = frozenset(
    {
        "new-session",
        "new-window",
        "split-window",
        "send-keys",
        "paste-buffer",
        "load-buffer",
        "set-buffer",
        "resize-pane",
        "kill-server",
        "kill-session",
        "kill-window",
        "kill-pane",
        "respawn-pane",
        "respawn-window",
        "rename-session",
        "rename-window",
        "select-window",
        "select-pane",
        "switch-client",
        "swap-pane",
        "swap-window",
        "move-window",
        "set-option",
        "set-window-option",
        # Runs an arbitrary command under the live server. No app code runs
        # it since the companion's handoff went (#399); kept blocked like
        # paste-buffer was after #345.
        "run-shell",
    }
)

# Global tmux options (before the subcommand) that consume the NEXT token as
# their value — e.g. ``tmux -L sock new-session``. Without skipping the value,
# the option-scan loop would treat ``sock`` as the subcommand and wave the call
# through (false negative), which a custom-socket test rig would hit.
_TMUX_GLOBAL_OPTS_WITH_VALUE = frozenset({"-L", "-S", "-f", "-c"})


def _blocked_tmux_subcommand(args: object) -> str | None:
    """Return the mutating subcommand if ``args`` is such a tmux call.

    Only inspects list/tuple argv (the form every caller in this repo
    uses). A ``shell=True`` string / ``os.system`` is left alone rather
    than guessed at — no caller shells tmux that way.
    """
    if not isinstance(args, (list, tuple)) or len(args) < 2:
        return None
    program = str(args[0])
    if program != "tmux" and not program.endswith("/tmux"):
        return None
    skip_next = False
    for token in args[1:]:
        token = str(token)
        if skip_next:
            # This token is a global option's value, not the subcommand.
            skip_next = False
            continue
        if token.startswith("-"):
            skip_next = token in _TMUX_GLOBAL_OPTS_WITH_VALUE
            continue
        return token if token in MUTATING_TMUX_SUBCOMMANDS else None
    return None


def _is_headless_claude(args: object) -> bool:
    """True for a ``claude -p …`` / ``claude --print …`` argv (#248): the
    triage worker's boundary is injected in tests; a real spawn is a mocking gap."""
    if not isinstance(args, (list, tuple)) or not args:
        return False
    program = str(args[0])
    if program != "claude" and not program.endswith("/claude"):
        return False
    return any(str(t) in ("-p", "--print") for t in args[1:])


def _is_worktree_launcher(args: object) -> bool:
    """True for the real worktree launcher (#302 autopilot) — it creates
    worktrees + tmux sessions running Claude; tests inject a fake Launcher.

    The read-only ``launch.sh --capabilities`` probe (capabilities.py) is
    exempt: it launches nothing, and conftest already redirects its path to a
    nonexistent tmp file, so it degrades to CLAUDE_ONLY on its own."""
    if not isinstance(args, (list, tuple)) or not args:
        return False
    if [str(a) for a in args[1:]] == ["--capabilities"]:
        return False
    return str(args[0]).endswith("skills/worktree/scripts/launch.sh")


def _is_gh_call(args: object) -> bool:
    """True for an argv that would invoke the real `gh` binary.

    gh_budget's probe alone takes _SAMPLES reads with a 10s timeout each, so
    one unmocked call is a ~50s hang against the live GitHub API — and it
    spends the user's real rate-limit budget, which is the very thing #255
    exists to protect.
    """
    if not isinstance(args, (list, tuple)) or not args:
        return False
    program = str(args[0])
    return program == "gh" or program.endswith("/gh")


def _is_pgrep_call(args: object) -> bool:
    """True for an argv that would invoke the real `pgrep` (#333).

    ``start_daemon`` SIGTERMs every agent-hq daemon pgrep finds other than
    the PID-file owner, and conftest redirects ``PID_FILE`` into tmp — so a
    real pgrep under test reports the user's live daemon as an orphan.
    """
    if not isinstance(args, (list, tuple)) or not args:
        return False
    program = str(args[0])
    return program == "pgrep" or program.endswith("/pgrep")


# launchctl subcommands that change the user's live LaunchAgents (#396: the
# remote kill switch and service install). Read-only ``print`` passes.
MUTATING_LAUNCHCTL_SUBCOMMANDS = frozenset(
    {"bootout", "bootstrap", "kickstart", "load", "unload", "remove", "enable", "disable"}
)


def _blocked_process_control(args: object) -> str | None:
    """A live ``pkill`` or mutating ``launchctl`` argv, as a short label (#396)."""
    if not isinstance(args, (list, tuple)) or not args:
        return None
    program = str(args[0]).rsplit("/", 1)[-1]
    if program in ("pkill", "killall"):
        return program
    if program == "launchctl" and len(args) > 1 and str(args[1]) in MUTATING_LAUNCHCTL_SUBCOMMANDS:
        return f"launchctl {args[1]}"
    return None


@pytest.fixture(autouse=True)
def _block_live_tmux_mutations(monkeypatch):
    """Fail loudly when a test would mutate the user's real tmux server.

    Tests that legitimately exercise dispatch must patch the dispatcher
    (or ``subprocess.run``) themselves; reaching the real tmux binary is
    always a mocking gap, never intent.
    """
    real_run = subprocess.run
    real_popen_init = subprocess.Popen.__init__

    def guarded_run(args, *a, **kw):
        blocked_control = _blocked_process_control(args)
        if blocked_control is not None:
            raise RuntimeError(
                f"Test tried to run the real `{blocked_control}` (argv: {list(args)!r}). "
                "It would stop or reload the user's live LaunchAgents/processes (#396) "
                "- patch the runner in this test."
            )
        if _is_worktree_launcher(args):
            raise RuntimeError(
                f"Test tried to run the real launch.sh (argv: {list(args)!r}). "
                "Inject a fake Launcher into autopilot.run_launcher (#302)."
            )
        if isinstance(args, (list, tuple)) and list(args[:2]) == ["claude", "agents"]:
            raise FileNotFoundError("Tests must inject the native Claude status reader")
        if _is_headless_claude(args):
            raise RuntimeError(
                f"Test tried to spawn `claude -p` (argv: {list(args)!r}). Inject a fake "
                "runner into triage_worker.run_triage / invoke_claude instead (#248)."
            )
        blocked = _blocked_tmux_subcommand(args)
        if blocked is not None:
            raise RuntimeError(
                f"Test tried to run `tmux {blocked}` against the live tmux "
                f"server (argv: {list(args)!r}). This escapes test isolation "
                "and mutates the user's real sessions (#212) — patch the "
                "dispatcher or subprocess.run in this test."
            )
        if _is_gh_call(args):
            raise FileNotFoundError(
                f"Test tried to run the real `gh` (argv: {list(args)!r}). "
                "gh is an external service like tmux, so path redirection "
                "cannot contain it — patch subprocess.run, gh_meter.run_gh, "
                "or the fetcher in this test. FileNotFoundError is raised "
                "rather than a RuntimeError because every gh caller already "
                "handles it, so an unmocked call degrades instead of hanging "
                "for the full timeout (#255)."
            )
        if _is_pgrep_call(args):
            raise FileNotFoundError(
                f"Test tried to run the real `pgrep` (argv: {list(args)!r}). "
                "The daemon's orphan cleanup would SIGTERM the user's live "
                "daemon (#333) — patch subprocess.run or the pid finder in "
                "this test. FileNotFoundError makes _pgrep_daemon_pids "
                "degrade to [] (no orphans) instead of erroring."
            )
        return real_run(args, *a, **kw)

    def guarded_popen_init(self, args, *a, **kw):
        blocked_control = _blocked_process_control(args)
        if blocked_control is not None:
            raise RuntimeError(
                f"Test tried to run the real `{blocked_control}` (argv: {list(args)!r}). "
                "It would stop or reload the user's live LaunchAgents/processes (#396) "
                "- patch the runner in this test."
            )
        if _is_worktree_launcher(args):
            raise RuntimeError(
                f"Test tried to run the real launch.sh (argv: {list(args)!r}). "
                "Inject a fake Launcher into autopilot.run_launcher (#302)."
            )
        if _is_headless_claude(args):
            raise RuntimeError(
                f"Test tried to spawn `claude -p` (argv: {list(args)!r}). Inject a fake "
                "runner into triage_worker.run_triage / invoke_claude instead (#248)."
            )
        blocked = _blocked_tmux_subcommand(args)
        if blocked is not None:
            raise RuntimeError(
                f"Test tried to spawn `tmux {blocked}` against the live tmux "
                f"server (argv: {list(args)!r}). This escapes test isolation "
                "and mutates the user's real sessions (#212) — patch the "
                "dispatcher or subprocess.Popen in this test."
            )
        if _is_pgrep_call(args):
            raise FileNotFoundError(
                f"Test tried to spawn the real `pgrep` (argv: {list(args)!r}). "
                "The daemon's orphan cleanup would SIGTERM the user's live "
                "daemon (#333) — patch subprocess.Popen in this test."
            )
        return real_popen_init(self, args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen_init)


# Bound at import, before the subprocess guard patches ``subprocess.run``, so
# the signal guard's ``ps`` lookup is immune to a test's own run mock.
_REAL_SUBPROCESS_RUN = subprocess.run


def _ps_ppid_and_command(pid: int) -> tuple[int, str] | None:
    """``(ppid, command)`` for ``pid`` via the real ``ps``, or None."""
    try:
        result = _REAL_SUBPROCESS_RUN(
            ["ps", "-o", "ppid=,command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    parts = result.stdout.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1]


@pytest.fixture(autouse=True)
def _guard_daemon_signals(monkeypatch):
    """Refuse a real signal to an agent-hq process the test did not spawn.

    Defense in depth for #333: the pgrep guard covers today's orphan scan,
    but any new way of finding PIDs would again hand ``kill_orphan_daemons``
    the user's live daemon. The kill itself is the one place every path
    meets. Signal 0 (liveness probes), the test process itself and its
    direct children pass; so does anything that isn't agent-hq. Tests that
    patch ``os.kill`` replace this guard for their block. Request this
    fixture and set ``ps_reader`` to fake what ``ps`` reports.
    """
    import os
    import types

    from agent_hq.daemon import _is_agent_hq_cmdline

    real_kill = os.kill
    guard = types.SimpleNamespace(ps_reader=_ps_ppid_and_command)

    def guarded_kill(pid, sig):
        if sig != 0 and pid > 0 and pid != os.getpid():
            info = guard.ps_reader(pid)
            if info is not None:
                ppid, command = info
                if ppid != os.getpid() and _is_agent_hq_cmdline(command):
                    raise RuntimeError(
                        f"Test tried to send signal {sig} to PID {pid} ({command!r}), "
                        "an agent-hq process this test did not spawn — likely the "
                        "user's live daemon (#333). Patch the pid finder or os.kill."
                    )
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", guarded_kill)
    return guard


@pytest.fixture(autouse=True)
def _isolate_machine_state(tmp_path_factory, monkeypatch):
    """Redirect all real-machine path constants into a per-test tmp dir.

    Allocated via ``tmp_path_factory`` (not the test's own ``tmp_path``)
    so tests that assert on their ``tmp_path`` contents — e.g. the
    atomic-write leftovers checks in ``test_hooks.py`` — don't see the
    isolation scaffolding.
    """
    tmp_path = tmp_path_factory.mktemp("isolated-machine-state")
    # --- /tmp/agent-hq (daemon + hooks + overrides) ---
    cache_file = tmp_path / "agent-hq" / "cache.json"
    states_dir = tmp_path / "agent-hq" / "states"
    states_dir.mkdir(parents=True)
    pid_file = tmp_path / "agent-hq" / "daemon.pid"
    daemon_lock = tmp_path / "agent-hq" / "daemon.lock"
    overrides_file = tmp_path / "agent-hq" / "overrides.json"

    monkeypatch.setattr("agent_hq.runtime_dir.RUNTIME_ROOT", tmp_path / "agent-hq")
    monkeypatch.setattr("agent_hq.cache.CACHE_FILE", cache_file)
    monkeypatch.setattr("agent_hq.codex_runtime.RUNTIME_DIR", tmp_path / "codex-runtimes")
    monkeypatch.setattr("agent_hq.status_collector.STATUS_FILE", tmp_path / "runtime-status.json")
    # ``from agent_hq.cache import CACHE_FILE`` creates separate bindings.
    monkeypatch.setattr("agent_hq.daemon.CACHE_FILE", cache_file)
    monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
    monkeypatch.setattr("agent_hq.daemon.PID_FILE", pid_file)
    monkeypatch.setattr("agent_hq.daemon.DAEMON_LOCK_FILE", daemon_lock)
    monkeypatch.setattr("agent_hq.hooks.HOOK_STATE_DIR", states_dir)
    monkeypatch.setattr("agent_hq.web.watcher.HOOKS_DIR", states_dir)
    monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_file)
    monkeypatch.setattr(
        "agent_hq.overrides.OVERRIDES_LOCK", overrides_file.with_suffix(".json.lock")
    )

    # --- real $HOME state ---
    config_home = tmp_path / "config" / "agent-hq"
    cache_home = tmp_path / "cache" / "agent-hq"
    monkeypatch.setattr("agent_hq.overrides.FEEDBACK_FILE", config_home / "phase-feedback.jsonl")
    monkeypatch.setattr("agent_hq.transcript.CLAUDE_PROJECTS_DIR", tmp_path / "claude-projects")
    monkeypatch.setattr("agent_hq.enrichment.CLAUDE_SESSIONS_DIR", tmp_path / "claude-sessions")
    monkeypatch.setattr("agent_hq.runtime_status.CLAUDE_SESSIONS_DIR", tmp_path / "claude-sessions")
    monkeypatch.setattr("agent_hq.enrichment.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
    monkeypatch.setattr(
        "agent_hq.hooks.CODEX_SESSION_TAGS_FILE", tmp_path / "codex" / "session-tags.jsonl"
    )
    monkeypatch.setattr("agent_hq.codex_hooks.CODEX_HOOKS_FILE", tmp_path / "codex-hooks.json")
    monkeypatch.setattr(
        "agent_hq.snapshots._DEFAULT_SNAPSHOTS_FILE", config_home / "agent-snapshots.jsonl"
    )
    monkeypatch.setattr("agent_hq.state.STATE_DIR", cache_home)
    monkeypatch.setattr("agent_hq.state.ACCESS_FILE", cache_home / "access.json")
    monkeypatch.setattr("agent_hq.state.INACTIVE_FILE", cache_home / "inactive.json")
    monkeypatch.setattr("agent_hq.config.CONFIG_PATH", config_home / "projects.yaml")
    monkeypatch.setattr("agent_hq.github._CACHE_FILE", config_home / "cache" / "github.json")
    monkeypatch.setattr(
        "agent_hq.gh_index._DEFAULT_INDEX_FILE", config_home / "cache" / "issue-index.json"
    )
    monkeypatch.setattr(
        "agent_hq.pinned_store._DEFAULT_PINNED_FILE", config_home / "pinned-sessions.json"
    )
    monkeypatch.setattr("agent_hq.ui_prefs._DEFAULT_PREFS_FILE", config_home / "ui-prefs.json")
    # #396 remote MCP: token store, logs, logger stamps. The real Keychain is
    # never read: tests inject keychain_lookup.
    monkeypatch.setattr("agent_hq.remote.config.STATE_DIR", config_home / "remote")
    monkeypatch.setattr("agent_hq.remote.config.LOG_DIR", tmp_path / "Logs" / "agent-hq")
    monkeypatch.setattr(
        "agent_hq.remote.config.REQUEST_LOG",
        tmp_path / "Logs" / "agent-hq" / "remote-requests.log",
    )

    monkeypatch.setattr(
        "agent_hq.remote.logger_events.STAMP_FILE", config_home / "remote-logger-stamps.json"
    )

    monkeypatch.setattr("agent_hq.remote.service.LAUNCH_AGENTS_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(
        "agent_hq.remote.service.SERVER_LOG", tmp_path / "Logs" / "agent-hq" / "remote.log"
    )
    monkeypatch.setattr(
        "agent_hq.remote.service.TUNNEL_LOG", tmp_path / "Logs" / "agent-hq" / "remote-tunnel.log"
    )
    monkeypatch.setattr(
        "agent_hq.remote.service.TUNNEL_CONFIG", tmp_path / "cloudflared" / "agent-hq-mcp.yml"
    )

    def _no_real_keychain():
        raise RuntimeError("real Keychain in tests: inject keychain_lookup")

    monkeypatch.setattr("agent_hq.remote.config.keychain_secret", _no_real_keychain)

    # Unified-logger transport (#231): token missing in tmp ⇒ transport
    # disabled ⇒ the suite can never POST to the real worker.
    monkeypatch.setattr(
        "agent_hq.logclient._DEFAULT_TOKEN_FILE", tmp_path / "secrets" / "logger-write.token"
    )
    monkeypatch.setattr("agent_hq.logclient._DEFAULT_QUEUE_FILE", cache_home / "logger-queue.jsonl")
    monkeypatch.setattr("agent_hq.gh_meter._DEFAULT_COUNTER_FILE", cache_home / "gh-calls.jsonl")
    monkeypatch.setattr("agent_hq.enrichment.ENRICH_ERRORS_FILE", cache_home / "enrich-errors.log")
    monkeypatch.setattr(
        "agent_hq.logreader._DEFAULT_ADMIN_TOKEN_FILE",
        tmp_path / "secrets" / "logger-admin.token",
    )
    monkeypatch.setattr(
        "agent_hq.idea.sent_store._DEFAULT_SENT_FILE", config_home / "inbox-sent.jsonl"
    )
    monkeypatch.setattr("agent_hq.idea.labels.DEFAULT_CACHE_PATH", cache_home / "idea-inbox.json")
    # ``from ..labels import DEFAULT_CACHE_PATH`` creates a separate binding.
    monkeypatch.setattr("agent_hq.idea.handler.DEFAULT_CACHE_PATH", cache_home / "idea-inbox.json")
    monkeypatch.setattr("agent_hq.idea.cli.DEFAULT_TOKEN_FILE", config_home / "web-token")
    monkeypatch.setattr(
        "agent_hq.capabilities.DEFAULT_LAUNCHER_PATH",
        tmp_path / "claude" / "skills" / "worktree" / "scripts" / "launch.sh",
    )
    # Autopilot (#302) read-only inputs. ``autopilot.launch``'s ``script=``
    # default binds at def time, so the launch.sh argv guard above stays the
    # real protection; these keep module-level reads out of ~/.claude.
    monkeypatch.setattr(
        "agent_hq.autopilot.LAUNCH_SCRIPT",
        tmp_path / "claude" / "skills" / "worktree" / "scripts" / "launch.sh",
    )
    goal_template = tmp_path / "claude" / "skills" / "goal-mode" / "templates"
    monkeypatch.setattr("agent_hq.autopilot.GOAL_TEMPLATE", goal_template / "dispatch-condition.md")
    # ``from agent_hq.autopilot import GOAL_TEMPLATE`` creates a separate binding.
    monkeypatch.setattr(
        "agent_hq.autopilot_cli.GOAL_TEMPLATE", goal_template / "dispatch-condition.md"
    )

    # Not path constants (so not pinned in test_isolation.py): the suite runs
    # inside tmux, and the dashboard's initial-focus lookup (#176) shells out to
    # ``tmux display-message`` when $TMUX_PANE is set — or, popup fallback, when
    # $TMUX alone is set — drop both so pilot tests never touch the live tmux
    # server.
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    # Tests choose wrapped/unwrapped runtime explicitly, independent of the host.
    monkeypatch.delenv("AGENT_HQ_CODEX_WRAPPED", raising=False)

    # Unified-logger transport (#231) env seams: LOGGER_TOKEN_FILE/
    # LOGGER_QUEUE_FILE/LOGGER_URL are consulted before the monkeypatched
    # module constants above, so an ambient env var would silently bypass
    # isolation — drop them so the suite always reads the patched paths.
    monkeypatch.delenv("LOGGER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("LOGGER_QUEUE_FILE", raising=False)
    monkeypatch.delenv("LOGGER_URL", raising=False)
    monkeypatch.delenv("LOGGER_ADMIN_TOKEN_FILE", raising=False)
    monkeypatch.delenv("LOGGER_COOLDOWN_MS", raising=False)
    # logger#9 failure cooldown is module state: one test's transient failure
    # would otherwise put the next test's log() on the cooldown path.
    monkeypatch.setattr("agent_hq.logclient._last_transient_at", float("-inf"))
    monkeypatch.setattr("agent_hq.logclient._cooldown_appends", 0)

    # In-memory process caches (#254) — not path constants, so not pinned in
    # test_isolation.py, but they leak across tests exactly the same way. The
    from agent_hq import github as _github

    _github.invalidate_worktree_cache()
    # Only reset when a test has imported it: it pulls in mcp_server and the
    # MCP SDK, which most tests never need.
    _remote_health = sys.modules.get("agent_hq.remote.health")
    if _remote_health is not None:
        _remote_health.reset_caches()
    # The shared IssueIndex handle captures its path when first built, so a
    # handle left over from a previous test points at that test's tmp dir.
    _github.clear_issue_index_cache()
    # gh_budget keeps its learned rate-limit window in module globals;
    # one test's observed rejection would otherwise block the next
    # test's background refresh.
    from agent_hq import gh_budget as _gh_budget

    _gh_budget.clear_cache()
    # gh_board remembers whether an owner is a user or an organization, so a
    # resolution learned from one test's fake gh would answer the next.
    from agent_hq import gh_board as _gh_board

    _gh_board.clear_owner_kind_cache()
