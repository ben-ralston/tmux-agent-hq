"""Runtime snapshots expire independently of discovery and source success."""

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from agent_hq.models import Agent, AgentState, AgentType
from agent_hq.status_collector import RuntimeStatusCollector, apply_live_status
from agent_hq.status_types import LiveStatus

NOW = 100.0
NS = 100_000_000_000


def agent(kind=AgentType.CLAUDE, **kwargs):
    return Agent("%1", "test", 0, 0, kind, "/tmp", agent_pid=42,
                 claude_transcript_session_id="session" if kind is AgentType.CLAUDE else None,
                 **kwargs)


def status(source="claude", **kwargs):
    return replace(LiveStatus("running", "busy", "Running", "", source, "session", 42,
                              NOW, NS), **kwargs)


def receipt(**kwargs):
    return dict(version=1, pane_id="%1", frontend_pid=42, generation="generation",
                thread_id="session", socket_path="/tmp/fake.sock", created_at=90,
                transcript_path="/tmp/session.jsonl", **kwargs)


def snapshot(path, observations=None, **kwargs):
    data = dict(version=1, heartbeat_at=NOW, heartbeat_ns=NS,
                observations=observations if observations is not None else [
                    {"status": status().to_dict()}], source_errors={})
    data.update(kwargs)
    path.write_text(json.dumps(data))


def overlay(agents, path, **kwargs):
    return apply_live_status(agents, path=path, now=NOW, now_ns=NS,
                             registrations=kwargs.pop("registrations", []),
                             process_started_at=kwargs.pop("process_started_at", lambda pid: 90),
                             **kwargs)


def test_agent_status_round_trip_and_legacy_compatibility():
    item = agent(status_snapshot=status())
    copy = Agent.from_dict(item.to_dict())
    assert copy.agent_pid == 42 and copy.status_snapshot == status()
    assert (copy.status_label, copy.status_reason) == ("Running", "")
    data = item.to_dict()
    del data["agent_pid"], data["status_snapshot"]
    assert Agent.from_dict(data).agent_pid is None
    assert Agent.from_dict(data).status_snapshot is None


def test_fresh_status_overwrites_cached_state(tmp_path):
    path = tmp_path / "status.json"
    snapshot(path)
    item = agent(state=AgentState.WAITING)
    assert overlay([item], path) == [item]
    assert item.state is AgentState.RUNNING and item.status_snapshot == status()


@pytest.mark.parametrize("changes", [
    {"heartbeat_at": 98.49}, {"heartbeat_ns": NS - 1_500_000_001},
    {"heartbeat_at": 101}, {"heartbeat_ns": NS + 1}, {"version": 2},
    {"heartbeat_at": float("nan")}, {"heartbeat_ns": True},
    {"observations": {}},
])
def test_stale_corrupt_future_envelope_never_revives_cache(tmp_path, changes):
    path = tmp_path / "status.json"
    snapshot(path, **changes)
    item = agent(state=AgentState.RUNNING, status_snapshot=status())
    overlay([item], path)
    assert item.state is AgentState.UNKNOWN and item.status_reason


def test_missing_snapshot_drops_serialized_known_status(tmp_path):
    item = agent(state=AgentState.RUNNING, status_snapshot=status())
    overlay([item], tmp_path / "missing")
    assert item.state is AgentState.UNKNOWN


@pytest.mark.parametrize("changes", [
    {"session_id": "other"}, {"pid": 43}, {"observed_at": 98.49},
    {"observed_ns": NS + 1}, {"observed_at": 101}, {"source": "codex"},
])
def test_observation_must_match_and_be_fresh(tmp_path, changes):
    path = tmp_path / "status.json"
    snapshot(path, [{"status": status(**changes).to_dict()}])
    item = agent()
    overlay([item], path)
    assert item.state is AgentState.UNKNOWN


def test_duplicate_observations_are_unknown(tmp_path):
    path = tmp_path / "status.json"
    snapshot(path, [{"status": status().to_dict()}] * 2)
    item = agent()
    overlay([item], path)
    assert item.state is AgentState.UNKNOWN and "ambiguous" in item.status_reason.lower()


def test_reused_process_cannot_accept_previous_observation(tmp_path):
    path = tmp_path / "status.json"
    snapshot(path)
    item = agent()
    overlay([item], path, process_started_at=lambda pid: 100.1)
    assert item.state is AgentState.UNKNOWN


