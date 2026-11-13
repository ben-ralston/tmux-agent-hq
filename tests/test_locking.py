"""Tests for ``agent_hq._locking.file_lock``.

Cross-process exclusivity is verified by spawning a child process that
holds the lock and asserting the parent blocks until release. Single-
process retention is also tested with a thread to prove the contextmanager
plumbing is correct.
"""

import multiprocessing
import threading
import time

import pytest

from agent_hq._locking import file_lock


def _hold_lock_in_child(lock_path: str, ready_path: str, release_delay: float) -> None:
    """Child process: acquire lock, signal readiness, sleep, release."""
    from pathlib import Path

    from agent_hq._locking import file_lock as _file_lock

    with _file_lock(Path(lock_path)):
        Path(ready_path).touch()
        time.sleep(release_delay)


def test_file_lock_yields_and_releases(tmp_path):
    lock = tmp_path / "x.lock"
    with file_lock(lock):
        assert lock.exists()
    # Reacquiring after release succeeds.
    with file_lock(lock):
        pass


def test_file_lock_releases_on_exception(tmp_path):
    lock = tmp_path / "x.lock"
    with pytest.raises(RuntimeError):
        with file_lock(lock):
            raise RuntimeError("boom")
    # Lock must be released — reacquiring succeeds without blocking.
    acquired = threading.Event()

    def acquire():
        with file_lock(lock):
            acquired.set()

    t = threading.Thread(target=acquire)
    t.start()
    t.join(timeout=2.0)
    assert acquired.is_set(), "lock not released on exception"


def test_file_lock_creates_parent_directory(tmp_path):
    lock = tmp_path / "deep" / "nested" / "x.lock"
    with file_lock(lock):
        assert lock.parent.is_dir()


def test_file_lock_blocks_cross_process(tmp_path):
    """Second flock attempt blocks while the first process holds the lock.

    POSIX flock is process-scoped — the same fd in the same process can
    re-lock without blocking. Verify true cross-process exclusion by
    spawning a worker that holds the lock for a known duration and
    timing how long the parent waits.
    """
    lock = tmp_path / "x.lock"
    ready_marker = tmp_path / "ready.flag"
    hold_seconds = 0.6

    ctx = multiprocessing.get_context("fork")
    p = ctx.Process(
        target=_hold_lock_in_child,
        args=(str(lock), str(ready_marker), hold_seconds),
    )
    p.start()
    try:
        # Wait for child to confirm it has the lock. Bounded so a stuck
        # child doesn't hang the suite.
        deadline = time.monotonic() + 5.0
        while not ready_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_marker.exists(), "child never reported lock acquisition"

        # Parent now tries to acquire — must block until child releases.
        t0 = time.monotonic()
        with file_lock(lock):
            elapsed = time.monotonic() - t0
        # Allow some slack for fork/exec/sched, but elapsed must clearly
        # reflect waiting on the child (well under hold_seconds means we
        # got the lock prematurely, i.e. no cross-process exclusion).
        assert elapsed >= hold_seconds * 0.5, (
            f"parent acquired in {elapsed:.3f}s before child released after {hold_seconds}s"
        )
    finally:
        p.join(timeout=5.0)
        if p.is_alive():
            p.terminate()
            p.join(timeout=1.0)
        assert p.exitcode == 0, f"child exited with {p.exitcode}"


def test_file_lock_serializes_in_process_threads(tmp_path):
    """Two threads in the same process serialize through the lock."""
    lock = tmp_path / "x.lock"
    enter_order: list[int] = []
    exit_order: list[int] = []
    barrier = threading.Barrier(2)

    def worker(idx: int, hold: float):
        barrier.wait()
        with file_lock(lock):
            enter_order.append(idx)
            time.sleep(hold)
            exit_order.append(idx)

    t1 = threading.Thread(target=worker, args=(1, 0.2))
    t2 = threading.Thread(target=worker, args=(2, 0.0))
    t1.start()
    # Tiny stagger to make t1 more likely to enter first — both threads
    # are still trying to acquire concurrently, so the serialization
    # claim holds either way.
    time.sleep(0.05)
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    assert len(enter_order) == 2
    assert len(exit_order) == 2
    # Whichever thread entered first must also have exited before the
    # other one entered — serial regions don't interleave.
    first = enter_order[0]
    assert exit_order[0] == first
