"""``agent-hq remote …`` (#396): serve, check-config, tokens, kill, service."""

import argparse
import asyncio
import sys
from datetime import datetime
from typing import TextIO

from agent_hq import eventlog
from agent_hq.config import ConfigError
from agent_hq.service import ServiceError

from . import request_log
from .config import RemoteConfigError, load_remote_config

HEARTBEAT_S = 300.0

AUTOMATIONS_YAML = """\
automations:
  - name: agent-hq-remote
    # agent-hq #396: read-only remote MCP for claude.ai/Duet (KeepAlive,
    # 127.0.0.1:{port}). Heartbeat every 5 min; kill: agent-hq remote kill.
    label: com.benralston.agent-hq.remote
    log_file: "~/Library/Logs/agent-hq/remote.log"
    failure_pattern: "ERROR|CRITICAL"
    stale_after: 20m
  - name: agent-hq-remote-tunnel
    # cloudflared named tunnel agent-hq-mcp -> agents.example.com (KeepAlive).
    label: com.benralston.agent-hq.remote-tunnel
    log_file: "~/Library/Logs/agent-hq/remote-tunnel.log"
    failure_pattern: " ERR "
"""


def add_remote_parser(subparsers) -> None:
    """Register ``agent-hq remote`` on the top-level subparsers."""
    parser = subparsers.add_parser(
        "remote",
        help="Read-only remote MCP for claude.ai / Duet (#396)",
        description=(
            "Serve the owner's sessions, backlog, automations health and recent errors over "
            "Streamable HTTP on 127.0.0.1 behind GitHub OAuth, for a claude.ai custom "
            "connector. See docs/remote-connector.md."
        ),
    )
    parser.set_defaults(_remote_parser=parser)
    sub = parser.add_subparsers(dest="remote_action", metavar="<command>")
    sub.add_parser("serve", help="Run the HTTP server (what the LaunchAgent runs)")
    sub.add_parser("check-config", help="Validate the remote: block + secret; show what's exposed")
    sub.add_parser("revoke-all", help="Wipe every issued token, pin and in-flight sign-in")
    allow = sub.add_parser(
        "allow-client", help="Allow another connector client_id (after re-adding it in claude.ai)"
    )
    allow.add_argument("client_id")
    sub.add_parser("kill", help="Kill switch: stop the tunnel, then the server")
    service = sub.add_parser("service", help="Manage the server and tunnel LaunchAgents")
    service_sub = service.add_subparsers(dest="remote_service_action", metavar="<action>")
    install = service_sub.add_parser("install", help="Install and load the server LaunchAgent")
    install.add_argument(
        "--tunnel", action="store_true", help="Install the cloudflared tunnel LaunchAgent instead"
    )
    service_sub.add_parser("uninstall", help="Unload and remove both LaunchAgents")
    service_sub.add_parser("status", help="Show whether both LaunchAgents are loaded")


async def heartbeat_loop(stream: TextIO | None = None, interval: float = HEARTBEAT_S) -> None:
    """One line per ``interval`` on stderr (remote.log): the Automations tab's
    ``stale_after`` tells a hung server from a quiet healthy one."""
    while True:
        out = stream or sys.stderr
        out.write(
            f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
            "agent-hq remote: heartbeat\n"
        )
        out.flush()
        await asyncio.sleep(interval)


def _run_server(app, cfg) -> None:
    import uvicorn

    config = uvicorn.Config(
        app,
        host=cfg.host,
        port=cfg.port,
        log_level="warning",
        access_log=False,
        # The tunnel connects from 127.0.0.1; never let X-Forwarded-* rewrite
        # the client/scheme (issuer and callback URLs come from config anyway).
        proxy_headers=False,
        forwarded_allow_ips="",
        server_header=False,
        log_config=None,  # keep uvicorn on the redacting root handler
    )
    server = uvicorn.Server(config)

    async def main():
        beat = asyncio.create_task(heartbeat_loop())
        try:
            await server.serve()
        finally:
            beat.cancel()

    asyncio.run(main())


