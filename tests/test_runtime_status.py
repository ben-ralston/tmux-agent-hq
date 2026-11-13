"""Native runtime queries: identity, fail-closed mapping, and read-only protocol."""

import asyncio
import calendar
import json
import subprocess
import time
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest

from agent_hq.runtime_status import (
    map_codex_status,
    parse_claude_statuses,
    read_claude_statuses,
    read_codex_status,
)
from agent_hq.status_types import LiveStatus, unknown_status

THREAD = "01996500-1234-7123-8123-123456789abc"
OTHER = "01996500-1234-7123-8123-123456789abd"


def test_status_round_trip_and_immutability():
    status = LiveStatus("running", "busy", "Running", "", "claude", THREAD, 42, 10.5, 90)
    assert LiveStatus.from_dict(status.to_dict()) == status
    with pytest.raises(FrozenInstanceError):
        status.state = "idle"
    assert unknown_status("Disconnected").reason == "Disconnected"


@pytest.mark.parametrize("payload", [None, [], {}, {"state": "running"}])
def test_invalid_serialization_is_unknown(payload):
    assert LiveStatus.from_dict(payload).state == "unknown"


@pytest.mark.parametrize("field,value", [
    ("state", "future"), ("state", []), ("source", "future"), ("source", {}),
    ("pid", True), ("pid", -1), ("session_id", []), ("label", 1),
    ("observed_at", float("nan")), ("observed_at", 10 ** 1000),
    ("observed_at", float("inf")), ("observed_at", True),
    ("observed_ns", -1), ("observed_ns", True),
])
def test_corrupt_fields_cannot_revive_known_state(field, value):
    data = LiveStatus("idle", "idle", "Idle", "", "claude", THREAD, 42, 10.5, 90).to_dict()
    data[field] = value
    assert LiveStatus.from_dict(data).state == "unknown"


@pytest.mark.parametrize("native,state", [("busy", "running"), ("waiting", "waiting"),
                                          ("idle", "idle"), ("future", "unknown")])
def test_claude_native_mapping(native, state):
    [status] = parse_claude_statuses([
        {"sessionId": THREAD, "pid": 42, "status": native, "waitingFor": "approval"},
    ])
    assert (status.state, status.native_value, status.source) == (state, native, "claude")
    assert (status.session_id, status.pid) == (THREAD, 42)
    assert status.observed_at > 0 and status.observed_ns > 0
    if state == "waiting":
        assert "approval" in status.reason
    if state == "unknown":
        assert status.reason


@pytest.mark.parametrize("entry", [None, [], {}, {"state": "done", "sessionId": THREAD},
    {"status": "busy", "sessionId": THREAD},
    {"status": "busy", "sessionId": THREAD, "pid": True},
    {"status": "busy", "sessionId": THREAD, "pid": 0},
    {"status": "busy", "sessionId": "", "pid": 42},
    {"status": [], "sessionId": THREAD, "pid": 42},
])
def test_claude_skips_background_and_malformed_records(entry):
    assert parse_claude_statuses([entry]) == []


@pytest.mark.parametrize("second", [
    {"sessionId": THREAD, "pid": 42, "status": "idle"},
    {"sessionId": OTHER, "pid": 42, "status": "idle"},
    {"sessionId": THREAD, "pid": 43, "status": "idle"},
])
def test_claude_duplicate_identity_is_never_usable(second):
    statuses = parse_claude_statuses([
        {"sessionId": THREAD, "pid": 42, "status": "busy"}, second,
    ])
    assert statuses and all(status.state == "unknown" for status in statuses)
    assert all("ambiguous" in status.reason.lower() for status in statuses)


def test_malformed_duplicate_cannot_make_claude_identity_look_unique():
    [status] = parse_claude_statuses([
        {"sessionId": THREAD, "pid": 42, "status": "busy"},
        {"sessionId": OTHER, "pid": 42, "status": {}},
    ])
    assert status.state == "unknown"


def test_claude_read_command_is_bounded_and_read_only():
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "[]", ""))
    assert read_claude_statuses(runner=runner) == []
    args, kwargs = runner.call_args
    assert args[0] == ["claude", "agents", "--settings", '{"disableAgentView":false}', "--json"]
    assert 0 < kwargs["timeout"] <= 0.75
    assert kwargs["check"] and kwargs["capture_output"] and kwargs["text"]


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("claude", .75),
    FileNotFoundError("claude"), subprocess.CalledProcessError(1, "claude")])
