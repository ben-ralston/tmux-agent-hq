"""Persistent hosting via a macOS launchd LaunchAgent (issue #100).

Manages the ``agent-hq web`` server as a per-user LaunchAgent in the
``gui/$UID`` domain so it keeps running in the login session (where it can
see the user's tmux server), survives crashes (``KeepAlive``), and starts at
login (``RunAtLoad``). macOS-only — launchd has no portable equivalent.

``build_plist`` is a pure function (the testable core); everything else
shells out to ``launchctl`` and touches the filesystem.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.benralston.agent-hq.web"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "agent-hq"
STDOUT_LOG = LOG_DIR / "web.out.log"
STDERR_LOG = LOG_DIR / "web.err.log"


class ServiceError(RuntimeError):
    """A user-facing service-management failure (printed, exit 1)."""


def _require_macos() -> None:
    """Raise ``ServiceError`` when not running on macOS."""
    if sys.platform != "darwin":
        raise ServiceError("agent-hq web service is only supported on macOS (launchd).")


def _resolve_executable() -> str:
    """Absolute path to the ``agent-hq`` console script.

    launchd ignores the login shell's PATH, so element 0 of
    ``ProgramArguments`` must be absolute. Resolved at install time.
    """
    path = shutil.which("agent-hq")
    if not path:
        raise ServiceError(
            "agent-hq is not on your PATH. Run `./run install-global` first "
            "so the service can point at an absolute executable path."
        )
    return path


def _launchd_path() -> str:
    """PATH baked into the plist so the server can find its tools.

    The server shells out to tmux/git/gh/tailscale (Homebrew on Apple
    Silicon); launchd's minimal default PATH would not find them.
    """
    return ":".join(
        [
            "/opt/homebrew/bin",  # Apple Silicon Homebrew
            "/opt/homebrew/sbin",
            "/usr/local/bin",  # Intel Homebrew
            "/usr/local/sbin",
            str(Path.home() / ".local" / "bin"),
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
    )


def _uid() -> int:
    return os.getuid()


def _domain_target() -> str:
    """The ``gui/$UID/<label>`` service target for launchctl."""
    return f"gui/{_uid()}/{LABEL}"


def build_plist(
    *,
    executable: str,
    bind: str,
    port: int,
    token_file: Path,
    label: str = LABEL,
    stdout_log: Path = STDOUT_LOG,
    stderr_log: Path = STDERR_LOG,
    path_env: str | None = None,
) -> str:
    """Build the LaunchAgent plist XML. Pure — no I/O, no subprocess."""
    plist: dict = {
        "Label": label,
        "ProgramArguments": [
            executable,
            "web",
            "--bind",
            bind,
            "--port",
            str(port),
            "--token-file",
            str(token_file),
            # Suppress the token/QR banner so the secret never lands in the
            # persistent log file the LaunchAgent redirects stdout to.
            "--quiet",
        ],
        "EnvironmentVariables": {"PATH": path_env or _launchd_path()},
        "RunAtLoad": True,
        "KeepAlive": True,
        # Default respawn floor; also the knob that lets the Tailscale boot
        # race self-heal: if the tailnet IP isn't up yet, uvicorn fails to
        # bind, the process exits, and KeepAlive relaunches it ~10s later.
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "StandardOutPath": str(stdout_log),
        "StandardErrorPath": str(stderr_log),
        "WorkingDirectory": str(Path.home()),
    }
    return plistlib.dumps(plist).decode("utf-8")


def _launchctl(args: list[str]) -> subprocess.CompletedProcess:
    """Run ``launchctl`` with the given args, capturing output."""
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
    )


def _is_loaded() -> bool:
    """True if the LaunchAgent is registered (``launchctl print`` succeeds)."""
    return _launchctl(["print", _domain_target()]).returncode == 0


def _resolve_bind(bind: str | None) -> str:
    """Resolve the bind address, guarding against a localhost-only service.

    When ``bind`` is None we auto-detect the Tailscale IP. If detection
    fails (returns 127.0.0.1) we refuse rather than bake a service that is
    only reachable from the host itself.
    """
    if bind is not None:
        return bind
    from .cli import detect_tailnet_ip

    detected = detect_tailnet_ip()
    if detected == "127.0.0.1":
        raise ServiceError(
            "Tailscale IP not detected — start Tailscale, or pass an explicit "
            "--bind (e.g. `--bind 0.0.0.0` to listen on all interfaces, "
            "token-gated)."
        )
    return detected


def install_service(*, bind: str | None, port: int, token_file: Path) -> int:
    """Write the LaunchAgent plist and (re)load it. Idempotent."""
    _require_macos()
    executable = _resolve_executable()
    resolved_bind = _resolve_bind(bind)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(
        build_plist(
            executable=executable,
            bind=resolved_bind,
            port=port,
            token_file=token_file,
        )
    )

    # Reload idempotently: bootout any existing job (best-effort — a "not
    # loaded" nonzero is fine), then bootstrap the freshly-written plist.
    _launchctl(["bootout", _domain_target()])
    result = _launchctl(["bootstrap", f"gui/{_uid()}", str(PLIST_PATH)])
    # launchd can return "5: Input/output error" from bootstrap right after a
    # bootout (the unload hasn't settled) even though the new job does load.
    # Tolerate *only* that specific signature when the job is loaded afterward;
    # any other failure (e.g. "service already loaded" from a bootout that
    # didn't take) is real — surface it rather than report a stale success.
    spurious_eio = "Input/output error" in result.stderr and _is_loaded()
    if result.returncode != 0 and not spurious_eio:
        raise ServiceError(
            f"launchctl bootstrap failed: {result.stderr.strip() or result.returncode}"
        )

    print(f"Installed and loaded {LABEL}")
    print(f"Dashboard:  http://{resolved_bind}:{port}/agents")
    print(f"Plist:      {PLIST_PATH}")
    print(f"Logs:       {STDOUT_LOG}")
    print(f"            {STDERR_LOG}")
    return 0


def uninstall_service() -> int:
    """Unload the job, remove the plist, and stop the discovery daemon."""
    _require_macos()
    _launchctl(["bootout", _domain_target()])  # best-effort
    PLIST_PATH.unlink(missing_ok=True)

    from .daemon import stop_daemon

    stop_daemon()  # gracefully no-ops if not running

    print(f"Uninstalled {LABEL}")
    return 0


def _parse_pid(text: str) -> int | None:
    """Pull the ``pid = N`` value out of ``launchctl print`` output."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid ="):
            try:
                return int(stripped.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None


def _bind_url_from_plist() -> str | None:
    """Reconstruct the dashboard URL from the installed plist, if present."""
    try:
        with PLIST_PATH.open("rb") as fh:
            plist = plistlib.load(fh)
    except (FileNotFoundError, OSError):
        return None
    args = plist.get("ProgramArguments", [])

    def _value_after(flag: str) -> str | None:
        # Guard against a manually-edited plist where the flag is the last
        # element (no following value) — return None rather than IndexError.
        if flag in args:
            idx = args.index(flag)
            if idx + 1 < len(args):
                return args[idx + 1]
        return None

    bind = _value_after("--bind")
    port = _value_after("--port")
    if bind and port:
        return f"http://{bind}:{port}/agents"
    return None


def service_status() -> int:
    """Report loaded/running state. Returns 0 if loaded, 1 otherwise."""
    _require_macos()
    result = _launchctl(["print", _domain_target()])
    if result.returncode != 0:
        print(f"{LABEL} is not loaded.")
        print("Install it with: agent-hq web service install")
        return 1

    pid = _parse_pid(result.stdout)
    if pid is not None:
        print(f"{LABEL}: loaded, running (PID {pid})")
    else:
        print(f"{LABEL}: loaded, not running (will relaunch via KeepAlive)")

    url = _bind_url_from_plist()
    if url:
        print(f"Dashboard:  {url}")
    print(f"Plist:      {PLIST_PATH}")
    print(f"Logs:       {STDOUT_LOG}")
    print(f"            {STDERR_LOG}")
    return 0


def restart_service() -> int:
    """Force-restart the running job via ``launchctl kickstart -k``."""
    _require_macos()
    result = _launchctl(["kickstart", "-k", _domain_target()])
    if result.returncode != 0:
        raise ServiceError(
            f"launchctl kickstart failed: {result.stderr.strip() or result.returncode}. "
            "Is the service installed? Try: agent-hq web service install"
        )
    print(f"Restarted {LABEL}")
    return 0