def _load(require_secret: bool = True):
    from agent_hq.config import load_config

    return load_remote_config(load_config(), require_secret=require_secret)


def _serve() -> int:
    from . import logger_events

    eventlog.set_process_role("remote")
    request_log.configure_remote_logging()
    try:
        cfg = _load()
    except Exception as exc:  # a malformed projects.yaml raises ConfigError, not ours
        logger_events.report_config_failure(exc)
        # "ERROR" keeps the Automations failure_pattern matching the last line.
        print(f"ERROR agent-hq remote: {exc}", file=sys.stderr)
        return 2
    phase = "build"
    try:
        from .app import build_remote_app

        app = build_remote_app(cfg)
        phase = "serve"
        print(
            f"agent-hq remote: serving http://{cfg.host}:{cfg.port}/mcp for {cfg.mcp_url}",
            file=sys.stderr,
        )
        eventlog.emit("info", "remote mcp started", {"port": cfg.port})
        _run_server(app, cfg)
    except BaseException as exc:
        logger_events.report_server_failure(phase, exc)
        if isinstance(exc, KeyboardInterrupt) or (
            isinstance(exc, SystemExit) and exc.code in (0, None)
        ):
            return 0
        print(f"ERROR agent-hq remote: {type(exc).__name__} during {phase}", file=sys.stderr)
        return 1
    return 0


def _check_config() -> int:
    from .app import remote_tool_copies
    from .tools import build_tool_source

    cfg = _load()
    source = build_tool_source(writes=cfg.writes)
    tools = sorted(t.name for t in remote_tool_copies(source, writes=cfg.writes))
    print(f"public URL:      {cfg.mcp_url}")
    print(f"listening on:    http://{cfg.host}:{cfg.port}/mcp")
    print(f"allowed GitHub:  {cfg.allowed_github_login} (id {cfg.allowed_github_id})")
    print(f"tools ({len(tools)}):       {', '.join(tools)}")
    print(f"writes:          {'on' if cfg.writes else 'off'} (remote.writes)")
    if cfg.writes:
        print(f"sessions:        {', '.join(cfg.sessions) or 'none'} (remote.sessions)")
    print("client secret:   found")
    print(f"token store:     {cfg.state_dir}")
    print(f"request log:     {cfg.log_path}")
    print("\nAutomations tab entries for ~/.config/agent-hq/projects.yaml:\n")
    print(AUTOMATIONS_YAML.format(port=cfg.port))
    return 0


def run_remote(args: argparse.Namespace) -> int:
    action = getattr(args, "remote_action", None)
    try:
        if action == "serve":
            return _serve()
        if action == "check-config":
            return _check_config()
        if action in ("revoke-all", "allow-client"):
            from . import config as remote_config
            from .auth import TokenStore

            # The fixed state dir, not the config: an emergency revoke must
            # not depend on projects.yaml parsing (#396 review).
            store = TokenStore(remote_config.STATE_DIR)
            if action == "revoke-all":
                # One pass is enough: every grant write checks the epoch
                # this bumps (notes-graph #192, vendored at 29ab497).
                count = store.revoke_all()
                print(f"Revoked {count} tokens/codes. Claude must sign in again.")
            else:
                store.allow_client(args.client_id)
                print(f"Allowed connector client {args.client_id}. Sign in again from claude.ai.")
            return 0
        if action == "kill":
            from . import service

            return service.kill()
        if action == "service":
            from . import service

            sub = getattr(args, "remote_service_action", None)
            if sub == "install":
                return service.install(tunnel=args.tunnel)
            if sub == "uninstall":
                return service.uninstall()
            if sub == "status":
                return service.status()
    except (RemoteConfigError, ConfigError, ServiceError, ValueError) as exc:
        print(f"agent-hq remote: {exc}", file=sys.stderr)
        return 2
    parser = getattr(args, "_remote_parser", None)
    if parser is not None:
        parser.print_help()
    return 2