def test_claude_source_failures_propagate(failure):
    with pytest.raises(type(failure)):
        read_claude_statuses(runner=Mock(side_effect=failure))


@pytest.mark.parametrize("output", ["not json", "{}", "null"])
def test_claude_malformed_envelope_fails_source(output):
    with pytest.raises(ValueError):
        read_claude_statuses(runner=Mock(return_value=subprocess.CompletedProcess([], 0, output)))


@pytest.mark.parametrize("native,state", [
    ({"type": "active", "activeFlags": []}, "running"),
    ({"type": "active", "activeFlags": ["waitingOnApproval"]}, "waiting"),
    ({"type": "active", "activeFlags": ["waitingOnUserInput"]}, "waiting"),
    ({"type": "active", "activeFlags": ["waitingOnApproval", "waitingOnUserInput"]}, "waiting"),
    ({"type": "idle"}, "idle"), ({"type": "notLoaded"}, "unknown"),
    ({"type": "systemError"}, "unknown"), ({"type": "future"}, "unknown"),
    ({"type": "active", "activeFlags": ["future", "waitingOnApproval"]}, "unknown"),
    ({"type": "active", "activeFlags": "waitingOnApproval"}, "unknown"),
    ({"type": "active", "activeFlags": [{}]}, "unknown"),
    ({"type": "active"}, "unknown"), ({"type": []}, "unknown"), (None, "unknown"),
])
def test_codex_native_mapping(native, state):
    status = map_codex_status(native, THREAD, 42)
    assert status.state == state
    assert (status.source, status.session_id, status.pid) == ("codex", THREAD, 42)
    if state in ("waiting", "unknown"):
        assert status.reason


class FakeSocket:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        reply = next(self.replies, None)
        if reply is None:
            await asyncio.Future()
        return reply if isinstance(reply, str) else json.dumps(reply)


def socket_for(thread=None, extras=()):
    return FakeSocket([
        {"id": 1, "result": {"userAgent": "codex-test"}}, *extras,
        {"id": 2, "result": {"thread": thread or {"id": THREAD, "status": {"type": "idle"}}}},
    ])


async def test_codex_query_correlates_and_never_starts_or_resumes():
    socket = socket_for(extras=[
        {"method": "thread/status/changed", "params": {}},
        {"id": 88, "result": {"thread": {"id": OTHER}}},
        {"id": 91, "method": "item/commandExecution/requestApproval", "params": {}},
    ])
    connect = Mock(return_value=socket)
    status = await read_codex_status("/tmp/fake.sock", THREAD, 42, connect=connect)
    assert status.state == "idle" and socket.closed
    assert [message["method"] for message in socket.sent] == [
        "initialize", "initialized", "thread/read",
    ]
    assert socket.sent[2]["params"] == {"threadId": THREAD, "includeTurns": False}
    assert connect.call_args.args[0] == "/tmp/fake.sock"
    assert connect.call_args.kwargs["open_timeout"] <= .75
    assert connect.call_args.kwargs["compression"] is None
    assert connect.call_args.kwargs["uri"] == "ws://localhost/rpc"


async def test_codex_foreign_thread_is_unknown():
    socket = socket_for({"id": OTHER, "status": {"type": "active", "activeFlags": []}})
    status = await read_codex_status("/tmp/fake.sock", THREAD, 42, connect=lambda *a, **k: socket)
    assert status.state == "unknown" and "identity" in status.reason.lower()


@pytest.mark.parametrize("reply", ["bad json", [], {"id": 1, "error": {"code": -1}},
    {"id": 1}, {"id": 1, "result": []}])
async def test_codex_malformed_protocol_propagates(reply):
    socket = FakeSocket([reply])
    with pytest.raises(ValueError):
        await read_codex_status("/tmp/fake.sock", THREAD, 42, connect=lambda *a, **k: socket)
    assert socket.closed


