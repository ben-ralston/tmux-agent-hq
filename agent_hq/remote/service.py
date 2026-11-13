"""LaunchAgents and the kill switch for remote mode (#396).

Two KeepAlive jobs, separate from the web service and from notes-graph's
connector: the server (``agent-hq remote serve``) and its own cloudflared
tunnel (``agents.example.com``). ``kill`` stops the tunnel first, so nothing
public reaches the server while it stops, and touches nothing else: never
notes-graph's agents, never a blanket ``pkill cloudflared``.
"""

import plistlib
import subprocess
import sys
from pathlib import Path

from agent_hq import service as web_service

LABEL = "com.benralston.agent-hq.remote"
TUNNEL_LABEL = "com.benralston.agent-hq.remote-tunnel"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
SERVER_LOG = Path.home() / "Library" / "Logs" / "agent-hq" / "remote.log"
TUNNEL_LOG = Path.home() / "Library" / "Logs" / "agent-hq" / "remote-tunnel.log"
TUNNEL_CONFIG = Path.home() / ".cloudflared" / "agent-hq-mcp.yml"
DEFAULT_CLOUDFLARED = "/opt/homebrew/bin/cloudflared"
SERVE_PATTERN = "agent-hq remote serve"


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


def _uid() -> int:
    return web_service._uid()


def _target(label: str) -> str:
    return f"gui/{_uid()}/{label}"


def _plist_path(label: str) -> Path:
    return LAUNCH_AGENTS_DIR / f"{label}.plist"


def build_server_plist(executable: str, path_env: str | None = None) -> str:
    """The server LaunchAgent. Pure. PATH carries Homebrew so ``gh`` resolves."""
    plist = {
        "Label": LABEL,
        "ProgramArguments": [executable, "remote", "serve"],
        "EnvironmentVariables": {"PATH": path_env or web_service._launchd_path()},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 30,
        "StandardOutPath": str(SERVER_LOG),
        "StandardErrorPath": str(SERVER_LOG),
        "WorkingDirectory": str(Path.home()),
    }
    return plistlib.dumps(plist).decode("utf-8")


def build_tunnel_plist(cloudflared: str = DEFAULT_CLOUDFLARED) -> str:
    """The tunnel LaunchAgent (named tunnel agent-hq-mcp). Pure."""
    plist = {
        "Label": TUNNEL_LABEL,
        "ProgramArguments": [cloudflared, "tunnel", "--config", str(TUNNEL_CONFIG), "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(TUNNEL_LOG),
        "StandardErrorPath": str(TUNNEL_LOG),
    }
    return plistlib.dumps(plist).decode("utf-8")


def _load(label: str, text: str) -> int:
    path = _plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_name(path.name + ".disabled").unlink(missing_ok=True)  # left by kill
    SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _run(["launchctl", "bootout", _target(label)])  # best-effort reload
    result = _run(["launchctl", "bootstrap", f"gui/{_uid()}", str(path)])
    loaded = _loaded(label)
    # Same tolerance as the web service: a bootstrap right after a bootout can
    # report "Input/output error" although the job loads.
    if result.returncode != 0 and not ("Input/output error" in result.stderr and loaded):
        print(
            f"launchctl bootstrap failed for {label}: {result.stderr.strip() or result.returncode}",
            file=sys.stderr,
        )
        return 1
    print(f"Installed and loaded {label}")
    print(f"Plist: {path}")
    return 0


def install(tunnel: bool = False) -> int:
    """Write and (re)load the server LaunchAgent, or with ``tunnel`` the tunnel's."""
    if tunnel:
        if not TUNNEL_CONFIG.exists():
            print(
                f"No tunnel config at {TUNNEL_CONFIG}: create the agent-hq-mcp tunnel "
                "first (docs/remote-connector.md).",
                file=sys.stderr,
            )
            return 1
        return _load(TUNNEL_LABEL, build_tunnel_plist())
    return _load(LABEL, build_server_plist(web_service._resolve_executable()))


def uninstall() -> int:
    """Unload both jobs (tunnel first) and remove their plists."""
    for label in (TUNNEL_LABEL, LABEL):
        _run(["launchctl", "bootout", _target(label)])
        _plist_path(label).unlink(missing_ok=True)
        _plist_path(label).with_name(f"{label}.plist.disabled").unlink(missing_ok=True)
        print(f"Uninstalled {label}")
    return 0


def status() -> int:
    """Loaded/running state of both jobs. 0 when the server is loaded."""
    rc = 1
    for label in (LABEL, TUNNEL_LABEL):
        result = _run(["launchctl", "print", _target(label)])
        if result.returncode != 0:
            print(f"{label}: not loaded")
            continue
        if label == LABEL:
            rc = 0
        pid = web_service._parse_pid(result.stdout)
        print(f"{label}: loaded, " + (f"running (PID {pid})" if pid else "not running"))
    print(f"Logs: {SERVER_LOG}\n      {TUNNEL_LOG}")
    return rc


def _loaded(label: str) -> bool:
    return _run(["launchctl", "print", _target(label)]).returncode == 0


def kill() -> int:
    """Kill switch: stop the tunnel, then the server, and keep them stopped.

    Each plist is renamed to ``*.plist.disabled``, so a reboot or re-login
    doesn't bring the tunnel back (RunAtLoad); ``service install`` undoes it.
    Tolerates "not running", but returns 1 when a job is still loaded
    afterwards - a kill switch must never report success it didn't achieve.
    """
    steps = (
        (["launchctl", "bootout", _target(TUNNEL_LABEL)], "tunnel LaunchAgent"),
        (["launchctl", "bootout", _target(LABEL)], "server LaunchAgent"),
        (["pkill", "-f", SERVE_PATTERN], "stray `agent-hq remote serve` process"),
    )
    for argv, what in steps:
        stopped = _run(argv).returncode == 0
        print(f"stopped {what}" if stopped else f"no {what} running")
    for label in (TUNNEL_LABEL, LABEL):
        plist = _plist_path(label)
        if plist.exists():
            plist.replace(plist.with_name(plist.name + ".disabled"))
            print(f"disabled {plist.name} (agent-hq remote service install re-enables)")
    still = [label for label in (TUNNEL_LABEL, LABEL) if _loaded(label)]
    for label in still:
        print(f"{label} is STILL LOADED - run: launchctl bootout {_target(label)}", file=sys.stderr)
    return 1 if still else 0
