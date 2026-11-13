"""The remote write-action log (#397): fail-closed, local, mode 600."""

import json
import os
import stat

import pytest

from agent_hq.remote import actions
from agent_hq.remote import config as remote_config


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_default_path_is_under_the_remote_state_dir():
    # conftest redirects STATE_DIR into pytest tmp, so this never touches ~/.config.
    assert actions.ActionLog().path == remote_config.STATE_DIR / "actions.jsonl"


def test_attempt_then_result_records(tmp_path):
    log = actions.ActionLog(tmp_path / "remote" / "actions.jsonl")
    action_id = log.attempt("set_triage", {"issue_key": "o/r#1", "value": 3}, now=100.0)
    log.result(action_id, "set_triage", ok=True, code="ok", outcome="o/r#1 V3", now=101.0)
    first, second = _lines(log.path)
    assert first == {
        "ts": 100.0,
        "phase": "attempt",
        "action_id": action_id,
        "tool": "set_triage",
        "args": {"issue_key": "o/r#1", "value": 3},
    }
    assert second["phase"] == "result" and second["action_id"] == action_id
    assert (second["ok"], second["code"], second["outcome"]) == (True, "ok", "o/r#1 V3")


def test_file_mode_600_and_dir_700(tmp_path):
    log = actions.ActionLog(tmp_path / "remote" / "actions.jsonl")
    log.attempt("mute_agent", {"session": "x"})
    assert stat.S_IMODE(os.stat(log.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(log.path.parent).st_mode) == 0o700


def test_unwritable_log_raises_on_attempt(tmp_path):
    blocker = tmp_path / "remote"
    blocker.write_text("a file where the dir should be")
    log = actions.ActionLog(blocker / "actions.jsonl")
    with pytest.raises(OSError):
        log.attempt("dispatch_issues", {"issue_keys": ["o/r#1"]})


def test_result_never_raises(tmp_path):
    blocker = tmp_path / "remote"
    blocker.write_text("not a dir")
    actions.ActionLog(blocker / "actions.jsonl").result("x", "t", ok=False, code="failed")


def test_trims_to_newest_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "MAX_LINES", 5)
    monkeypatch.setattr(actions, "TRIM_AT", 8)
    log = actions.ActionLog(tmp_path / "actions.jsonl")
    for i in range(9):
        log.attempt("set_triage", {"i": i})
    kept = [r["args"]["i"] for r in _lines(log.path)]
    assert kept == [4, 5, 6, 7, 8]
    assert stat.S_IMODE(os.stat(log.path).st_mode) == 0o600


def test_recent_attempts_filters_by_tool_and_time(tmp_path):
    log = actions.ActionLog(tmp_path / "actions.jsonl")
    log.attempt("dispatch_issues", {"issue_keys": ["o/r#1"]}, now=100.0)
    log.attempt("set_triage", {"issue_key": "o/r#2"}, now=200.0)
    log.attempt("dispatch_issues", {"issue_keys": ["o/r#3"]}, now=300.0)
    got = log.recent_attempts("dispatch_issues", since=150.0)
    assert [r["args"]["issue_keys"] for r in got] == [["o/r#3"]]


def test_recent_attempts_carry_their_result_code(tmp_path):
    log = actions.ActionLog(tmp_path / "actions.jsonl")
    done = log.attempt("dispatch_issues", {"issue_keys": ["o/r#1"]}, now=10.0)
    log.result(done, "dispatch_issues", ok=True, code="ok", now=11.0)
    log.attempt("dispatch_issues", {"issue_keys": ["o/r#2"]}, now=12.0)  # no result yet
    got = log.recent_attempts("dispatch_issues", since=0.0)
    assert [r["result_code"] for r in got] == ["ok", None]


def test_recent_attempts_missing_file_is_empty(tmp_path):
    assert actions.ActionLog(tmp_path / "actions.jsonl").recent_attempts("x", since=0.0) == []


def test_recent_attempts_skips_garbage_lines(tmp_path):
    path = tmp_path / "actions.jsonl"
    path.write_text('not json\n{"phase": "attempt"}\n[1]\n')
    log = actions.ActionLog(path)
    log.attempt("dispatch_issues", {"issue_keys": ["o/r#1"]}, now=10.0)
    assert len(log.recent_attempts("dispatch_issues", since=0.0)) == 1


def test_recent_attempts_unreadable_raises(tmp_path):
    """Fail closed: a duplicate guard that can't read the log must not pass."""
    path = tmp_path / "actions.jsonl"
    path.mkdir()
    with pytest.raises(OSError):
        actions.ActionLog(path).recent_attempts("dispatch_issues", since=0.0)