async def test_codex_timeout_covers_whole_exchange():
    socket = FakeSocket([])
    with pytest.raises(asyncio.TimeoutError):
        await read_codex_status("/tmp/fake.sock", THREAD, 42,
                               connect=lambda *a, **k: socket, timeout=.01)
    assert socket.closed


@pytest.mark.parametrize("result", [{}, {"thread": []}])
async def test_codex_malformed_thread_result_fails_source(result):
    socket = FakeSocket([{"id": 1, "result": {}}, {"id": 2, "result": result}])
    with pytest.raises(ValueError):
        await read_codex_status("/tmp/fake.sock", THREAD, 42, connect=lambda *a, **k: socket)
    assert socket.closed


async def test_codex_read_rejection_fails_source():
    socket = FakeSocket([
        {"id": 1, "result": {}}, {"id": 2, "error": {"message": "not found"}},
    ])
    with pytest.raises(ValueError):
        await read_codex_status("/tmp/fake.sock", THREAD, 42, connect=lambda *a, **k: socket)


async def test_codex_wrong_response_id_does_not_supply_status():
    socket = FakeSocket([{"id": 1, "result": {}},
                         {"id": 3, "result": {"thread": {"id": THREAD,
                                                          "status": {"type": "idle"}}}}])
    with pytest.raises(asyncio.TimeoutError):
        await read_codex_status("/tmp/fake.sock", THREAD, 42,
                               connect=lambda *a, **k: socket, timeout=.01)


# --- #289: the ~/.claude/sessions registry is the primary Claude source ---

from agent_hq import runtime_status  # noqa: E402
from agent_hq.runtime_status import RegistryUnavailable, read_claude_registry  # noqa: E402


def _record(pid, session, status="idle", **extra):
    return {"pid": pid, "sessionId": session, "status": status, "kind": "interactive",
            "peerProtocol": 1, "waitingFor": None, **extra}


def _write(directory, *records):
    directory.mkdir(parents=True, exist_ok=True)
    for record in records:
        (directory / f"{record['pid']}.json").write_text(json.dumps(record))


def _never_run():
    return Mock(side_effect=AssertionError("claude agents must not run"))


def _alive(*pids):
    return lambda pid: pid in pids


def test_registry_maps_statuses_without_running_the_cli(tmp_path):
    _write(tmp_path, _record(41, THREAD, "busy"),
           _record(42, OTHER, "waiting", waitingFor="input needed"),
           _record(43, "s-43", "idle"))
    statuses = read_claude_statuses(runner=_never_run(), sessions_dir=tmp_path,
                                    alive=_alive(41, 42, 43))
    by_pid = {s.pid: s for s in statuses}
    assert by_pid[41].state == "running"
    assert (by_pid[42].state, by_pid[42].reason) == ("waiting", "input needed")
    assert by_pid[43].state == "idle"
    assert by_pid[41].session_id == THREAD and by_pid[41].source == "claude"


def test_registry_skips_dead_pids_and_key_files(tmp_path):
    _write(tmp_path, _record(41, THREAD, "busy"), _record(99, OTHER, "busy"))
    (tmp_path / "41.abc123.key").write_text("secret-token")
    records = read_claude_registry(tmp_path, alive=_alive(41))
    assert [r["pid"] for r in records] == [41]


def test_dead_record_with_unknown_protocol_does_not_force_fallback(tmp_path):
    _write(tmp_path, _record(41, THREAD), _record(99, OTHER, peerProtocol=7))
    assert [r["pid"] for r in read_claude_registry(tmp_path, alive=_alive(41))] == [41]


def _missing_dir(path):
    return path / "absent"


def _malformed(path):
    _write(path, _record(41, THREAD))
    (path / "42.json").write_text('{"pid": 42, "sess')
    return path


def _protocol(value):
    def build(path):
        record = _record(41, THREAD)
        if value is None:
            del record["peerProtocol"]
        else:
            record["peerProtocol"] = value
        _write(path, record)
        return path
    return build


def _no_status(path):
    record = _record(41, THREAD)
    del record["status"]
    _write(path, record)
    return path


def _not_a_dict(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "41.json").write_text("[1, 2]")
    return path


@pytest.mark.parametrize("build", [_missing_dir, _malformed, _protocol(2), _protocol(None),
                                   _protocol("1"), _no_status, _not_a_dict])
