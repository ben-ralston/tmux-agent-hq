"""Tests for the vendored unified-logger transport (#231).

All network I/O is mocked at urllib.request.urlopen. The token/queue
path constants are already redirected into tmp by the isolation
fixture; tests write the token file themselves to enable the client.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from agent_hq import logclient


@pytest.fixture()
def token_file(monkeypatch, tmp_path):
    path = tmp_path / "logger-write.token"
    path.write_text("test-token\n")
    monkeypatch.setattr(logclient, "_DEFAULT_TOKEN_FILE", path)
    return path


@pytest.fixture()
def queue_file(monkeypatch, tmp_path):
    path = tmp_path / "queue.jsonl"
    monkeypatch.setattr(logclient, "_DEFAULT_QUEUE_FILE", path)
    return path


def _ok_response():
    response = MagicMock()
    response.status = 200
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *a: False
    return response


def test_disabled_without_token_drops_event(queue_file, monkeypatch, tmp_path):
    monkeypatch.setattr(logclient, "_DEFAULT_TOKEN_FILE", tmp_path / "missing")
    with patch("agent_hq.logclient.urllib.request.urlopen") as urlopen:
        assert logclient.log("agent-hq", "error", "boom") is False
    urlopen.assert_not_called()
    assert not queue_file.exists()  # dropped, NOT queued


def test_enabled_reflects_token_presence(token_file, tmp_path, monkeypatch):
    assert logclient.enabled() is True
    monkeypatch.setattr(logclient, "_DEFAULT_TOKEN_FILE", tmp_path / "missing")
    assert logclient.enabled() is False


@pytest.mark.parametrize("content", ["", "   \n", "\n\t\n"])
def test_empty_token_file_is_disabled_and_drops(content, queue_file, monkeypatch, tmp_path):
    """#237: a present-but-blank token would otherwise report enabled, queue
    every event to the cap and pay a doomed ~15 s replay on each log()."""
    blank = tmp_path / "blank.token"
    blank.write_text(content)
    monkeypatch.setattr(logclient, "_DEFAULT_TOKEN_FILE", blank)
    assert logclient.enabled() is False
    with patch("agent_hq.logclient.urllib.request.urlopen") as urlopen:
        assert logclient.log("agent-hq", "error", "boom") is False
        assert logclient.enqueue("agent-hq", "error", "boom") is False
    urlopen.assert_not_called()
    assert not queue_file.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_unreadable_token_file_is_disabled(monkeypatch, tmp_path):
    locked = tmp_path / "locked.token"
    locked.write_text("test-token\n")
    locked.chmod(0)
    monkeypatch.setattr(logclient, "_DEFAULT_TOKEN_FILE", locked)
    try:
        assert logclient.enabled() is False
    finally:
        locked.chmod(0o600)


def test_undecodable_token_on_delivery_queues_instead_of_dropping(token_file, queue_file):
    """Review #237: a token that turns undecodable between enabled() and the
    delivery read (non-atomic rotation) is a transient failure: queue it."""
    reads = {"n": 0}
    real = logclient._read_token

    def flaky(path):
        reads["n"] += 1
        if reads["n"] > 1:
            path.write_bytes(b"\xff\xfe\xfa")
        return real(path)

    with (
        patch.object(logclient, "_read_token", side_effect=flaky),
        patch("agent_hq.logclient.urllib.request.urlopen") as urlopen,
    ):
        assert logclient.log("agent-hq", "error", "boom") is False
    urlopen.assert_not_called()
    assert queue_file.exists() and "boom" in queue_file.read_text()


def test_undecodable_token_reads_as_oserror(tmp_path):
    bad = tmp_path / "bad.token"
    bad.write_bytes(b"\xff\xfe\xfa")
    with pytest.raises(OSError):
        logclient._read_token(bad)


def test_log_delivers_with_custom_user_agent(token_file, queue_file):
    with patch("agent_hq.logclient.urllib.request.urlopen", return_value=_ok_response()) as urlopen:
        assert logclient.log("agent-hq", "info", "daemon started", {"interval": 2.0}) is True
    request = urlopen.call_args[0][0]
    assert request.get_header("User-agent") == "claude-logger/1.0"
    assert request.get_header("Authorization") == "Bearer test-token"
    [event] = json.loads(request.data)
    assert event["app"] == "agent-hq"
    assert event["level"] == "info"
    assert event["json_extra"] == {"interval": 2.0}
    assert not queue_file.exists()


def test_transient_failure_queues_event(token_file, queue_file):
    with patch("agent_hq.logclient.urllib.request.urlopen", side_effect=OSError("net down")):
        assert logclient.log("agent-hq", "error", "dispatch failed") is False
    lines = queue_file.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "dispatch failed"


def test_replay_first_then_new_event(token_file, queue_file, monkeypatch):
    # Replay semantics, not the logger#9 failure cooldown (which would send
    # the second event alone — see test_cooldown_sends_only_new_event).
    monkeypatch.setenv("LOGGER_COOLDOWN_MS", "0")
    with patch("agent_hq.logclient.urllib.request.urlopen", side_effect=OSError("down")):
        logclient.log("agent-hq", "warn", "first")
    with patch("agent_hq.logclient.urllib.request.urlopen", return_value=_ok_response()) as urlopen:
        assert logclient.log("agent-hq", "warn", "second") is True
    [event_first, event_second] = json.loads(urlopen.call_args[0][0].data)
    assert event_first["message"] == "first"
    assert event_second["message"] == "second"
    assert not queue_file.exists()


def test_enqueue_writes_without_network(token_file, queue_file):
    with patch("agent_hq.logclient.urllib.request.urlopen") as urlopen:
        assert logclient.enqueue("agent-hq", "error", "hook state write failed") is True
    urlopen.assert_not_called()
    assert json.loads(queue_file.read_text())["message"] == "hook state write failed"


def test_enqueue_disabled_without_token(queue_file, monkeypatch, tmp_path):
    monkeypatch.setattr(logclient, "_DEFAULT_TOKEN_FILE", tmp_path / "missing")
    assert logclient.enqueue("agent-hq", "error", "x") is False
    assert not queue_file.exists()


def test_flush_drains_queue(token_file, queue_file):
    logclient.enqueue("agent-hq", "info", "queued one")
    logclient.enqueue("agent-hq", "info", "queued two")
    with patch("agent_hq.logclient.urllib.request.urlopen", return_value=_ok_response()) as urlopen:
        assert logclient.flush() is True
    messages = [e["message"] for e in json.loads(urlopen.call_args[0][0].data)]
    assert messages == ["queued one", "queued two"]
    assert not queue_file.exists()


def test_flush_empty_queue_is_noop_success(token_file, queue_file):
    with patch("agent_hq.logclient.urllib.request.urlopen") as urlopen:
        assert logclient.flush() is True
    urlopen.assert_not_called()


def test_queue_capped_drop_oldest(token_file, queue_file, monkeypatch):
    monkeypatch.setattr(logclient, "MAX_QUEUE_EVENTS", 3)
    for n in range(5):
        logclient.enqueue("agent-hq", "info", f"event {n}")
    messages = [json.loads(line)["message"] for line in queue_file.read_text().splitlines()]
    assert messages == ["event 2", "event 3", "event 4"]


def test_rejected_batch_quarantined(token_file, queue_file):
    import urllib.error

    error = urllib.error.HTTPError("url", 400, "bad", {}, None)
    with patch("agent_hq.logclient.urllib.request.urlopen", side_effect=error):
        assert logclient.log("agent-hq", "error", "malformed") is False
    sidecar = queue_file.with_name(queue_file.name + ".rejected")
    assert "malformed" in sidecar.read_text()
    assert not queue_file.exists()


def test_unexpected_post_exception_queues_event(token_file, queue_file):
    import http.client

    with patch(
        "agent_hq.logclient.urllib.request.urlopen",
        side_effect=http.client.HTTPException("truncated"),
    ):
        assert logclient.log("agent-hq", "error", "boom") is False
    lines = queue_file.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "boom"


def test_log_never_raises(token_file, queue_file):
    with patch("agent_hq.logclient.urllib.request.urlopen", side_effect=RuntimeError("bug")):
        assert logclient.log("agent-hq", "error", "boom") is False
    # RuntimeError is caught by _post_events' generic except (finding #1's
    # fix), so the event queues like any other transient failure instead of
    # being lost.
    assert json.loads(queue_file.read_text())["message"] == "boom"


def test_enqueue_does_not_block_on_contended_lock(token_file, queue_file):
    """enqueue() is the hook binary's hot path: it must never wait on the
    queue lock that log()/flush() can hold for ~15s while replaying against
    a down endpoint. On contention it drops the event instead of blocking.
    """
    import fcntl
    import time

    queue_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = queue_file.with_name(queue_file.name + ".lock")
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        assert logclient.enqueue("agent-hq", "error", "dropped") is False
        assert time.monotonic() - start < 1.0
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    assert not queue_file.exists()

    assert logclient.enqueue("agent-hq", "error", "queued") is True
    assert json.loads(queue_file.read_text())["message"] == "queued"


# --- logger#9 hardening: per-event quarantine, 413 halving, local repair of
# guaranteed rejects, queue cap on transient failure, failure cooldown. Same
# semantics as the logger project's reference clients, adapted to this fork's
# drop-oldest cap and enqueue()/flush() APIs.


def _ev(message):
    return logclient._build_event("agent-hq", "info", message)


def _seed(queue_file, events):
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    queue_file.write_text("".join(json.dumps(e) + "\n" for e in events))


def _messages(path):
    if not path.exists():
        return []
    return [json.loads(line)["message"] for line in path.read_text().splitlines()]


def _rejected(queue_file):
    return queue_file.with_name(queue_file.name + ".rejected")


class _Server:
    """urlopen side_effect: records each POSTed body and rejects any body
    carrying an event whose message starts with ``flag``."""

    def __init__(self, flag=None, code=400, error=None, fail=None):
        self.flag = flag
        self.code = code
        self.error = error
        self.fail = fail  # exception raised for every request when set
        self.bodies = []

    def sizes(self):
        return [len(b) for b in self.bodies]

    def __call__(self, request, timeout=None):
        import io
        import urllib.error

        body = json.loads(request.data)
        self.bodies.append(body)
        if self.fail is not None:
            raise self.fail
        for i, event in enumerate(body):
            if self.flag is not None and str(event["message"]).startswith(self.flag):
                text = self.error(i) if self.error else "rejected"
                payload = io.BytesIO(json.dumps({"error": text}).encode())
                raise urllib.error.HTTPError("url", self.code, "err", {}, payload)
        return _ok_response()


def _urlopen(server):
    return patch("agent_hq.logclient.urllib.request.urlopen", side_effect=server)


def _http_error(code):
    import urllib.error

    return urllib.error.HTTPError("url", code, "err", {}, None)


def test_400_naming_item_quarantines_only_offender(token_file, queue_file):
    _seed(queue_file, [_ev("q0"), _ev("bad"), _ev("q2")])
    server = _Server("bad", 400, lambda i: f"item {i}: level must be one of ...")
    with _urlopen(server):
        assert logclient.log("agent-hq", "info", "fresh") is True
    assert len(server.bodies) == 2
    assert [e["message"] for e in server.bodies[1]] == ["q0", "q2", "fresh"]
    assert _messages(_rejected(queue_file)) == ["bad"]
    assert not queue_file.exists()


def test_unattributed_reject_bisects_to_offender(token_file, queue_file):
    _seed(queue_file, [_ev("bad"), _ev("q1")])
    server = _Server("bad", 422)
    with _urlopen(server):
        assert logclient.log("agent-hq", "info", "fresh") is True
    assert server.sizes() == [3, 2, 1, 2]
    assert _messages(_rejected(queue_file)) == ["bad"]


def test_413_multi_event_chunk_halves_instead_of_wedging(token_file, queue_file):
    _seed(queue_file, [_ev("big 1"), _ev("big 2")])
    server = _Server("big", 413)
    with _urlopen(server):
        assert logclient.log("agent-hq", "info", "fresh") is True
    # Size resets to 100 after each isolated offender.
    assert server.sizes() == [3, 2, 1, 2, 1, 1]
    assert _messages(_rejected(queue_file)) == ["big 1", "big 2"]
    assert not queue_file.exists()


def test_halvings_have_own_budget_and_size_resets(token_file, queue_file):
    _seed(queue_file, [_ev("big")] + [_ev(f"q{i}") for i in range(99)])
    server = _Server("big", 413)
    with _urlopen(server):
        assert logclient.log("agent-hq", "info", "fresh") is True
    assert server.sizes() == [100, 50, 25, 13, 7, 4, 2, 1, 100]
    assert not queue_file.exists()


def test_spent_halving_budget_ends_call(token_file, queue_file):
    _seed(queue_file, [_ev("big"), _ev("big")] + [_ev(f"q{i}") for i in range(98)])
    server = _Server("big", 413)
    with _urlopen(server):
        assert logclient.log("agent-hq", "info", "first call") is False
    assert len(server.bodies) == 9
    assert len(_messages(queue_file)) == 100
    with _urlopen(server):
        assert logclient.log("agent-hq", "info", "second call") is True
    assert not queue_file.exists()
    assert len(_messages(_rejected(queue_file))) == 2


def test_flush_quarantines_only_offender(token_file, queue_file):
    _seed(queue_file, [_ev("q0"), _ev("bad"), _ev("q2")])
    server = _Server("bad", 400, lambda i: f"item {i}: message required")
    with _urlopen(server):
        assert logclient.flush() is True
    assert [e["message"] for e in server.bodies[-1]] == ["q0", "q2"]
    assert _messages(_rejected(queue_file)) == ["bad"]


def test_empty_message_gets_placeholder():
    assert _ev("")["message"] == "(empty message)"


def test_scalar_json_extra_wrapped():
    event = logclient._build_event("agent-hq", "info", "m", json_extra="text")
    assert event["json_extra"] == {"value": "text"}
    listed = logclient._build_event("agent-hq", "info", "m", json_extra=[1, 2])
    assert listed["json_extra"] == [1, 2]


def test_nan_json_extra_sanitized():
    # NaN would serialize to invalid JSON and fail the whole batch.
    event = logclient._build_event("agent-hq", "info", "m", json_extra={"x": float("nan")})
    assert "unserializable" in event["json_extra"]


def test_oversized_json_extra_truncated():
    huge = {"blob": '"\\' * 20_000}
    extra = logclient._build_event("agent-hq", "info", "m", json_extra=huge)["json_extra"]
    compact = json.dumps(extra, separators=(",", ":"), ensure_ascii=False)
    assert len(compact) <= 16384
    assert extra["truncated"] is True
    assert extra["original_length"] == len(
        json.dumps(huge, separators=(",", ":"), ensure_ascii=False)
    )
    assert isinstance(extra["preview"], str)
    assert len(extra["preview"]) == 4096


def test_source_clipped_to_128():
    event = logclient._build_event("agent-hq", "info", "m", source="s" * 500)
    assert event["source"] == "s" * 128


def test_clipping_counts_utf16_units_and_keeps_pairs():
    # The Worker measures JS .length (UTF-16 units): an emoji counts 2.
    emoji = "\U0001f600"
    assert _ev("a" * 8191 + emoji)["message"] == "a" * 8191
    assert _ev(emoji * 5000)["message"] == emoji * 4096


def test_enqueue_applies_build_time_repair(token_file, queue_file):
    assert logclient.enqueue("agent-hq", "info", "", json_extra=7) is True
    [event] = [json.loads(line) for line in queue_file.read_text().splitlines()]
    assert event["message"] == "(empty message)"
    assert event["json_extra"] == {"value": 7}


def test_queue_cap_drops_oldest_on_transient_failure(token_file, queue_file, monkeypatch):
    monkeypatch.setattr(logclient, "MAX_QUEUE_EVENTS", 5)
    _seed(queue_file, [_ev(f"q{i}") for i in range(5)])
    with _urlopen(_Server(fail=_http_error(500))):
        assert logclient.log("agent-hq", "info", "fresh") is False
    assert _messages(queue_file) == ["q1", "q2", "q3", "q4", "fresh"]
    # Divergence 3 (drop-oldest) kept: overflow is dropped, not quarantined.
    assert not _rejected(queue_file).exists()


def test_no_overflow_drop_when_healthy(token_file, queue_file, monkeypatch):
    # Overflow a healthy server can still take is never dropped ahead of a
    # delivery attempt.
    monkeypatch.setattr(logclient, "MAX_QUEUE_EVENTS", 5)
    _seed(queue_file, [_ev(f"q{i}") for i in range(9)])
    server = _Server()
    with _urlopen(server):
        assert logclient.log("agent-hq", "info", "fresh") is True
    assert server.sizes() == [10]
    assert not queue_file.exists()
    assert not _rejected(queue_file).exists()


def test_flush_caps_queue_on_transient_failure(token_file, queue_file, monkeypatch):
    monkeypatch.setattr(logclient, "MAX_QUEUE_EVENTS", 3)
    _seed(queue_file, [_ev(f"q{i}") for i in range(5)])
    with _urlopen(_Server(fail=OSError("down"))):
        assert logclient.flush() is False
    assert _messages(queue_file) == ["q2", "q3", "q4"]


def test_cooldown_sends_only_new_event(token_file, queue_file, monkeypatch):
    server = _Server(fail=_http_error(401))
    with _urlopen(server):
        assert logclient.log("agent-hq", "info", "first") is False
        assert logclient.log("agent-hq", "info", "second") is False
        assert server.sizes() == [1, 1]  # no backlog replay
        assert _messages(queue_file) == ["first", "second"]

        monkeypatch.setenv("LOGGER_COOLDOWN_MS", "0")  # cooldown over
        assert logclient.log("agent-hq", "info", "third") is False
        assert server.sizes() == [1, 1, 3]


def test_cooldown_success_clears_it_and_next_call_replays(token_file, queue_file):
    with _urlopen(_Server(fail=_http_error(503))):
        assert logclient.log("agent-hq", "alert", "while down") is False
    server = _Server()
    with _urlopen(server):
        assert logclient.log("agent-hq", "alert", "back up") is True
        assert [e["message"] for e in server.bodies[0]] == ["back up"]
        assert _messages(queue_file) == ["while down"]
        assert logclient.log("agent-hq", "info", "next") is True
    assert [e["message"] for e in server.bodies[1]] == ["while down", "next"]
    assert not queue_file.exists()


def test_cooldown_rejected_event_quarantined(token_file, queue_file):
    with _urlopen(_Server(fail=OSError("down"))):
        logclient.log("agent-hq", "info", "opens cooldown")
    with _urlopen(_Server("bad", 400)):
        assert logclient.log("agent-hq", "info", "bad one") is False
    assert _messages(_rejected(queue_file)) == ["bad one"]
    assert _messages(queue_file) == ["opens cooldown"]


def test_cap_enforced_through_cooldown(token_file, queue_file, monkeypatch):
    monkeypatch.setattr(logclient, "MAX_QUEUE_EVENTS", 5)
    _seed(queue_file, [_ev(f"q{i}") for i in range(4)])
    server = _Server(fail=OSError("refused"))
    with _urlopen(server):
        logclient.log("agent-hq", "info", "opens cooldown")  # full path: 5 queued
        for i in range(6):
            logclient.log("agent-hq", "info", f"c{i}")
    # 5 cooldown appends (O(1), no backlog read), then the 6th call takes the
    # full path and trims drop-oldest.
    assert server.sizes() == [5, 1, 1, 1, 1, 1, 11]
    assert len(_messages(queue_file)) == 5


def test_flush_ignores_cooldown(token_file, queue_file):
    # flush() is the daemon's deliberate backlog drain: it always replays,
    # and its success clears the cooldown for later log() calls.
    with _urlopen(_Server(fail=OSError("down"))):
        logclient.log("agent-hq", "info", "while down")
    server = _Server()
    with _urlopen(server):
        assert logclient.flush() is True
        assert logclient.log("agent-hq", "info", "after") is True
    assert [[e["message"] for e in b] for b in server.bodies] == [["while down"], ["after"]]
    assert not queue_file.exists()


def test_cooldown_cleared_by_permanent_reject(token_file, queue_file):
    # A 400/413 answer proves the server is reachable: replay resumes.
    with _urlopen(_Server(fail=_http_error(503))):
        assert logclient.log("agent-hq", "info", "while down") is False
    with _urlopen(_Server("rejected", 400, lambda i: f"item {i}: level must be one of ...")):
        assert logclient.log("agent-hq", "info", "rejected one") is False
    assert _messages(_rejected(queue_file)) == ["rejected one"]
    server = _Server()
    with _urlopen(server):
        assert logclient.log("agent-hq", "info", "next") is True
    assert [e["message"] for e in server.bodies[0]] == ["while down", "next"]


def test_full_path_reject_clears_cooldown(token_file, queue_file, monkeypatch):
    # flush() bypasses the cooldown; a permanent reject there also proves
    # the server reachable, so the next log() replays instead of going alone.
    with _urlopen(_Server(fail=OSError("down"))):
        logclient.log("agent-hq", "info", "bad while down")
    with _urlopen(_Server("bad", 413)):
        assert logclient.flush() is True
    assert _messages(_rejected(queue_file)) == ["bad while down"]
    _seed(queue_file, [_ev("queued")])
    server = _Server()
    with _urlopen(server):
        assert logclient.log("agent-hq", "info", "next") is True
    assert [e["message"] for e in server.bodies[0]] == ["queued", "next"]
