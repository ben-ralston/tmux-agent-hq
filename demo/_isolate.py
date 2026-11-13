"""Point a process, and every process it starts, at a throwaway demo environment.

Agent HQ reads live machine state: tmux, ``~/.claude``, ``~/.config/agent-hq``,
``/tmp/agent-hq``, ``gh`` and ``launchctl``. The demo must touch none of it.

Most of the redirection travels in the environment, so child processes
inherit it without doing anything:

* ``HOME`` moves to ``<root>/home``, which relocates every ``~/…`` path.
* ``TMUX_TMPDIR`` moves to ``<root>/tmux`` and a foreign ``TMUX`` is dropped,
  so plain ``tmux`` commands reach a separate tmux server.
* ``demo/bin`` goes first on ``PATH``. Its ``agent-hq``, ``agent-hq-hook`` and
  ``agent-hq-mcp`` run this checkout inside the demo, so a feature that
  re-runs ``agent-hq`` by name (the dashboard's Restart, fzf key bindings, the
  Codex wrapper) never reaches an installed copy. Its ``gh`` and ``launchctl``
  answer from ``demo/data.py`` and refuse writes; ``pgrep``, ``pkill``,
  ``osascript``, ``tailscale``, ``security`` and ``cloudflared`` are stand-ins
  that do nothing.
* ``GIT_ALLOW_PROTOCOL=file`` stops git from using the network.

The app has no setting for its ``/tmp/agent-hq`` runtime directory; those paths
are module constants. They are repointed at ``<root>/runtime`` by an import
hook, and ``demo/site`` on ``PYTHONPATH`` installs the same hook in every
Python child (see ``demo/site/sitecustomize.py``), so a fresh interpreter that
imports ``agent_hq`` gets the demo paths too. The hook also disables
``daemon.kill_orphan_daemons``, which finds daemons with ``pgrep``.

Last, ``os.kill`` is wrapped: a real signal is delivered only to a process
whose command line belongs to the demo. That holds even if a path above were
ever wrong.

Nothing in ``agent_hq/`` is changed. The root defaults to ``demo/.state``;
set ``AGENT_HQ_DEMO_ROOT`` to put it elsewhere.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
REPO_DIR = DEMO_DIR.parent
LOGGER_PORT = int(os.environ.get("AGENT_HQ_DEMO_LOGGER_PORT", "9913"))

# Ambient settings that would let the demo reach a real account or server.
_DROPPED_ENV = (
    "TMUX",
    "TMUX_PANE",
    "ANTHROPIC_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "LOGGER_TOKEN_FILE",
    "LOGGER_QUEUE_FILE",
    "LOGGER_ADMIN_TOKEN_FILE",
    "AGENT_HQ_CODEX_WRAPPED",
    "AGENT_HQ_DEBUG",
)


def demo_root() -> Path:
    raw = os.environ.get("AGENT_HQ_DEMO_ROOT")
    return Path(raw).resolve() if raw else DEMO_DIR / ".state"


def tmux_socket(root: Path | None = None) -> Path:
    """The demo tmux server's socket (what ``tmux -S`` takes)."""
    return (root or demo_root()) / "tmux" / f"tmux-{os.getuid()}" / "default"


# --- signals -----------------------------------------------------------------