def codex_record(**kwargs):
    return dict(status=status("codex").to_dict(), pane_id="%1", generation="generation",
                socket_path="/tmp/fake.sock", **kwargs)


def test_codex_receipt_supplies_missing_thread_and_transcript(tmp_path):
    path = tmp_path / "status.json"
    snapshot(path, [codex_record()])
    item = agent(AgentType.CODEX)
    overlay([item], path, registrations=[receipt()])
    assert item.state is AgentState.RUNNING
    assert item.codex_thread_id == "session" and item.codex_rollout_path == "/tmp/session.jsonl"


@pytest.mark.parametrize("field,value", [
    ("generation", "other"), ("thread_id", "other"), ("frontend_pid", 43),
    ("pane_id", "%2"), ("socket_path", "/tmp/other.sock"),
])
def test_codex_observation_must_match_current_receipt(tmp_path, field, value):
    path = tmp_path / "status.json"
    snapshot(path, [codex_record()])
    registration = receipt()
    registration[field] = value
    item = agent(AgentType.CODEX)
    overlay([item], path, registrations=[registration])
    assert item.state is AgentState.UNKNOWN


def test_fresh_selection_replaces_cached_thread_without_waiting_for_discovery(tmp_path):
    path = tmp_path / "status.json"
    snapshot(path, [codex_record()])
    item = agent(AgentType.CODEX, codex_thread_id="other")
    overlay([item], path, registrations=[receipt()])
    assert item.state is AgentState.RUNNING and item.codex_thread_id == "session"


def test_duplicate_receipts_are_unknown(tmp_path):
    path = tmp_path / "status.json"
    snapshot(path, [codex_record()])
    item = agent(AgentType.CODEX)
    overlay([item], path, registrations=[receipt(), receipt()])
    assert item.state is AgentState.UNKNOWN


async def test_collector_replaces_failed_source_and_preserves_other(tmp_path):
    path = tmp_path / "status.json"
    fail = False

    def claude():
        if fail:
            raise OSError("disconnected")
        return [status()]

    async def codex(*args):
        return status("codex")

    collector = RuntimeStatusCollector(path, claude_reader=claude, codex_reader=codex,
                                       registrations_reader=lambda: [receipt()],
                                       clock=lambda: NOW, monotonic_ns=lambda: NS)
    await collector.collect_once()
    data = json.loads(path.read_text())
    assert len(data["observations"]) == 2
    fail = True
    await collector.collect_once()
    data = json.loads(path.read_text())
    assert all(row["status"]["source"] != "claude" for row in data["observations"])
    assert "claude" in data["source_errors"]
    item = agent()
    overlay([item], path)
    assert item.state is AgentState.UNKNOWN and "disconnected" in item.status_reason


async def test_fast_codex_publishes_before_slow_claude(tmp_path):
    path = tmp_path / "status.json"
    release = threading.Event()

    def claude():
        release.wait(1)
        return [status()]

    async def codex(*args):
        return status("codex")

    collector = RuntimeStatusCollector(path, claude_reader=claude, codex_reader=codex,
                                       registrations_reader=lambda: [receipt()],
                                       clock=lambda: NOW, monotonic_ns=lambda: NS)
    task = asyncio.create_task(collector.collect_once())
    try:
        for _ in range(100):
            await asyncio.sleep(.001)
            if path.exists() and json.loads(path.read_text())["observations"]:
                break
        assert not task.done()
        [row] = json.loads(path.read_text())["observations"]
        assert row["status"]["source"] == "codex"
    finally:
        release.set()
        await task


@pytest.mark.parametrize("birth", [float("nan"), float("inf"), "yesterday", -1])
def test_malformed_process_birth_is_unknown(tmp_path, birth):
    path = tmp_path / "status.json"
    snapshot(path)
    item = agent()
    overlay([item], path, process_started_at=lambda pid: birth)
    assert item.state is AgentState.UNKNOWN


def test_age_limit_is_inclusive(tmp_path):
    path = tmp_path / "status.json"
    snapshot(path, [{"status": status(observed_at=98.5,
                                       observed_ns=NS - 1_500_000_000).to_dict()}],
             heartbeat_at=98.5, heartbeat_ns=NS - 1_500_000_000)
    item = agent()
    overlay([item], path)
    assert item.state is AgentState.RUNNING


