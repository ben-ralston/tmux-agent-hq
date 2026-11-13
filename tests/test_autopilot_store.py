import json

import pytest

from agent_hq.autopilot_store import (
    MAX_RUNS,
    Attempt,
    AutopilotStore,
    Outcome,
    OutcomeUpdate,
    ReserveResult,
    RunRecord,
)

KEY = "ben-ralston/agent-hq#900"


def _attempt(key=KEY, attempt_id="a1", dispatched_at="2026-10-08T01:00:00+00:00"):
    return Attempt(
        issue_key=key,
        repo="ben-ralston/agent-hq",
        branch="feature/900-x",
        run_id="r1",
        dispatched_at=dispatched_at,
        outcome=Outcome.LAUNCHING,
        attempt_id=attempt_id,
    )


def test_empty_snapshot(tmp_path):
    s = AutopilotStore(tmp_path).snapshot()
    assert s.paused is False and s.attempts == {} and s.runs == []
    assert s.blocked_keys == set() and s.in_flight == 0


def test_reserve_then_finish(tmp_path):
    store = AutopilotStore(tmp_path)
    assert store.reserve(_attempt(), max_open=3) is ReserveResult.OK
    assert store.snapshot().attempts[KEY].outcome is Outcome.LAUNCHING
    assert store.snapshot().in_flight == 1
    assert (
        store.finish_launch(
            KEY,
            "a1",
            outcome=Outcome.RUNNING,
            session_name="s",
            worktree_path="/wt",
            detail=None,
            now="t2",
        )
        is True
    )
    a = store.snapshot().attempts[KEY]
    assert a.outcome is Outcome.RUNNING and a.session_name == "s" and a.updated_at == "t2"


def test_finish_launch_refuses_wrong_id_or_state(tmp_path):
    store = AutopilotStore(tmp_path)
    store.reserve(_attempt(), max_open=3)
    assert (
        store.finish_launch(
            KEY,
            "other",
            outcome=Outcome.RUNNING,
            session_name=None,
            worktree_path=None,
            detail=None,
            now="t",
        )
        is False
    )
    store.finish_launch(
        KEY,
        "a1",
        outcome=Outcome.RUNNING,
        session_name=None,
        worktree_path=None,
        detail=None,
        now="t",
    )
    assert (
        store.finish_launch(
            KEY,
            "a1",
            outcome=Outcome.LAUNCH_FAILED,
            session_name=None,
            worktree_path=None,
            detail=None,
            now="t",
        )
        is False
    )


def test_reserve_checks_pause_exists_and_capacity_atomically(tmp_path):
    store = AutopilotStore(tmp_path)
    store.set_paused(True)
    assert store.reserve(_attempt(), max_open=3) is ReserveResult.PAUSED
    store.set_paused(False)
    assert store.reserve(_attempt(), max_open=3) is ReserveResult.OK
    assert store.reserve(_attempt(attempt_id="a2"), max_open=3) is ReserveResult.EXISTS
    assert store.reserve(_attempt(key="x/y#1", attempt_id="a3"), max_open=1) is ReserveResult.FULL


def test_malformed_attempts_block_and_count(tmp_path):
    store = AutopilotStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"paused": False, "runs": [], "attempts": {"x/y#5": {"outcome": "weird"}}})
    )
    s = store.snapshot()
    assert "x/y#5" in s.blocked_keys and s.in_flight == 1
    assert store.reserve(_attempt(key="x/y#5"), max_open=3) is ReserveResult.EXISTS
    assert store.reserve(_attempt(), max_open=1) is ReserveResult.FULL
    store.reserve(_attempt(), max_open=3)
    assert json.loads(store.path.read_text())["attempts"]["x/y#5"] == {"outcome": "weird"}


_CORRUPT_PAYLOADS = [
    b"{not json",
    b"[]",
    json.dumps({"attempts": [], "runs": []}).encode(),
    json.dumps({"attempts": {}, "runs": {}}).encode(),
    b"\xff\xfe{}",
]


@pytest.mark.parametrize("payload", _CORRUPT_PAYLOADS)
def test_corrupt_file_write_raises(tmp_path, payload):
    store = AutopilotStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(payload)
    with pytest.raises(OSError):
        store.assert_readable()
    with pytest.raises(OSError):
        store.reserve(_attempt(), max_open=3)
    assert store.path.read_bytes() == payload


