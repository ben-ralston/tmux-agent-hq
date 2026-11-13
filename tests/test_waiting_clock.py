"""WaitingClock (#415): when a session entered native waiting, across restarts."""

import json

import pytest

from agent_hq.status_types import LiveStatus, unknown_status
from agent_hq.waiting_clock import WaitingClock


def status(state="waiting", *, pid=42, session="sid", at=100.0, waiting_since=None):
    return LiveStatus(
        state,
        "native",
        state.title(),
        "",
        "claude",
        session,
        pid,
        at,
        1,
        waiting_since,
    )


@pytest.fixture
def path(tmp_path):
    return tmp_path / "waiting-since.json"


def clock(path, *, alive=lambda pid: True):
    return WaitingClock(path, alive=alive)


def test_entering_waiting_stamps_the_observation_time(path):
    assert clock(path).stamp(status(at=100.0)).waiting_since == 100.0


def test_a_continuing_wait_keeps_its_start(path):
    c = clock(path)
    c.stamp(status(at=100.0))
    assert c.stamp(status(at=500.0)).waiting_since == 100.0


def test_native_start_seeds_a_new_wait(path):
    """A wait the daemon was down for starts when the harness says it did."""
    assert clock(path).stamp(status(at=500.0, waiting_since=90.0)).waiting_since == 90.0


def test_a_moved_native_start_is_a_new_wait(path):
    """The owner approves, a fast tool runs, the next prompt appears: no poll saw busy."""
    c = clock(path)
    c.stamp(status(at=100.0, waiting_since=100.0))
    assert c.stamp(status(at=500.0, waiting_since=450.0)).waiting_since == 450.0
    c.save()
    assert clock(path).stamp(status(at=900.0)).waiting_since == 450.0


def test_a_native_start_corrects_an_observed_one(path):
    """The first read went through the CLI fallback, which has no stamp."""
    c = clock(path)
    c.stamp(status(at=100.0))
    assert c.stamp(status(at=500.0, waiting_since=90.0)).waiting_since == 90.0


def test_losing_the_native_start_keeps_the_wait(path):
    """The registry falls back to the CLI for a cycle."""
    c = clock(path)
    c.stamp(status(at=100.0, waiting_since=90.0))
    assert c.stamp(status(at=500.0)).waiting_since == 90.0


def test_a_steady_native_start_writes_nothing(path):
    c = clock(path)
    c.stamp(status(at=100.0, waiting_since=90.0))
    c.save()
    path.unlink()
    c.stamp(status(at=500.0, waiting_since=90.0))
    c.save()
    assert not path.exists()


@pytest.mark.parametrize("state", ["idle", "running"])
def test_leaving_waiting_starts_the_next_wait_fresh(path, state):
    c = clock(path)
    c.stamp(status(at=100.0))
    assert c.stamp(status(state, at=200.0)).waiting_since is None
    assert c.stamp(status(at=300.0)).waiting_since == 300.0


def test_unknown_says_nothing_about_the_wait(path):
    """A collector blip (ambiguous identity, source error) must not reset it."""
    c = clock(path)
    c.stamp(status(at=100.0))
    blip = unknown_status("blip", source="claude", session_id="sid", pid=42, observed_at=200.0)
    assert c.stamp(blip).waiting_since is None
    assert c.stamp(status(at=300.0)).waiting_since == 100.0


def test_a_status_without_identity_is_passed_through(path):
    c = clock(path)
    orphan = unknown_status("no identity")
    assert c.stamp(orphan) is orphan


def test_a_different_session_in_the_same_process_is_a_new_wait(path):
    """/clear and /resume change the session under the same pid."""
    c = clock(path)
    c.stamp(status(at=100.0, session="old"))
    assert c.stamp(status(at=200.0, session="new")).waiting_since == 200.0


def test_a_resumed_session_in_a_new_process_is_a_new_wait(path):
    c = clock(path)
    c.stamp(status(at=100.0, pid=42))
    assert c.stamp(status(at=200.0, pid=43)).waiting_since == 200.0


def test_a_wait_survives_a_restart(path):
    first = clock(path)
    first.stamp(status(at=100.0))
    first.save()
    assert clock(path).stamp(status(at=900.0)).waiting_since == 100.0


def test_a_cleared_wait_stays_cleared_after_a_restart(path):
    first = clock(path)
    first.stamp(status(at=100.0))
    first.save()
    first.stamp(status("idle", at=200.0))
    first.save()
    assert clock(path).stamp(status(at=900.0)).waiting_since == 900.0


def test_entries_of_dead_processes_are_dropped_and_saved(path):
    alive = {41, 42}
    c = clock(path, alive=lambda pid: pid in alive)
    c.stamp(status(pid=41, session="a", at=100.0))
    c.stamp(status(pid=42, session="b", at=100.0))
    c.save()
    alive.discard(41)
    c.save()
    assert set(json.loads(path.read_text())) == {"42"}


def test_save_writes_nothing_when_nothing_changed(path):
    c = clock(path)
    c.save()
    assert not path.exists()
    c.stamp(status(at=100.0))
    c.save()
    before = path.stat().st_mtime_ns
    path.unlink()
    c.stamp(status(at=200.0))
    c.save()
    assert not path.exists(), f"rewrote an unchanged clock (first write {before})"


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"abc": {"session_id": "sid", "since": 1.0}}',
        '{"42": "nope"}',
        '{"42": {"session_id": "", "since": 1.0}}',
        '{"42": {"session_id": "sid", "since": "soon"}}',
        '{"42": {"session_id": "sid", "since": true}}',
        '{"42": {"session_id": "sid", "since": -1}}',
        '{"-42": {"session_id": "sid", "since": 1.0}}',
    ],
)
def test_a_corrupt_file_starts_the_wait_over(path, content):
    path.write_text(content)
    assert clock(path).stamp(status(at=100.0)).waiting_since == 100.0


def test_a_future_start_on_disk_is_not_trusted(path):
    path.write_text(json.dumps({"42": {"session_id": "sid", "since": 9e12}}))
    assert clock(path).stamp(status(at=100.0)).waiting_since == 100.0


def test_a_huge_integer_on_disk_does_not_stop_the_daemon(path):
    """The clock is built before the poll loop's try: a raise here kills startup."""
    path.write_text('{"42": {"session_id": "sid", "since": 1' + "0" * 400 + "}}")
    assert clock(path).stamp(status(at=100.0)).waiting_since == 100.0


def test_a_failed_temp_cleanup_never_raises(path, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("nope")

    monkeypatch.setattr("agent_hq.waiting_clock.eventlog.emit", lambda *a, **k: None)
    monkeypatch.setattr("agent_hq.waiting_clock.os.replace", refuse)
    monkeypatch.setattr("agent_hq.waiting_clock.Path.unlink", refuse)
    c = clock(path)
    c.stamp(status(at=100.0))
    c.save()


def test_a_failed_save_never_raises_and_is_retried(path, tmp_path, monkeypatch):
    """The status snapshot must still publish when this file cannot be written."""
    emitted = []
    monkeypatch.setattr(
        "agent_hq.waiting_clock.eventlog.emit", lambda *a, **k: emitted.append(a[1])
    )
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("")
    c = WaitingClock(blocked / "waiting-since.json", alive=lambda pid: True)
    c.stamp(status(at=100.0))
    c.save()
    assert emitted == ["waiting clock save failed"]
    blocked.unlink()
    blocked.mkdir()
    c.save()
    assert (blocked / "waiting-since.json").exists()
