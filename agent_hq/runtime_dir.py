"""Private ``/tmp/agent-hq`` runtime root (#263 F2/F4/F6).

``/tmp/agent-hq`` holds the daemon PID file, the discovery cache, hook state
(including each session's last assistant message) and the codex runtime
sockets. ``/tmp`` is shared: any local user can create ``/tmp/agent-hq``
first after a reboot and then read, swap or plant files under it. Every
writer therefore creates directories through :func:`ensure_dir`, which makes
the root a 0700 directory owned by this user — so nothing beneath it is
reachable by anyone else — and refuses one that is not.

The paths themselves are unchanged; external readers (hooks, the tmux status
line) keep working.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

RUNTIME_ROOT = Path("/tmp/agent-hq")


class UnsafeRuntimeDirError(FileNotFoundError):
    """The runtime root is a symlink, not a directory, not ours, or (for a
    reader) writable by others.

    A ``FileNotFoundError`` (so an ``OSError``) on purpose: every caller
    already treats a missing runtime file as "nothing there" and degrades,
    which is exactly right for data under a root someone else controls.
    """


def _secure_root(root: Path) -> None:
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise UnsafeRuntimeDirError(
            f"{root} is not a directory owned by this user, so agent-hq will not "
            f"use it (another account may have created it). /tmp is sticky, so it "
            f"cannot be removed without admin rights: inspect it, then run "
            f"`sudo rm -rf {root}`, or reboot (which clears /tmp)"
        )
    if stat.S_IMODE(info.st_mode) != 0o700:
        # Ours and a real directory: a sticky /tmp stops anyone else from
        # swapping it between the lstat and this chmod.
        os.chmod(root, 0o700)


def ensure_dir(path: Path) -> None:
    """``mkdir -p path``, first securing the runtime root when ``path`` is
    inside it. Paths elsewhere are a plain ``mkdir``."""
    root = RUNTIME_ROOT
    if path == root or root in path.parents:
        _secure_root(root)
    path.mkdir(parents=True, exist_ok=True)


def check_trusted(path: Path) -> None:
    """Raise :class:`UnsafeRuntimeDirError` before a reader trusts ``path``
    if it lies under a runtime root that is not this user's alone.

    Readers never create or chmod anything; a process that only reads (the
    web watcher, the TUI) must still refuse a root another user created and
    filled (#263 review). A missing root raises the ordinary
    ``FileNotFoundError``.
    """
    root = RUNTIME_ROOT
    if path != root and root not in path.parents:
        return
    info = os.lstat(root)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise UnsafeRuntimeDirError(f"{root} is not private to this user; ignoring its contents")