def test_non_utf8_file_snapshot_degrades_to_empty(tmp_path):
    store = AutopilotStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b"\xff\xfe{}")
    s = store.snapshot()
    assert s.paused is False and s.attempts == {} and s.runs == [] and s.unparsed == {}


@pytest.mark.parametrize("value", ["true", 1, None, "yes"])
def test_non_bool_paused_fails_closed(tmp_path, value):
    store = AutopilotStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"paused": value, "attempts": {}, "runs": []}))
    assert store.snapshot().paused is True
    assert store.reserve(_attempt(), max_open=3) is ReserveResult.PAUSED


def test_absent_paused_key_is_unpaused(tmp_path):
    store = AutopilotStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"attempts": {}, "runs": []}))
    assert store.snapshot().paused is False
    assert store.reserve(_attempt(), max_open=3) is ReserveResult.OK


def test_attempt_under_mismatched_key_is_unparsed_and_blocks(tmp_path):
    store = AutopilotStore(tmp_path)
    store.reserve(_attempt(), max_open=3)
    raw = json.loads(store.path.read_text())
    raw["attempts"] = {"x/y#7": raw["attempts"][KEY]}
    store.path.write_text(json.dumps(raw))
    s = store.snapshot()
    assert "x/y#7" in s.unparsed and s.attempts == {}
    assert "x/y#7" in s.blocked_keys and s.in_flight == 1
    assert store.reserve(_attempt(key="x/y#7", attempt_id="a9"), max_open=3) is ReserveResult.EXISTS


def test_apply_outcomes_compare_and_set(tmp_path):
    store = AutopilotStore(tmp_path)
    store.reserve(_attempt(), max_open=3)
    store.finish_launch(
        KEY,
        "a1",
        outcome=Outcome.RUNNING,
        session_name="s",
        worktree_path=None,
        detail=None,
        now="t1",
    )
    stale = OutcomeUpdate("a1", Outcome.LAUNCHING, Outcome.ENDED_WITHOUT_PR)
    assert store.apply_outcomes({KEY: stale}, now="t2") == 0
    ok = OutcomeUpdate("a1", Outcome.RUNNING, Outcome.PR_OPEN, pr_url="u")
    assert store.apply_outcomes({KEY: ok}, now="t3") == 1
    a = store.snapshot().attempts[KEY]
    assert a.outcome is Outcome.PR_OPEN and a.pr_url == "u"
    assert store.snapshot().refreshed_at == "t3"


def test_apply_outcomes_without_a_change_does_not_write(tmp_path):
    # The data repo's TriageSyncer commits+pushes every write: a no-op
    # refresh must leave state.json byte-for-byte unchanged.
    store = AutopilotStore(tmp_path)
    store.reserve(_attempt(), max_open=3)
    store.finish_launch(
        KEY,
        "a1",
        outcome=Outcome.RUNNING,
        session_name="s",
        worktree_path=None,
        detail=None,
        now="t1",
    )
    before = store.path.read_bytes()
    mtime = store.path.stat().st_mtime_ns
    assert store.apply_outcomes({}, now="t2") == 0
    stale = OutcomeUpdate("a1", Outcome.LAUNCHING, Outcome.ENDED_WITHOUT_PR)
    assert store.apply_outcomes({KEY: stale}, now="t3") == 0  # CAS miss: no change
    assert store.path.read_bytes() == before and store.path.stat().st_mtime_ns == mtime
    assert store.snapshot().refreshed_at is None


def test_stale_refresh_cannot_touch_replacement_attempt(tmp_path):
    store = AutopilotStore(tmp_path)
    store.reserve(_attempt(), max_open=3)
    store.finish_launch(
        KEY,
        "a1",
        outcome=Outcome.ENDED_WITHOUT_PR,
        session_name=None,
        worktree_path=None,
        detail=None,
        now="t1",
    )
    observed = OutcomeUpdate("a1", Outcome.ENDED_WITHOUT_PR, Outcome.CLOSED)
    assert store.retry(KEY, now="t2") is True
    store.reserve(_attempt(attempt_id="a2"), max_open=3)
    assert store.apply_outcomes({KEY: observed}, now="t3") == 0
    assert store.snapshot().attempts[KEY].outcome is Outcome.LAUNCHING


