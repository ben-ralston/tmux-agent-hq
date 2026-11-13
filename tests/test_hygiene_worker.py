# tests/test_hygiene_worker.py
"""hygiene_worker (#249): stale detector, prompt, strict parser, run_sweep."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_hq import eventlog
from agent_hq import hygiene_worker as hw
from agent_hq.config import RepoConfig, TriageConfig
from agent_hq.hygiene_store import FindingStatus, FindingType, HygieneStore
from agent_hq.models import Issue, Priority
from agent_hq.triage_store import AgentFitness, Effort, TriageRecord, TriageState, TriageStore

FIXTURES = Path(__file__).parent / "fixtures" / "claude_p"
REPO = "ben-ralston/agent-hq"
OTHER = "ben-ralston/other"
NOW = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
THRESHOLDS = {"stale_days": 90, "stale_max_value": 2}


def _issue(number, *, repo=REPO, title=None, labels=(), body="", updated_at=None):
    return Issue(
        number=number,
        title=title or f"issue {number}",
        repo=repo,
        status="Todo",
        priority=Priority.IDEA,
        labels=list(labels),
        body=body,
        updated_at=updated_at,
    )


def _rec(key, value=None, *, tombstoned=False, state=TriageState.CONFIRMED):
    return TriageRecord(
        issue_key=key, value=value, updated_at="t", tombstoned_at="t" if tombstoned else None,
        state=state,
    )


def _confirm(ts, key, value):
    """A CONFIRMED record the way the app makes one: full dims, then Confirm."""
    ts.upsert(
        TriageRecord(issue_key=key, value=value, effort=Effort.S,
                     agent_fitness=AgentFitness.SOLO, updated_at="t"),
        actor="human",
    )
    assert ts.confirm(key, now="t") is not None


# ---- detect_stale ----


def test_detect_stale_requires_old_and_low_value():
    old, fresh = "2026-01-01T00:00:00Z", "2026-09-10T00:00:00+00:00"
    issues = [
        _issue(1, updated_at=old),  # value 2 → stale
        _issue(2, updated_at=old),  # value 3 → not stale
        _issue(3, updated_at=fresh),  # recent → not stale
        _issue(4, updated_at=old),  # no value → skipped
        _issue(5, updated_at=None),  # unknown date → skipped
        _issue(6, updated_at=old),  # tombstoned → skipped
        _issue(7, updated_at="garbage"),  # unparseable → skipped
    ]
    records = {
        f"{REPO}#1": _rec(f"{REPO}#1", 2),
        f"{REPO}#2": _rec(f"{REPO}#2", 3),
        f"{REPO}#3": _rec(f"{REPO}#3", 1),
        f"{REPO}#4": _rec(f"{REPO}#4", None),
        f"{REPO}#5": _rec(f"{REPO}#5", 1),
        f"{REPO}#6": _rec(f"{REPO}#6", 1, tombstoned=True),
        f"{REPO}#7": _rec(f"{REPO}#7", 1),
    }
    found = hw.detect_stale(issues, records, THRESHOLDS, now=NOW)
    assert [f.issue_keys for f in found] == [[f"{REPO}#1"]]
    f = found[0]
    assert f.type is FindingType.STALE and f.id == f"stale:{REPO}#1"
    assert f.action == {
        "kind": "close_stale",
        "close": f"{REPO}#1",
        "last_activity": old,
        "value": 2,
    }
    assert "2026-01-01" in f.reasoning


def test_detect_stale_boundary_is_inclusive():
    exactly = (NOW - timedelta(days=90)).isoformat()
    issues = [_issue(1, updated_at=exactly)]
    records = {f"{REPO}#1": _rec(f"{REPO}#1", 2)}
    assert len(hw.detect_stale(issues, records, THRESHOLDS, now=NOW)) == 1
    assert hw.detect_stale(issues, records, {"stale_days": 91, "stale_max_value": 2}, now=NOW) == []


# ---- prompt + schema ----


def test_build_hygiene_prompt_lists_every_issue_with_context():
    issues = [
        _issue(1, title="dup A", body="B" * 500, labels=["sentinel"]),
        _issue(2, repo=OTHER, title="other repo"),
    ]
    records = {f"{REPO}#1": _rec(f"{REPO}#1", 4)}
    repos = {
        REPO: RepoConfig(REPO, Path("/nope"), description="Monitors agents"),
        OTHER: RepoConfig(OTHER, Path("/nope")),
    }
    prompt = hw.build_hygiene_prompt(issues, records, repos)
    assert prompt.startswith(hw.HYGIENE_RUBRIC)
    assert f"Repository: {REPO}" in prompt and "Monitors agents" in prompt
    assert f"Repository: {OTHER}" in prompt
    assert f"{REPO}#1" in prompt and f"{OTHER}#2" in prompt
    assert "sentinel: yes" in prompt and "sentinel: no" in prompt
    assert "value: 4" in prompt and "value: unscored" in prompt
    assert "B" * 300 in prompt and "B" * 301 not in prompt
    assert "Only use the issue keys listed above" in prompt


def test_schema_requires_all_three_lists_and_caps_reasoning():
    s = hw.HYGIENE_OUTPUT_SCHEMA
    assert set(s["required"]) == {"duplicates", "clusters", "mismatches"}
    for name in ("duplicates", "clusters", "mismatches"):
        item = s["properties"][name]["items"]
        assert item["properties"]["reasoning"]["maxLength"] == hw.MAX_REASONING_CHARS
        assert "reasoning" in item["required"]
    assert s["properties"]["mismatches"]["items"]["properties"]["proposed_value"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 5,
    }


# ---- parse_findings ----


def _ctx():
    issues = [
        _issue(1, title="reading list A"),
        _issue(2, title="reading list B"),
        _issue(3, title="[sentinel] boom", labels=["sentinel"]),
        _issue(4, title="[sentinel] boom again", labels=["sentinel"]),
        _issue(5, title="token leak", body="secrets in logs"),
        _issue(6, repo=OTHER, title="[sentinel] elsewhere", labels=["sentinel"]),
    ]
    records = {f"{REPO}#5": _rec(f"{REPO}#5", 2), f"{REPO}#1": _rec(f"{REPO}#1", 3)}
    return {i.key: i for i in issues}, records


def _good():
    return {
        "duplicates": [{"keep": f"{REPO}#1", "close": f"{REPO}#2", "reasoning": "same"}],
        "clusters": [
            {
                "repo": REPO,
                "umbrella_title": "Sentinel booms",
                "children": [f"{REPO}#3", f"{REPO}#4"],
                "reasoning": "one root cause",
            }
        ],
        "mismatches": [{"key": f"{REPO}#5", "proposed_value": 5, "reasoning": "security"}],
    }


def test_parse_findings_happy_path_builds_typed_findings():
    by_key, records = _ctx()
    found = hw.parse_findings(_good(), issues_by_key=by_key, records=records)
    assert [f.type for f in found] == [
        FindingType.DUPLICATE,
        FindingType.CLUSTER,
        FindingType.MISMATCH,
    ]
    dup, cluster, mismatch = found
    assert dup.id == f"duplicate:{REPO}#1+{REPO}#2"
    assert dup.action == {"kind": "close_as_duplicate", "close": f"{REPO}#2", "of": f"{REPO}#1"}
    assert cluster.id == f"cluster:{REPO}#3+{REPO}#4"
    assert cluster.action == {
        "kind": "merge_cluster",
        "repo": REPO,
        "umbrella_title": "Sentinel booms",
        "children": [f"{REPO}#3", f"{REPO}#4"],
    }
    assert mismatch.action == {
        "kind": "bump_value",
        "key": f"{REPO}#5",
        "current": 2,
        "proposed": 5,
    }


def test_parse_findings_empty_lists_are_valid():
    by_key, records = _ctx()
    empty = {"duplicates": [], "clusters": [], "mismatches": []}
    assert hw.parse_findings(empty, issues_by_key=by_key, records=records) == []


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda s: s["duplicates"][0].update(close=f"{REPO}#999"), "unknown"),
        (lambda s: s["duplicates"][0].update(close=f"{REPO}#1"), "same issue"),
        (lambda s: s["duplicates"].append(dict(s["duplicates"][0])), "duplicate finding"),
        (lambda s: s["clusters"][0].update(children=[f"{REPO}#3"]), "at least 2"),
        (lambda s: s["clusters"][0].update(children=[f"{REPO}#3", f"{OTHER}#6"]), "repo"),
        (lambda s: s["clusters"][0].update(children=[f"{REPO}#3", f"{REPO}#5"]), "sentinel"),
        (lambda s: s["clusters"][0].update(umbrella_title=""), "umbrella_title"),
        (lambda s: s["mismatches"][0].update(proposed_value=2), "current value"),
        (lambda s: s["mismatches"][0].update(proposed_value=9), "1-5"),
        (lambda s: s["mismatches"][0].update(reasoning=""), "reasoning"),
        (lambda s: s["mismatches"][0].update(reasoning="x" * 201), "reasoning"),
        (lambda s: s.pop("clusters"), "clusters"),
    ],
)
def test_parse_findings_rejects_whole_batch(mutate, match):
    by_key, records = _ctx()
    structured = _good()
    mutate(structured)
    with pytest.raises(hw.HygieneParseError, match=match):
        hw.parse_findings(structured, issues_by_key=by_key, records=records)


def test_parse_findings_rejects_non_object():
    by_key, records = _ctx()
    with pytest.raises(hw.HygieneParseError):
        hw.parse_findings(["nope"], issues_by_key=by_key, records=records)


# ---- run_sweep ----




class FakeRunner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, prompt, timeout_s, cwd):
        self.calls.append((argv, prompt, timeout_s, cwd))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _ok():
    return (FIXTURES / "hygiene_ok.json").read_text()


def _bad():
    return (FIXTURES / "hygiene_malformed.json").read_text()


def _sweep_ctx(tmp_path, **cfg):
    data = tmp_path / "data"
    data.mkdir()
    old = "2026-01-01T00:00:00Z"
    issues = [
        _issue(1, title="reading list A", updated_at="2026-09-01T00:00:00Z"),
        _issue(2, title="reading list B", updated_at="2026-09-01T00:00:00Z"),
        _issue(3, title="[sentinel] boom", labels=["sentinel"], updated_at="2026-09-01T00:00:00Z"),
        _issue(4, title="[sentinel] boom", labels=["sentinel"], updated_at="2026-09-01T00:00:00Z"),
        _issue(5, title="token leak", updated_at="2026-09-01T00:00:00Z"),
        _issue(8, title="old idea", updated_at=old),  # stale: value 1, untouched since Jan
    ]
    ts = TriageStore(data)
    ts.upsert(_rec(f"{REPO}#5", 2), actor="human")
    _confirm(ts, f"{REPO}#8", 1)  # stale needs a human-confirmed value (round 3)
    hs = HygieneStore(data)
    tcfg = TriageConfig(data_dir=data, **cfg)
    repos = {REPO: RepoConfig(REPO, tmp_path / "co", description="Monitors agents")}
    return issues, ts, hs, tcfg, repos


def _emits(monkeypatch):
    calls = []
    monkeypatch.setattr(
        eventlog, "emit", lambda level, msg, extra=None, **kw: calls.append((level, msg))
    )
    return calls


def test_run_sweep_writes_stale_and_ai_findings(tmp_path, monkeypatch):
    emitted = _emits(monkeypatch)
    issues, ts, hs, tcfg, repos = _sweep_ctx(
        tmp_path, hygiene_model="sonnet", hygiene_timeout_s=42
    )
    runner = FakeRunner([_ok()])
    report = hw.run_sweep(issues, ts, hs, tcfg, repos, runner=runner, now=NOW)
    assert report.error is None and not report.dry_run
    assert (report.stale, report.duplicates, report.clusters, report.mismatches) == (1, 1, 1, 1)
    assert report.written == 4 and report.total == 4
    argv, prompt, timeout_s, cwd = runner.calls[0]
    assert argv[argv.index("--model") + 1] == "sonnet" and timeout_s == 42
    assert json.loads(argv[argv.index("--json-schema") + 1]) == hw.HYGIENE_OUTPUT_SCHEMA
    assert cwd == hs.data_dir and prompt.startswith(hw.HYGIENE_RUBRIC)
    stored = {f.id: f for f in hs.all_findings()}
    assert f"stale:{REPO}#8" in stored and f"duplicate:{REPO}#1+{REPO}#2" in stored
    assert all(f.model == "sonnet" and f.status is FindingStatus.OPEN for f in stored.values())
    assert hs.last_swept_at() == NOW.isoformat(timespec="seconds")
    assert ("info", "hygiene sweep run") in emitted


def test_run_sweep_defaults_model_to_ai_model(tmp_path):
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path, ai_model="opus")
    runner = FakeRunner([_ok()])
    hw.run_sweep(issues, ts, hs, tcfg, repos, runner=runner, now=NOW)
    argv = runner.calls[0][0]
    assert argv[argv.index("--model") + 1] == "opus"
    assert hs.all_findings()[0].model == "opus"


def test_run_sweep_empty_result_is_success(tmp_path, monkeypatch):
    emitted = _emits(monkeypatch)
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    empty = json.loads(_ok())
    empty["structured_output"] = {"duplicates": [], "clusters": [], "mismatches": []}
    report = hw.run_sweep(
        issues[:5], ts, hs, tcfg, repos, runner=FakeRunner([json.dumps(empty)]), now=NOW
    )
    assert report.error is None and report.total == 0 and report.written == 0
    assert hs.last_swept_at() is not None
    assert not any(level == "error" for level, _ in emitted)


def test_run_sweep_retries_once_then_rejects_and_stamps(tmp_path, monkeypatch):
    emitted = _emits(monkeypatch)
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    runner = FakeRunner([_bad(), _bad()])
    report = hw.run_sweep(issues, ts, hs, tcfg, repos, runner=runner, now=NOW)
    assert report.error and report.error.startswith("parse:")
    assert len(runner.calls) == 2
    assert "previous output was rejected" in runner.calls[1][1]
    # code-found stale findings still land on a rejected AI batch
    assert [f.type for f in hs.all_findings()] == [FindingType.STALE]
    assert hs.last_swept_at() == NOW.isoformat(timespec="seconds")  # ...but stamped
    assert ("error", "hygiene sweep output rejected") in emitted


def test_run_sweep_retry_succeeds(tmp_path):
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    report = hw.run_sweep(issues, ts, hs, tcfg, repos, runner=FakeRunner([_bad(), _ok()]), now=NOW)
    assert report.error is None and report.written == 4


def test_run_sweep_invoke_failure_warns_no_retry_and_stamps(tmp_path, monkeypatch):
    emitted = _emits(monkeypatch)
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    runner = FakeRunner([hw.TriageInvokeError("claude binary not found on PATH")])
    report = hw.run_sweep(issues, ts, hs, tcfg, repos, runner=runner, now=NOW)
    assert report.error and report.error.startswith("invoke:")
    assert len(runner.calls) == 1
    assert [f.type for f in hs.all_findings()] == [FindingType.STALE]
    assert hs.last_swept_at() is not None
    assert ("warn", "hygiene sweep invoke failed") in emitted


def test_run_sweep_dry_run_writes_nothing_not_even_the_stamp(tmp_path):
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    report = hw.run_sweep(
        issues, ts, hs, tcfg, repos, dry_run=True, runner=FakeRunner([_ok()]), now=NOW
    )
    assert report.dry_run and report.total == 4 and report.written == 0
    assert hs.all_findings() == [] and hs.last_swept_at() is None


def test_run_sweep_suppresses_dismissed_and_resolves_gone(tmp_path):
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    hw.run_sweep(issues, ts, hs, tcfg, repos, runner=FakeRunner([_ok()]), now=NOW)
    hs.dismiss(f"duplicate:{REPO}#1+{REPO}#2", now="t")
    # #8 closed since: its stale finding resolves; the dup stays dismissed.
    report = hw.run_sweep(issues[:5], ts, hs, tcfg, repos, runner=FakeRunner([_ok()]), now=NOW)
    # cluster + mismatch are still open and re-reported → refreshed (counts as written);
    # the dismissed dup is suppressed.
    assert report.resolved == 1 and report.written == 2
    by_id = {f.id: f for f in hs.all_findings()}
    assert by_id[f"stale:{REPO}#8"].status is FindingStatus.RESOLVED
    assert by_id[f"duplicate:{REPO}#1+{REPO}#2"].status is FindingStatus.DISMISSED


def test_run_sweep_skips_the_ai_call_with_no_issues(tmp_path):
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    runner = FakeRunner([])
    report = hw.run_sweep([], ts, hs, tcfg, repos, runner=runner, now=NOW)
    assert report.error is None and runner.calls == []
    assert hs.last_swept_at() is None  # round 3: no stamp on an empty list


def test_run_sweep_reports_store_write_failure(tmp_path, monkeypatch):
    emitted = _emits(monkeypatch)
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    path = hs.data_dir / "hygiene" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{corrupt")
    report = hw.run_sweep(issues, ts, hs, tcfg, repos, runner=FakeRunner([_ok()]), now=NOW)
    assert report.written == 0 and report.error and report.error.startswith("write:")
    assert ("warn", "hygiene write failed") in emitted
    assert path.read_text() == "{corrupt"
# ---- review round (2026-09-12) ----


@pytest.mark.parametrize(
    "mutate, match",
    [
        # One-point nudge: current 2 → 3 is below the rubric's ≥2 gate.
        (lambda s: s["mismatches"][0].update(proposed_value=3), "at least 2"),
        # Unscored record (#2 has no triage record) must not be reported.
        (lambda s: s["mismatches"][0].update(key=f"{REPO}#2"), "unscored"),
    ],
)
def test_parse_findings_enforces_mismatch_rubric(mutate, match):
    by_key, records = _ctx()
    structured = _good()
    mutate(structured)
    with pytest.raises(hw.HygieneParseError, match=match):
        hw.parse_findings(structured, issues_by_key=by_key, records=records)


def test_run_sweep_with_no_issues_stamps_without_touching_findings(tmp_path):
    """Review P1: an empty list (cold cache / failed fetch) must never reach
    record_sweep — with an empty open set every finding would resolve."""
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    hw.run_sweep(issues, ts, hs, tcfg, repos, runner=FakeRunner([_ok()]), now=NOW)
    before = {f.id: f.status for f in hs.all_findings()}
    assert before and all(s is FindingStatus.OPEN for s in before.values())
    runner = FakeRunner([])
    report = hw.run_sweep([], ts, hs, tcfg, repos, runner=runner, now=NOW)
    assert report.error is None and runner.calls == [] and report.resolved == 0
    assert {f.id: f.status for f in hs.all_findings()} == before


def test_run_sweep_corrupt_store_with_clean_backlog_is_still_a_write_error(tmp_path, monkeypatch):
    """Review P1: zero findings + unreadable findings.json must report a write
    error (so the web trigger's cooldown trips) instead of a silent success
    that never stamps and re-kicks a paid call on every load."""
    emitted = _emits(monkeypatch)
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    path = hs.data_dir / "hygiene" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{corrupt")
    empty = json.loads(_ok())
    empty["structured_output"] = {"duplicates": [], "clusters": [], "mismatches": []}
    report = hw.run_sweep(
        issues[:5], ts, hs, tcfg, repos, runner=FakeRunner([json.dumps(empty)]), now=NOW
    )
    assert report.error and report.error.startswith("write:")
    assert ("warn", "hygiene write failed") in emitted
    assert path.read_text() == "{corrupt"


def test_run_sweep_still_writes_code_detected_stale_when_the_ai_call_fails(tmp_path, monkeypatch):
    """Review round 2: the stale rule needs no model; a claude outage must not
    drop what pure code already found. The error still reports (cooldown)."""
    _emits(monkeypatch)
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    runner = FakeRunner([hw.TriageInvokeError("claude binary not found on PATH")])
    report = hw.run_sweep(issues, ts, hs, tcfg, repos, runner=runner, now=NOW)
    assert report.error and report.error.startswith("invoke:")
    assert report.stale == 1 and report.written == 1
    assert [f.type for f in hs.all_findings()] == [FindingType.STALE]
    assert hs.last_swept_at() == NOW.isoformat(timespec="seconds")


# ---- review round 3 (2026-09-13) ----


def test_run_sweep_with_no_issues_does_not_stamp(tmp_path):
    """Built-in review: stamping on an empty list (cold cache, failed fetch)
    silently disabled the automatic sweep for 24 h. No issues → nothing
    happens, no stamp, no error."""
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    report = hw.run_sweep([], ts, hs, tcfg, repos, runner=FakeRunner([]), now=NOW)
    assert report.error is None and hs.last_swept_at() is None


def test_parse_findings_rejects_overlapping_clusters():
    """Built-in review: two clusters sharing a child would insert-then-resolve
    each other in one record_sweep, suppressing a fingerprint forever."""
    by_key, records = _ctx()
    by_key[f"{REPO}#7"] = _issue(7, title="[sentinel] boom 3", labels=["sentinel"])
    structured = _good()
    structured["clusters"].append({
        "repo": REPO, "umbrella_title": "Overlap",
        "children": [f"{REPO}#4", f"{REPO}#7"], "reasoning": "same",
    })
    with pytest.raises(hw.HygieneParseError, match="overlap"):
        hw.parse_findings(structured, issues_by_key=by_key, records=records)


def test_detect_stale_requires_a_confirmed_value():
    """Built-in review: an unconfirmed AI-proposed value ≤ threshold must not
    turn into a close on one tap — PROPOSED means "awaiting a human"."""
    old = "2026-01-01T00:00:00Z"
    issues = [_issue(1, updated_at=old), _issue(2, updated_at=old)]
    records = {
        f"{REPO}#1": _rec(f"{REPO}#1", 1, state=TriageState.PROPOSED),
        f"{REPO}#2": _rec(f"{REPO}#2", 1, state=TriageState.CONFIRMED),
    }
    found = hw.detect_stale(issues, records, {"stale_days": 90, "stale_max_value": 2}, now=NOW)
    assert [f.issue_keys for f in found] == [[f"{REPO}#2"]]


# ---- #273: open issues missing from their project board ----

GADGET = "ben-ralston/gadget"
_LOGGER_TARGET = hw.BoardTarget(
    repo=GADGET, project_name="Gadget", project_number=13, project_owner="@me"
)


def _inputs(open_keys, board_keys):
    return hw.OffboardInputs(
        open_by_repo={GADGET: set(open_keys)},
        board_by_repo={GADGET: set(board_keys)},
        targets={GADGET: _LOGGER_TARGET},
    )


def test_detect_offboard_reports_open_issues_not_on_the_board():
    seen = []

    def describe(repo, number):
        seen.append((repo, number))
        return {"title": f"title {number}", "state": "open"}

    found = hw.detect_offboard(
        _inputs([f"{GADGET}#8", f"{GADGET}#9", f"{GADGET}#3"], [f"{GADGET}#3"]),
        describe=describe,
    )
    assert [f.id for f in found] == [f"offboard:{GADGET}#8", f"offboard:{GADGET}#9"]
    f = found[0]
    assert f.type is FindingType.OFFBOARD and f.issue_keys == [f"{GADGET}#8"]
    assert f.action == {
        "kind": "add_to_board",
        "key": f"{GADGET}#8",
        "project_number": 13,
        "project_owner": "@me",
        "project_name": "Gadget",
        "title": "title 8",
    }
    assert "Gadget board" in f.reasoning
    assert seen == [(GADGET, 8), (GADGET, 9)]


def test_detect_offboard_skips_an_issue_the_lookup_says_is_closed():
    """The index can lag a close; REST is the fresher word (0 points)."""
    found = hw.detect_offboard(
        _inputs([f"{GADGET}#8"], []),
        describe=lambda repo, number: {"title": "gone", "state": "closed"},
    )
    assert found == []


def test_detect_offboard_reports_without_a_title_when_the_lookup_fails():
    found = hw.detect_offboard(_inputs([f"{GADGET}#8"], []), describe=lambda r, n: None)
    assert len(found) == 1 and found[0].action["title"] is None
    found = hw.detect_offboard(_inputs([f"{GADGET}#8"], []))  # no lookup at all
    assert len(found) == 1 and found[0].action["title"] is None


def test_detect_offboard_caps_the_lookups(monkeypatch):
    monkeypatch.setattr(hw, "MAX_OFFBOARD_LOOKUPS", 2)
    calls = []
    found = hw.detect_offboard(
        _inputs([f"{GADGET}#{n}" for n in range(1, 6)], []),
        describe=lambda r, n: calls.append(n) or {"title": f"t{n}", "state": "open"},
    )
    assert len(found) == 5 and calls == [1, 2]
    assert sum(1 for f in found if f.action["title"] is not None) == 2


class _BoardCache:
    def __init__(self, boards):
        self.boards = boards  # (number, owner) -> list[Issue] | None

    def get_issues_cached(self, number, owner):
        issues = self.boards.get((number, owner))
        return (None, False) if issues is None else (list(issues), True)


def test_collect_offboard_inputs_uses_configured_projects_and_counts_done_items(monkeypatch):
    from agent_hq.config import Config, DispatchConfig, ProjectConfig

    cfg = Config(
        repos={
            GADGET: RepoConfig(GADGET, Path("/tmp/l"), project="Gadget"),
            REPO: RepoConfig(REPO, Path("/tmp/a"), project="Agent HQ"),
            "ben-ralston/sprockets": RepoConfig("ben-ralston/sprockets", Path("/tmp/d")),
            OTHER: RepoConfig(OTHER, Path("/tmp/o"), project="Other"),
        },
        projects=[
            ProjectConfig(name="Gadget", number=13, owner="@me"),
            ProjectConfig(name="Agent HQ", number=12, owner="@me"),
            ProjectConfig(name="Other", number=99, owner="@me"),
        ],
        worktree_root=Path("/tmp/wt"),
        dispatch=DispatchConfig(),
    )
    done = _issue(3, repo=GADGET)
    done.status = "Done"
    cache = _BoardCache(
        {
            (13, "@me"): [_issue(1, repo=GADGET), done],
            (12, "@me"): [_issue(7)],
            # (99, "@me") never fetched → cold → repo skipped
        }
    )
    opened = {GADGET: dict.fromkeys([f"{GADGET}#1", f"{GADGET}#3", f"{GADGET}#8"]), REPO: None}
    asked = []
    monkeypatch.setattr(
        hw, "open_issue_rows", lambda repo: asked.append(repo) or opened.get(repo)
    )
    inputs = hw.collect_offboard_inputs(cfg, cache)
    # sprockets has no project: never asked; Other's board is cold: never asked;
    # agent-hq's index is unseeded (None): asked, then skipped.
    assert asked == [GADGET, REPO]
    assert inputs.open_by_repo == {GADGET: {f"{GADGET}#1", f"{GADGET}#3", f"{GADGET}#8"}}
    # Done counts as on-board; agent-hq's board is known even though its
    # unseeded index keeps the repo out of open_by_repo.
    assert inputs.board_by_repo == {GADGET: {f"{GADGET}#1", f"{GADGET}#3"}, REPO: {f"{REPO}#7"}}
    assert inputs.targets == {GADGET: _LOGGER_TARGET}
    assert [f.id for f in hw.detect_offboard(inputs)] == [f"offboard:{GADGET}#8"]


def test_run_sweep_writes_offboard_findings_and_scopes_their_resolution(tmp_path):
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    inputs = _inputs([f"{GADGET}#8", f"{GADGET}#9"], [])
    report = hw.run_sweep(
        issues, ts, hs, tcfg, repos, runner=FakeRunner([_ok()]), now=NOW, offboard=inputs
    )
    assert report.error is None and report.offboard == 2
    stored = {f.id: f for f in hs.all_findings()}
    assert stored[f"offboard:{GADGET}#8"].status is FindingStatus.OPEN
    # Next sweep: #9 went away (closed) — resolved because gadget was checked;
    # #8 is still off the board — refreshed.
    later = NOW + timedelta(days=1)
    report = hw.run_sweep(
        issues, ts, hs, tcfg, repos, runner=FakeRunner([_ok()]), now=later,
        offboard=_inputs([f"{GADGET}#8"], []),
    )
    assert report.offboard == 1 and report.resolved == 1
    assert hs.get(f"offboard:{GADGET}#9").status is FindingStatus.RESOLVED
    assert hs.get(f"offboard:{GADGET}#8").status is FindingStatus.OPEN
    # A sweep with no off-board inputs at all (cold cache) leaves #8 alone.
    report = hw.run_sweep(issues, ts, hs, tcfg, repos, runner=FakeRunner([_ok()]), now=later)
    assert report.offboard == 0 and report.resolved == 0
    assert hs.get(f"offboard:{GADGET}#8").status is FindingStatus.OPEN


def test_collect_offboard_inputs_skips_a_board_whose_refresh_failed(monkeypatch):
    """Hand-test 2026-09-14: a board fetch that failed serves last-good data,
    and an issue filed since then reads as off-board. Membership is unknown
    for that board — skip the repo rather than guess."""
    from agent_hq.config import Config, DispatchConfig, ProjectConfig

    cfg = Config(
        repos={GADGET: RepoConfig(GADGET, Path("/tmp/l"), project="Gadget")},
        projects=[ProjectConfig(name="Gadget", number=13, owner="@me")],
        worktree_root=Path("/tmp/wt"),
        dispatch=DispatchConfig(),
    )
    cache = _BoardCache({(13, "@me"): [_issue(1, repo=GADGET)]})
    cache.failed_keys = lambda: {"@me/13"}
    monkeypatch.setattr(
        hw, "open_issue_rows", lambda repo: dict.fromkeys([f"{GADGET}#1", f"{GADGET}#8"])
    )
    inputs = hw.collect_offboard_inputs(cfg, cache)
    assert inputs.open_by_repo == {} and inputs.board_by_repo == {}


def test_collect_offboard_inputs_skips_a_stale_board(monkeypatch):
    """Review round 1: a TTL-expired entry is a board nobody just refetched
    (a budget-skipped background refresh serves it without a failure
    record) — membership may lag a just-filed issue, so skip the repo."""
    from agent_hq.config import Config, DispatchConfig, ProjectConfig

    cfg = Config(
        repos={GADGET: RepoConfig(GADGET, Path("/tmp/l"), project="Gadget")},
        projects=[ProjectConfig(name="Gadget", number=13, owner="@me")],
        worktree_root=Path("/tmp/wt"),
        dispatch=DispatchConfig(),
    )

    class _Stale(_BoardCache):
        def get_issues_cached(self, number, owner):
            return [_issue(1, repo=GADGET)], False

    monkeypatch.setattr(
        hw, "open_issue_rows", lambda repo: dict.fromkeys([f"{GADGET}#1", f"{GADGET}#8"])
    )
    inputs = hw.collect_offboard_inputs(cfg, _Stale({}))
    assert inputs.open_by_repo == {} and inputs.board_by_repo == {}


def test_run_sweep_writes_offboard_findings_for_an_empty_board(tmp_path):
    """Review round 1: the headline case — a freshly configured board with
    nothing on it (or everything Done) — yields an empty open set. The
    off-board findings must still be written; resolution is repo-scoped so
    an empty open set resolves nothing else."""
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    hs.record_sweep(
        [hw.Finding.new(
            FindingType.DUPLICATE, [f"{REPO}#1", f"{REPO}#2"],
            action={"kind": "close_as_duplicate", "close": f"{REPO}#2", "of": f"{REPO}#1"},
            reasoning="same",
        )],
        now="2026-09-01T00:00:00+00:00", sweep_id="s0", model=None,
        open_keys={f"{REPO}#1", f"{REPO}#2"},
    )
    runner = FakeRunner([])
    report = hw.run_sweep(
        [], ts, hs, tcfg, repos, runner=runner, now=NOW,
        offboard=_inputs([f"{GADGET}#8"], []),
    )
    assert runner.calls == []  # no AI call over nothing
    assert report.offboard == 1 and report.written == 1 and report.resolved == 0
    assert hs.get(f"offboard:{GADGET}#8").status is FindingStatus.OPEN
    assert hs.get(f"duplicate:{REPO}#1+{REPO}#2").status is FindingStatus.OPEN
    assert hs.last_swept_at() == NOW.isoformat(timespec="seconds")


def test_collect_offboard_inputs_survives_an_index_failure_for_one_repo(monkeypatch):
    """Review round 2: the collector runs outside run_sweep's never-raises
    boundary; an unexpected error syncing one repo's index must skip that
    repo, not take the sweep (and its hygiene.swept publish) down."""
    from agent_hq.config import Config, DispatchConfig, ProjectConfig

    cfg = Config(
        repos={
            GADGET: RepoConfig(GADGET, Path("/tmp/l"), project="Gadget"),
            REPO: RepoConfig(REPO, Path("/tmp/a"), project="Agent HQ"),
        },
        projects=[
            ProjectConfig(name="Gadget", number=13, owner="@me"),
            ProjectConfig(name="Agent HQ", number=12, owner="@me"),
        ],
        worktree_root=Path("/tmp/wt"),
        dispatch=DispatchConfig(),
    )
    cache = _BoardCache({(13, "@me"): [_issue(1, repo=GADGET)], (12, "@me"): [_issue(7)]})

    def flaky(repo):
        if repo == GADGET:
            raise KeyError("unexpected payload")
        return dict.fromkeys([f"{REPO}#7", f"{REPO}#9"])

    monkeypatch.setattr(hw, "open_issue_rows", flaky)
    inputs = hw.collect_offboard_inputs(cfg, cache)
    assert inputs.open_by_repo == {REPO: {f"{REPO}#7", f"{REPO}#9"}}


def _cfg_logger():
    from agent_hq.config import Config, DispatchConfig, ProjectConfig

    return Config(
        repos={GADGET: RepoConfig(GADGET, Path("/tmp/l"), project="Gadget")},
        projects=[ProjectConfig(name="Gadget", number=13, owner="@me")],
        worktree_root=Path("/tmp/wt"),
        dispatch=DispatchConfig(),
    )


def test_collect_offboard_inputs_reads_failures_before_the_cached_read(monkeypatch):
    """Review round 3: the real cache clears a key's failure record when it
    serves a TTL-fresh entry, so a check AFTER get_issues_cached never sees
    a failed forced refetch that landed within the TTL."""

    class _Clearing(_BoardCache):
        failed = {"@me/13"}

        def failed_keys(self):
            return set(self.failed)

        def get_issues_cached(self, number, owner):
            self.failed.clear()  # what GitHubCache._fresh_entry does
            return [_issue(1, repo=GADGET)], True

    monkeypatch.setattr(hw, "open_issue_rows", lambda repo: dict.fromkeys([f"{GADGET}#8"]))
    inputs = hw.collect_offboard_inputs(_cfg_logger(), _Clearing({}))
    assert inputs.open_by_repo == {}


def test_collect_offboard_inputs_ignores_issues_newer_than_the_board_snapshot(monkeypatch):
    """Review round 3: the board is a snapshot (up to a TTL old from the CLI,
    or a failed refetch inside the TTL) while the index is synced now. An
    issue created after the snapshot — or within the Auto-add grace before
    it — cannot be judged; unknown creation dates still are."""

    class _Stamped(_BoardCache):
        def cached_at(self, number, owner):
            return 1_000_000.0

    old = "1970-01-11T00:00:00Z"  # a day before the snapshot (1_000_000 = Jan 12 13:46)
    just_before = "1970-01-12T13:40:00Z"  # inside the OFFBOARD_MIN_AGE_S grace
    after = "1970-01-12T14:00:00Z"  # after the snapshot
    rows = {f"{GADGET}#1": old, f"{GADGET}#2": just_before, f"{GADGET}#3": after,
            f"{GADGET}#4": None}
    monkeypatch.setattr(hw, "open_issue_rows", lambda repo: rows)
    inputs = hw.collect_offboard_inputs(_cfg_logger(), _Stamped({(13, "@me"): []}))
    assert inputs.open_by_repo == {GADGET: {f"{GADGET}#1", f"{GADGET}#4"}}


def test_collect_offboard_inputs_records_status_less_board_items(monkeypatch):
    """Review round 3: a board item with no Status (an item-add whose Todo
    write failed) is what keeps a partially executed finding alive."""
    no_status = _issue(5, repo=GADGET)
    no_status.status = None
    cache = _BoardCache({(13, "@me"): [_issue(1, repo=GADGET), no_status]})
    monkeypatch.setattr(hw, "open_issue_rows", lambda repo: dict.fromkeys([f"{GADGET}#1"]))
    inputs = hw.collect_offboard_inputs(_cfg_logger(), cache)
    assert inputs.statusless_keys == {f"{GADGET}#5"}


def test_run_sweep_passes_status_less_keys_to_the_store(tmp_path, monkeypatch):
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    seen = {}
    real = hs.record_sweep

    def spy(findings, **kw):
        seen.update(kw)
        return real(findings, **kw)

    monkeypatch.setattr(hs, "record_sweep", spy)
    inputs = _inputs([f"{GADGET}#8"], [])
    inputs.statusless_keys = {f"{GADGET}#9"}
    hw.run_sweep(issues, ts, hs, tcfg, repos, runner=FakeRunner([_ok()]), now=NOW, offboard=inputs)
    assert seen["offboard_statusless"] == {f"{GADGET}#9"}


def test_detect_offboard_drops_a_missing_issue():
    found = hw.detect_offboard(
        _inputs([f"{GADGET}#8"], []), describe=lambda r, n: {"title": "", "state": "missing"}
    )
    assert found == []


def test_run_sweep_reports_skipped_when_there_is_nothing_to_check(tmp_path):
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    report = hw.run_sweep([], ts, hs, tcfg, repos, runner=FakeRunner([]), now=NOW,
                          offboard=hw.OffboardInputs())
    assert report.skipped is True and hs.last_swept_at() is None
    report = hw.run_sweep(issues, ts, hs, tcfg, repos, runner=FakeRunner([_ok()]), now=NOW)
    assert report.skipped is False


def test_detect_offboard_judges_membership_against_the_repos_own_board():
    """Review round 7: an issue parked on a DIFFERENT configured board is
    off its repo's board — the premise is "not on its board", and the
    ACCEPTED-different-board revive expects the re-report."""
    inputs = hw.OffboardInputs(
        open_by_repo={GADGET: {f"{GADGET}#8"}},
        board_by_repo={GADGET: set(), REPO: {f"{GADGET}#8"}},
        targets={GADGET: _LOGGER_TARGET},
    )
    assert [f.id for f in hw.detect_offboard(inputs)] == [f"offboard:{GADGET}#8"]


def test_collect_offboard_inputs_excludes_closed_issues_from_the_status_less_set(monkeypatch):
    """Review round 7: a partially added finding whose issue was then closed
    must be allowed to resolve — a closed, status-less board item is not a
    Todo write anyone still has to finish."""
    closed_item = _issue(5, repo=GADGET)
    closed_item.status = None
    open_item = _issue(6, repo=GADGET)
    open_item.status = None
    cache = _BoardCache({(13, "@me"): [closed_item, open_item]})
    cache.get_closed_keys_cached = lambda repos: ({f"{GADGET}#5"}, True)
    monkeypatch.setattr(hw, "open_issue_rows", lambda repo: dict.fromkeys([f"{GADGET}#6"]))
    inputs = hw.collect_offboard_inputs(_cfg_logger(), cache)
    assert inputs.statusless_keys == {f"{GADGET}#6"}


def test_run_sweep_passes_target_board_keys_to_the_store(tmp_path, monkeypatch):
    issues, ts, hs, tcfg, repos = _sweep_ctx(tmp_path)
    seen = {}
    real = hs.record_sweep

    def spy(findings, **kw):
        seen.update(kw)
        return real(findings, **kw)

    monkeypatch.setattr(hs, "record_sweep", spy)
    inputs = hw.OffboardInputs(
        open_by_repo={GADGET: {f"{GADGET}#8"}},
        board_by_repo={GADGET: {f"{GADGET}#1"}, REPO: {f"{GADGET}#8", f"{REPO}#7"}},
        targets={GADGET: _LOGGER_TARGET},
    )
    hw.run_sweep(issues, ts, hs, tcfg, repos, runner=FakeRunner([_ok()]), now=NOW, offboard=inputs)
    # Only keys on their OWN repo's board count as landed.
    assert seen["offboard_board_keys"] == {f"{GADGET}#1", f"{REPO}#7"}


def test_offboard_membership_is_case_insensitive(monkeypatch):
    """Review round 9: the projects.yaml key and GitHub's nameWithOwner can
    differ in case; an exact match would report every issue off-board and
    Add all would reset Status on the whole board."""
    from agent_hq.config import Config, DispatchConfig, ProjectConfig

    cfg = Config(
        repos={
            "Ben-Ralston/Gadget": RepoConfig("Ben-Ralston/Gadget", Path("/tmp/l"), project="Gadget")
        },
        projects=[ProjectConfig(name="Gadget", number=13, owner="@me")],
        worktree_root=Path("/tmp/wt"),
        dispatch=DispatchConfig(),
    )
    cache = _BoardCache({(13, "@me"): [_issue(1, repo=GADGET), _issue(2, repo=GADGET)]})
    rows = dict.fromkeys(["Ben-Ralston/Gadget#1", "Ben-Ralston/Gadget#2", "Ben-Ralston/Gadget#8"])
    monkeypatch.setattr(hw, "open_issue_rows", lambda repo: rows)
    inputs = hw.collect_offboard_inputs(cfg, cache)
    assert [f.issue_keys for f in hw.detect_offboard(inputs)] == [["Ben-Ralston/Gadget#8"]]
    assert hw._own_board_keys(inputs) == {f"{GADGET}#1", f"{GADGET}#2"}


def test_collect_offboard_inputs_canonicalizes_the_repo_to_the_boards_case(monkeypatch):
    """Review round 10: the board's ``Issue.repo`` (GitHub's nameWithOwner)
    is the case every other consumer keys on — the index sync, finding keys
    and the chip filter must use it, not the projects.yaml spelling."""
    from agent_hq.config import Config, DispatchConfig, ProjectConfig

    cfg = Config(
        repos={
            "Ben-Ralston/Gadget": RepoConfig("Ben-Ralston/Gadget", Path("/tmp/l"), project="Gadget")
        },
        projects=[ProjectConfig(name="Gadget", number=13, owner="@me")],
        worktree_root=Path("/tmp/wt"),
        dispatch=DispatchConfig(),
    )
    cache = _BoardCache({(13, "@me"): [_issue(1, repo=GADGET)]})
    asked = []
    monkeypatch.setattr(
        hw, "open_issue_rows",
        lambda repo: asked.append(repo) or dict.fromkeys([f"{repo}#1", f"{repo}#8"]),
    )
    inputs = hw.collect_offboard_inputs(cfg, cache)
    assert asked == [GADGET]
    assert set(inputs.open_by_repo) == {GADGET} and set(inputs.targets) == {GADGET}
    assert [f.issue_keys for f in hw.detect_offboard(inputs)] == [[f"{GADGET}#8"]]