def test_unusable_registry_raises(tmp_path, build):
    with pytest.raises(RegistryUnavailable):
        read_claude_registry(build(tmp_path), alive=_alive(41, 42))


@pytest.mark.parametrize("build", [_missing_dir, _malformed, _protocol(2), _no_status])
def test_unusable_registry_falls_back_to_cli(tmp_path, monkeypatch, build):
    emitted = []
    monkeypatch.setattr(runtime_status.eventlog, "emit",
                        lambda *args, **kwargs: emitted.append(args))
    payload = json.dumps([{"pid": 41, "sessionId": THREAD, "status": "busy"}])
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, payload, ""))
    [status] = read_claude_statuses(runner=runner, sessions_dir=build(tmp_path),
                                    alive=_alive(41, 42))
    assert runner.called and status.state == "running"
    [(level, message, extra)] = emitted
    assert level == "warn" and message == "claude status registry unavailable, using cli"
    assert extra["reason"]


def test_registry_and_cli_agree_on_the_same_records(tmp_path):
    records = [_record(41, THREAD, "busy"),
               _record(42, OTHER, "waiting", waitingFor="permission prompt"),
               _record(43, "s-43", "idle"), _record(44, "s-44", "waiting")]
    _write(tmp_path, *records)
    from_registry = read_claude_statuses(runner=_never_run(), sessions_dir=tmp_path,
                                         alive=_alive(41, 42, 43, 44))
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(records), ""))
    from_cli = read_claude_statuses(runner=runner, sessions_dir=tmp_path / "absent")

    def shape(statuses):
        return sorted((s.pid, s.session_id, s.state, s.native_value, s.label, s.reason)
                      for s in statuses)

    assert shape(from_registry) == shape(from_cli)


def test_default_sessions_dir_is_the_redirected_constant(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_status, "CLAUDE_SESSIONS_DIR", tmp_path)
    _write(tmp_path, _record(41, THREAD, "busy"))
    [status] = read_claude_statuses(runner=_never_run(), alive=_alive(41))
    assert status.state == "running"


# --- #289 review round 1 ---