def test_retry_rules(tmp_path):
    store = AutopilotStore(tmp_path)
    store.reserve(_attempt(dispatched_at="2026-10-08T01:00:00+00:00"), max_open=3)
    assert store.retry(KEY, now="2026-10-08T01:30:00+00:00") is False  # fresh LAUNCHING
    assert store.retry(KEY, now="2026-10-08T02:30:00+00:00") is True  # stale LAUNCHING
    store.reserve(_attempt(attempt_id="a2"), max_open=3)
    store.finish_launch(
        KEY,
        "a2",
        outcome=Outcome.RUNNING,
        session_name=None,
        worktree_path=None,
        detail=None,
        now="t",
    )
    assert store.retry(KEY, now="2026-10-09T00:00:00+00:00") is False  # running
    store.apply_outcomes({KEY: OutcomeUpdate("a2", Outcome.RUNNING, Outcome.UNKNOWN)}, now="t")
    assert store.retry(KEY, now="t") is True  # unknown
    assert store.retry("nope#1", now="t") is False


def test_runs_capped(tmp_path):
    store = AutopilotStore(tmp_path)
    for i in range(MAX_RUNS + 5):
        store.record_run(
            RunRecord(
                run_id=f"r{i}",
                started_at="a",
                finished_at="b",
                launched=[],
                skipped=[("k#1", "throttled")],
                throttled=True,
                error="boom" if i % 2 == 0 else None,
            )
        )
    runs = store.snapshot().runs
    assert len(runs) == MAX_RUNS and runs[-1].run_id == f"r{MAX_RUNS + 4}"
    assert runs[-1].skipped == [("k#1", "throttled")] and runs[-1].error == "boom"


def test_malformed_run_entries_do_not_crash_reads(tmp_path):
    store = AutopilotStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps(
            {
                "paused": False,
                "attempts": {},
                "runs": [
                    {"run_id": "r1", "launched": 5, "skipped": 5},
                    {"run_id": "r2", "launched": "k#1", "skipped": {"a": "b"}},
                    "not a run",
                ],
            }
        )
    )
    store.assert_readable()
    runs = store.snapshot().runs
    assert [r.run_id for r in runs] == ["r1", "r2"]
    assert all(r.launched == [] and r.skipped == [] for r in runs)


_WRITERS = {
    "set_paused": lambda s: s.set_paused(True),
    "reserve": lambda s: s.reserve(_attempt(), max_open=3),
    "finish_launch": lambda s: s.finish_launch(
        KEY,
        "a1",
        outcome=Outcome.RUNNING,
        session_name=None,
        worktree_path=None,
        detail=None,
        now="t",
    ),
    "apply_outcomes": lambda s: s.apply_outcomes(
        {KEY: OutcomeUpdate("a1", Outcome.LAUNCHING, Outcome.RUNNING)}, now="t"
    ),
    "retry": lambda s: s.retry(KEY, now="t"),
    "record_run": lambda s: s.record_run(
        RunRecord(
            run_id="r", started_at="a", finished_at="b", launched=[], skipped=[], throttled=False
        )
    ),
}


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"attempts": {}, "runs": "nope"}).encode(),
        b"\xff\xfe{}",
    ],
)
@pytest.mark.parametrize("writer", sorted(_WRITERS))
def test_every_writer_refuses_corrupt_file(tmp_path, writer, payload):
    store = AutopilotStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(payload)
    with pytest.raises(OSError):
        _WRITERS[writer](store)
    assert store.path.read_bytes() == payload


@pytest.mark.parametrize("writer", sorted(_WRITERS))
def test_every_writer_preserves_unparsed_attempts(tmp_path, writer):
    store = AutopilotStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"paused": False, "runs": [], "attempts": {"x/y#5": {"outcome": "weird"}}})
    )
    _WRITERS[writer](store)
    assert json.loads(store.path.read_text())["attempts"]["x/y#5"] == {"outcome": "weird"}
