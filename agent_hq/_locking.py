"""POSIX file-lock helper shared by the overrides + hook-state writers.

``file_lock`` wraps ``fcntl.flock`` so any read-modify-write of a
JSON sidecar can be made cross-process exclusive. Used by:

* ``agent_hq.overrides`` to serialize ``write_override`` /
  ``delete_override`` / ``prune_stale_overrides`` against each other.
* ``agent_hq.hooks`` to serialize per-pane state writes (hook event,
  daemon prune tombstone, daemon orphan unlink) so the
  read-classify-tombstone cycle in ``prune_stale_hook_state_files``
  can't clobber a fresh write that landed between the two.

The lock file itself is just a sidecar — its contents are irrelevant
and it persists across runs. Only the ``flock`` acquisition is
exclusive. Lock files inherit the parent directory's perms; the
helper ``mkdir``s the parent if missing so first use can't trip on
``ENOENT`` against a fresh ``/tmp/agent-hq``.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path

from .runtime_dir import ensure_dir


@contextmanager
def file_lock(lock_path: Path):
    """Hold a cross-process exclusive flock on ``lock_path`` for the block.

    Opens the lock file in write mode (creating it on first acquire),
    takes ``LOCK_EX``, yields, then releases and closes. Releases on
    exception too — the ``try/finally`` is what makes this safe to use
    around code that can raise mid-mutation.
    """
    ensure_dir(lock_path.parent)
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


@contextmanager
def best_effort_file_lock(lock_path: Path):
    """``file_lock`` that degrades to *unlocked* when the lock cannot be taken.

    For coordination that is an optimization over correctness — a shared
    cache whose worst uncoordinated outcome is a redundant fetch or a
    clobbered peer entry that the next refresh repairs. ``main``'s cache
    writer swallowed every OSError, so an unwritable cache directory
    degraded to in-memory; locks added outside that handler turned the same
    environment into a crash in the TUI's refresh worker (#255 review).
    Yields ``True`` when the lock is held, ``False`` when it is not.
    """
    try:
        ensure_dir(lock_path.parent)
        f = open(lock_path, "w")
    except OSError:
        yield False
        return
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield True
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()