def test_collector_background_lifecycle_stops_and_invalidates(tmp_path):
    path = tmp_path / "status.json"
    queried = threading.Event()

    def claude():
        queried.set()
        return [status()]

    collector = RuntimeStatusCollector(path, interval=.02, claude_reader=claude,
                                       registrations_reader=lambda: [], clock=lambda: NOW,
                                       monotonic_ns=lambda: NS)
    collector.start()
    original = collector._thread
    collector.start()
    try:
        assert collector._thread is original and queried.wait(1)
    finally:
        collector.stop()
    assert not original.is_alive()
    data = json.loads(path.read_text())
    assert data["observations"] == [] and "stopped" in data["source_errors"]["claude"]


async def test_claude_polling_stops_without_sessions_and_resumes_on_discovery(tmp_path):
    from unittest.mock import Mock

    reader = Mock(return_value=[status()])
    path = tmp_path / 'status.json'
    collector = RuntimeStatusCollector(path, claude_reader=reader,
                                       registrations_reader=lambda: [])
    await collector._claude()
    assert len(json.loads(path.read_text())['observations']) == 1
    collector.set_claude_present(False)
    reader.reset_mock()
    for _ in range(3):
        await collector._claude()
    reader.assert_not_called()
    assert json.loads(path.read_text())['observations'] == []
    collector.set_claude_present(True)
    await collector._claude()
    reader.assert_called_once()
    assert len(json.loads(path.read_text())['observations']) == 1


def _waiting(at, **kwargs):
    return status(state="waiting", native_value="waiting", label="Waiting", reason="input needed",
                  observed_at=at, **kwargs)


def _collector(path, reader):
    async def no_codex(*args):
        raise AssertionError("no codex receipts")

    return RuntimeStatusCollector(path, claude_reader=reader, codex_reader=no_codex,
                                  registrations_reader=lambda: [], pid_alive=lambda pid: True,
                                  clock=lambda: NOW, monotonic_ns=lambda: NS)


def _published(path):
    return [LiveStatus.from_dict(row["status"])
            for row in json.loads(path.read_text())["observations"]]


async def test_waiting_since_is_published_and_reaches_the_overlay(tmp_path):
    path = tmp_path / "status.json"
    await _collector(path, lambda: [_waiting(NOW)]).collect_once()
    (published,) = _published(path)
    assert published.waiting_since == NOW
    item = agent()
    overlay([item], path)
    assert item.state is AgentState.WAITING
    assert item.status_snapshot.waiting_since == NOW


async def test_waiting_since_survives_a_daemon_restart(tmp_path):
    path = tmp_path / "status.json"
    await _collector(path, lambda: [_waiting(NOW)]).collect_once()
    await _collector(path, lambda: [_waiting(NOW + 3600)]).collect_once()
    assert _published(path)[0].waiting_since == NOW


async def test_waiting_since_clears_when_the_session_stops_waiting(tmp_path):
    path = tmp_path / "status.json"
    current = [_waiting(NOW)]
    collector = _collector(path, lambda: current)
    await collector.collect_once()
    current[:] = [status(observed_at=NOW + 10)]
    await collector.collect_once()
    assert _published(path)[0].waiting_since is None
    current[:] = [_waiting(NOW + 20)]
    await collector.collect_once()
    assert _published(path)[0].waiting_since == NOW + 20


async def test_a_failed_source_read_does_not_reset_the_wait(tmp_path):
    path = tmp_path / "status.json"
    fail = False

    def reader():
        if fail:
            raise OSError("disconnected")
        return [_waiting(NOW)]

    collector = _collector(path, reader)
    await collector.collect_once()
    fail = True
    await collector.collect_once()
    assert _published(path) == []
    fail = False
    await collector.collect_once()
    assert _published(path)[0].waiting_since == NOW


async def test_codex_waiting_gets_a_waiting_since(tmp_path):
    path = tmp_path / "status.json"

    async def codex(*args):
        return _waiting(NOW, source="codex")

    collector = RuntimeStatusCollector(path, claude_reader=lambda: [], codex_reader=codex,
                                       registrations_reader=lambda: [receipt()],
                                       pid_alive=lambda pid: True, clock=lambda: NOW,
                                       monotonic_ns=lambda: NS)
    await collector.collect_once()
    (published,) = _published(path)
    assert published.source == "codex"
    assert published.waiting_since == NOW
    item = agent(AgentType.CODEX)
    overlay([item], path, registrations=[receipt()])
    assert item.status_snapshot.waiting_since == NOW