def _process_info(pid: int) -> tuple[int, str] | None:
    """``(parent pid, command line)`` of a process, or None if it cannot be read."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "ppid=,command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        parent, _, command = result.stdout.strip().partition(" ")
        return int(parent), command.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def is_demo_process(pid: int, root: Path | None = None) -> bool:
    """True when ``pid``'s command line names the demo's scripts or the demo root."""
    info = _process_info(pid)
    if info is None:
        return False
    markers = (str(DEMO_DIR), str(root or demo_root()))
    return any(marker in info[1] for marker in markers)


_real_kill = os.kill


def _guarded_kill(pid: int, sig: int) -> None:
    """``os.kill`` that signals only this process's children and demo processes.

    Signal 0 is a liveness probe and always passes. A pid that no longer
    exists falls through to the real call, which raises as usual.
    """
    if sig != 0 and pid != os.getpid():
        info = _process_info(pid)
        own_child = info is not None and info[0] == os.getpid()
        if info is not None and not own_child and not is_demo_process(pid):
            raise PermissionError(f"demo: refusing to send signal {sig} to non-demo process {pid}")
    _real_kill(pid, sig)


_guarded_kill._demo_guard = True


# --- runtime paths -----------------------------------------------------------


def _no_orphan_cleanup(**_kwargs) -> list[int]:
    return []


def runtime_patches(root: Path) -> dict[str, dict[str, object]]:
    """Module attributes that replace the hard-coded ``/tmp/agent-hq`` paths."""
    runtime = root / "runtime"
    cache_file, states = runtime / "cache.json", runtime / "states"
    return {
        "agent_hq.runtime_dir": {"RUNTIME_ROOT": runtime},
        "agent_hq.cache": {"CACHE_FILE": cache_file},
        "agent_hq.daemon": {
            "CACHE_FILE": cache_file,
            "PID_FILE": runtime / "daemon.pid",
            "DAEMON_LOCK_FILE": runtime / "daemon.lock",
            "kill_orphan_daemons": _no_orphan_cleanup,
        },
        "agent_hq.hooks": {"HOOK_STATE_DIR": states},
        "agent_hq.web.watcher": {"CACHE_FILE": cache_file, "HOOKS_DIR": states},
        "agent_hq.overrides": {
            "OVERRIDES_FILE": runtime / "overrides.json",
            "OVERRIDES_LOCK": runtime / "overrides.json.lock",
        },
        "agent_hq.status_collector": {"STATUS_FILE": runtime / "runtime-status.json"},
        "agent_hq.codex_runtime": {"RUNTIME_DIR": runtime / "codex-runtimes"},
    }


class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, inner, attributes):
        self._inner, self._attributes = inner, attributes

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        self._inner.exec_module(module)
        for name, value in self._attributes.items():
            setattr(module, name, value)


class _PatchingFinder(importlib.abc.MetaPathFinder):
    """Applies ``runtime_patches`` to each listed module right after it is imported."""

    def __init__(self, patches):
        self._patches, self._resolving = patches, set()

    def find_spec(self, name, path=None, target=None):
        if name not in self._patches or name in self._resolving:
            return None
        self._resolving.add(name)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            self._resolving.discard(name)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PatchingLoader(spec.loader, self._patches[name])
        return spec


def install_hooks(root: Path) -> None:
    """Repoint the runtime paths and guard ``os.kill``. Safe to call twice."""
    # Compared by marker and name, not identity: this module can be loaded
    # twice in one process (by sitecustomize and by a demo script).
    if not getattr(os.kill, "_demo_guard", False):
        os.kill = _guarded_kill
    if any(type(finder).__name__ == "_PatchingFinder" for finder in sys.meta_path):
        return
    patches = runtime_patches(root)
    for name, attributes in patches.items():
        module = sys.modules.get(name)
        if module is not None:
            for attribute, value in attributes.items():
                setattr(module, attribute, value)
    sys.meta_path.insert(0, _PatchingFinder(patches))


# --- entry point -------------------------------------------------------------


def isolate() -> Path:
    """Redirect this process and its children, then return the demo root."""
    root = demo_root()
    home, runtime = root / "home", root / "runtime"
    for directory in (home, root / "tmux", root / "bin"):
        directory.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime.chmod(0o700)
    # tmux creates this itself for a default socket, but not for ``tmux -S``.
    tmux_socket(root).parent.mkdir(mode=0o700, exist_ok=True)

    # Inside a pane of the demo tmux server, $TMUX already names the demo
    # socket and the CLI needs it. Any other $TMUX is a real server: drop it.
    inside_demo_tmux = os.environ.get("TMUX", "").split(",")[0] == str(tmux_socket(root))
    for name in _DROPPED_ENV:
        if inside_demo_tmux and name in ("TMUX", "TMUX_PANE"):
            continue
        os.environ.pop(name, None)
    os.environ["AGENT_HQ_DEMO_ROOT"] = str(root)
    os.environ["AGENT_HQ_DEMO_PYTHON"] = sys.executable
    os.environ["HOME"] = str(home)
    os.environ["TMUX_TMPDIR"] = str(root / "tmux")
    os.environ["LOGGER_URL"] = f"http://127.0.0.1:{LOGGER_PORT}"
    # The staged repos name a GitHub remote that does not exist. Local-only
    # git keeps a fetch or push from ever leaving the machine.
    os.environ["GIT_ALLOW_PROTOCOL"] = "file"
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    for variable, entries in (
        ("PATH", [str(DEMO_DIR / "bin"), str(root / "bin")]),
        ("PYTHONPATH", [str(DEMO_DIR / "site")]),
    ):
        rest = [p for p in os.environ.get(variable, "").split(os.pathsep) if p not in entries]
        os.environ[variable] = os.pathsep.join([*entries, *[p for p in rest if p]])

    if str(REPO_DIR) not in sys.path:
        sys.path.insert(0, str(REPO_DIR))
    install_hooks(root)

    if Path.home() != home:
        raise RuntimeError(f"HOME did not move to the demo root ({Path.home()})")
    return root


def effective_paths() -> dict[str, str]:
    """What this process would actually read, write and run. For checks."""
    import shutil

    from agent_hq import (
        cache,
        codex_runtime,
        daemon,
        hooks,
        overrides,
        restart,
        runtime_dir,
        status_collector,
    )
    from agent_hq.web import watcher

    report = {
        "home": str(Path.home()),
        "tmux_socket": str(tmux_socket()),
        "runtime_root": str(runtime_dir.RUNTIME_ROOT),
        "cache_file": str(cache.CACHE_FILE),
        "daemon_cache_file": str(daemon.CACHE_FILE),
        "watcher_cache_file": str(watcher.CACHE_FILE),
        "pid_file": str(daemon.PID_FILE),
        "daemon_lock": str(daemon.DAEMON_LOCK_FILE),
        "hook_state_dir": str(hooks.HOOK_STATE_DIR),
        "watcher_hooks_dir": str(watcher.HOOKS_DIR),
        "overrides_file": str(overrides.OVERRIDES_FILE),
        "status_file": str(status_collector.STATUS_FILE),
        "codex_runtime_dir": str(codex_runtime.RUNTIME_DIR),
        "orphan_cleanup": daemon.kill_orphan_daemons.__name__,
        "kill_guard": os.kill.__name__,
        "restart_executable": str(restart._agent_hq_executable()),
    }
    for program in ("agent-hq", "agent-hq-hook", "gh", "launchctl", "pgrep", "osascript"):
        report[f"which_{program}"] = str(shutil.which(program))
    return report