def test_unlistable_directory_raises(tmp_path, monkeypatch):
    _write(tmp_path, _record(41, THREAD))

    def denied(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(runtime_status.os, "scandir", denied)
    with pytest.raises(RegistryUnavailable):
        read_claude_registry(tmp_path, alive=_alive(41))


def test_dead_sessions_corrupt_file_does_not_force_fallback(tmp_path):
    _write(tmp_path, _record(41, THREAD))
    (tmp_path / "99.json").write_text('{"pid": 99, "sess')
    (tmp_path / "98.json").write_text("")
    assert [r["pid"] for r in read_claude_registry(tmp_path, alive=_alive(41))] == [41]


def test_live_sessions_corrupt_file_still_forces_fallback(tmp_path):
    (tmp_path / "41.json").write_text('{"pid": 41, "sess')
    with pytest.raises(RegistryUnavailable):
        read_claude_registry(tmp_path, alive=_alive(41))


def test_record_pid_must_match_its_filename(tmp_path):
    tmp_path.joinpath("41.json").write_text(json.dumps(_record(42, THREAD)))
    with pytest.raises(RegistryUnavailable):
        read_claude_registry(tmp_path, alive=_alive(41, 42))


STALE_START = "Thu Oct  1 12:06:07 2026"
LIVE_START = "Sat Oct  3 09:00:00 2026"


def _epoch(text):
    return calendar.timegm(time.strptime(text, "%a %b %d %H:%M:%S %Y"))


def test_reused_pid_record_is_dropped_when_it_shadows_a_resumed_session(tmp_path):
    # pid 41 crashed while running THREAD; THREAD was resumed as pid 42; an
    # unrelated process later took pid 41.
    _write(tmp_path, _record(41, THREAD, "busy", procStart=STALE_START),
           _record(42, THREAD, "idle", procStart=LIVE_START))
    births = {41: _epoch(LIVE_START) + 3600, 42: _epoch(LIVE_START) + 1}
    records = read_claude_registry(tmp_path, alive=_alive(41, 42), births=lambda: births)
    assert [r["pid"] for r in records] == [42]


def test_unverifiable_duplicate_session_stays_ambiguous(tmp_path):
    _write(tmp_path, _record(41, THREAD, procStart=STALE_START),
           _record(42, THREAD, procStart=LIVE_START))
    records = read_claude_registry(tmp_path, alive=_alive(41, 42), births=dict)
    assert sorted(r["pid"] for r in records) == [41, 42]
    assert {s.state for s in parse_claude_statuses(records)} == {"unknown"}


def test_births_are_only_read_for_duplicate_sessions(tmp_path):
    _write(tmp_path, _record(41, THREAD), _record(42, OTHER))
    births = Mock(side_effect=AssertionError("ps must not run"))
    assert len(read_claude_registry(tmp_path, alive=_alive(41, 42), births=births)) == 2


def test_proc_start_is_parsed_as_utc():
    # Registry procStart is UTC; a local-time parse is off by the UTC offset.
    assert runtime_status._proc_start({"procStart": "Thu Oct  1 12:06:07 2026"}) == 1790856367


# --- #289 review round 2 ---

@pytest.mark.parametrize("session", [["a"], {"a": 1}, "", None, 7])
def test_live_record_with_malformed_session_id_raises(tmp_path, session):
    _write(tmp_path, _record(41, session), _record(42, OTHER))
    with pytest.raises(RegistryUnavailable):
        read_claude_registry(tmp_path, alive=_alive(41, 42))


def test_oversized_pid_filename_is_skipped_not_raised(tmp_path):
    # Real liveness check: os.kill raises OverflowError past a C int.
    (tmp_path / f"{10 ** 30}.json").write_text("{}")
    assert read_claude_registry(tmp_path) == []


def _waiting_entry(**extra):
    return dict(pid=42, sessionId=THREAD, status="waiting", waitingFor="input needed", **extra)


def test_claude_waiting_carries_the_registry_status_time():
    """#415: ``statusUpdatedAt`` is epoch milliseconds, undocumented."""
    (status,) = parse_claude_statuses([_waiting_entry(statusUpdatedAt=1_000_000_500)])
    assert status.state == "waiting"
    assert status.waiting_since == 1_000_000.5


@pytest.mark.parametrize(
    "value", [None, "soon", True, -5, 0, float("inf"), 10**30, 10**400, 4_102_444_800_000_000]
)
def test_claude_unusable_status_time_is_ignored(value):
    (status,) = parse_claude_statuses([_waiting_entry(statusUpdatedAt=value)])
    assert status.state == "waiting"
    assert status.waiting_since is None


def test_claude_without_status_time_has_no_native_start():
    """The `claude agents --json` fallback omits the field."""
    (status,) = parse_claude_statuses([_waiting_entry()])
    assert status.waiting_since is None


def test_claude_non_waiting_statuses_have_no_waiting_since():
    (status,) = parse_claude_statuses(
        [dict(pid=42, sessionId=THREAD, status="idle", statusUpdatedAt=1_000_000_500)]
    )
    assert status.waiting_since is None


def test_waiting_since_round_trips_and_defaults_for_old_snapshots():
    status = LiveStatus("waiting", "waiting", "Waiting", "", "claude", THREAD, 42, 10.5, 90, 7.5)
    assert LiveStatus.from_dict(status.to_dict()) == status
    data = status.to_dict()
    del data["waiting_since"]
    assert LiveStatus.from_dict(data).state == "waiting"
    assert LiveStatus.from_dict(data).waiting_since is None


@pytest.mark.parametrize("value", ["soon", True, -1, 11.0, float("inf"), float("nan"), 10**400])
def test_a_bad_waiting_since_drops_the_field_not_the_observation(value):
    status = LiveStatus("waiting", "waiting", "Waiting", "", "claude", THREAD, 42, 10.5, 90)
    parsed = LiveStatus.from_dict({**status.to_dict(), "waiting_since": value})
    assert parsed.state == "waiting"
    assert parsed.waiting_since is None


def test_waiting_since_only_applies_to_a_waiting_observation():
    status = LiveStatus("running", "busy", "Running", "", "claude", THREAD, 42, 10.5, 90)
    parsed = LiveStatus.from_dict({**status.to_dict(), "waiting_since": 7.5})
    assert parsed.waiting_since is None
