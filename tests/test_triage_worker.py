"""Tests for the AI triage worker (#248). The claude -p boundary is always injected."""

import json
from pathlib import Path

import pytest

from agent_hq import triage_worker as tw
from agent_hq.config import RepoConfig, TriageConfig
from agent_hq.models import Issue, Priority
from agent_hq.triage_store import (
    AgentFitness,
    Effort,
    TriageRecord,
    TriageState,
    TriageStore,
)

REPO = "ben-ralston/agent-hq"
FIXTURES = Path(__file__).parent / "fixtures" / "claude_p"


def _issue(number, *, repo=REPO, title=None, body="", labels=None, priority=Priority.NO_PRI):
    return Issue(
        number=number,
        title=title or f"issue {number}",
        repo=repo,
        status="Todo",
        priority=priority,
        body=body,
        labels=labels or [],
    )


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


# ---- body_hash ----


def test_body_hash_is_stable_and_16_hex():
    h = tw.body_hash(_issue(1, title="t", body="b", labels=["x", "y"]))
    assert h == tw.body_hash(
        _issue(1, title="t", body="b", labels=["y", "x"])
    )  # label order irrelevant
    assert len(h) == 16 and int(h, 16) >= 0


def test_body_hash_changes_with_title_body_or_labels():
    base = tw.body_hash(_issue(1, title="t", body="b", labels=["x"]))
    assert tw.body_hash(_issue(1, title="t2", body="b", labels=["x"])) != base
    assert tw.body_hash(_issue(1, title="t", body="b2", labels=["x"])) != base
    assert tw.body_hash(_issue(1, title="t", body="b", labels=["x", "z"])) != base


def test_body_hash_tolerates_a_none_body():
    """build_prompt already normalises ``issue.body or ""`` — hash the same
    thing so a body-less issue can't blow up the selection pass."""
    assert tw.body_hash(_issue(1, title="t", body=None)) == tw.body_hash(
        _issue(1, title="t", body="")
    )


# ---- select_targets ----


def _rec(number, state, *, body_hash=None, repo=REPO, tombstoned=False):
    r = TriageRecord(issue_key=f"{repo}#{number}", state=state, body_hash=body_hash, updated_at="t")
    if state is not TriageState.UNTRIAGED:
        r.value, r.effort, r.agent_fitness = 3, Effort.M, AgentFitness.PAIR
    if tombstoned:
        r.tombstoned_at = "t"
    return r


def test_select_targets_untriaged_and_missing_records_always():
    issues = [_issue(1), _issue(2)]
    targets = tw.select_targets(issues, [_rec(1, TriageState.UNTRIAGED)], cap=10)
    assert [i.number for i in targets[REPO]] == [1, 2]  # 2 has no record yet → untriaged


def test_select_targets_proposed_only_when_hash_changed():
    i1, i2 = _issue(1, body="same"), _issue(2, body="new body")
    recs = [
        _rec(1, TriageState.PROPOSED, body_hash=tw.body_hash(i1)),
        _rec(2, TriageState.PROPOSED, body_hash="0000000000000000"),
    ]
    targets = tw.select_targets([i1, i2], recs, cap=10)
    assert [i.number for i in targets[REPO]] == [2]


def test_select_targets_skips_confirmed_and_tombstoned():
    recs = [
        _rec(1, TriageState.CONFIRMED, body_hash="stale"),
        _rec(2, TriageState.UNTRIAGED, tombstoned=True),
    ]
    assert tw.select_targets([_issue(1), _issue(2)], recs, cap=10) == {}


def test_select_targets_caps_per_repo_and_groups_by_repo():
    issues = [_issue(n) for n in range(1, 5)] + [_issue(9, repo="o/other")]
    targets = tw.select_targets(issues, [], cap=2)
    assert [i.number for i in targets[REPO]] == [1, 2]
    assert [i.number for i in targets["o/other"]] == [9]


# ---- parse_proposals ----


def test_parse_ok_fixture():
    props = tw.parse_proposals(_fixture("triage_ok.json")["structured_output"], {67, 40})
    assert [p.number for p in props] == [67, 40]
    assert (
        props[0].value == 4
        and props[0].effort is Effort.M
        and props[0].agent_fitness is AgentFitness.PAIR
    )
    assert set(props[0].reasoning) == {"value", "effort", "agent_fitness"}


def test_parse_rejects_malformed_fixture():
    with pytest.raises(tw.TriageParseError):
        tw.parse_proposals(_fixture("triage_malformed.json")["structured_output"], {67})


@pytest.mark.parametrize(
    "bad",
    [
        {
            "proposals": [
                {
                    "number": 99,
                    "value": 3,
                    "effort": "S",
                    "agent_fitness": "solo",
                    "reasoning": {"value": "a", "effort": "b", "agent_fitness": "c"},
                }
            ]
        },  # unknown number
        {
            "proposals": [
                {
                    "number": 67,
                    "value": 3,
                    "effort": "S",
                    "agent_fitness": "solo",
                    "reasoning": {"value": "a", "effort": "b", "agent_fitness": "c"},
                }
            ]
            * 2
        },  # duplicate
        {
            "proposals": [
                {
                    "number": 67,
                    "value": 3,
                    "effort": "S",
                    "agent_fitness": "solo",
                    "reasoning": {"value": "", "effort": "b", "agent_fitness": "c"},
                }
            ]
        },  # empty reasoning
        {
            "proposals": [
                {
                    "number": 67,
                    "value": 3,
                    "effort": "S",
                    "agent_fitness": "solo",
                    "reasoning": {"value": "x" * 400, "effort": "b", "agent_fitness": "c"},
                }
            ]
        },  # oversized
        {"proposals": "nope"},
        None,
        {
            "proposals": [
                {
                    "number": 67,
                    "value": True,
                    "effort": "S",
                    "agent_fitness": "solo",
                    "reasoning": {"value": "a", "effort": "b", "agent_fitness": "c"},
                }
            ]
        },  # value is bool, not int
        {
            "proposals": [
                {
                    "number": True,
                    "value": 3,
                    "effort": "S",
                    "agent_fitness": "solo",
                    "reasoning": {"value": "a", "effort": "b", "agent_fitness": "c"},
                }
            ]
        },  # number is bool, not int
    ],
)
def test_parse_rejects_shape_violations(bad):
    with pytest.raises(tw.TriageParseError):
        tw.parse_proposals(bad, {67})


def test_parse_allows_omitted_issues():
    out = tw.parse_proposals(_fixture("triage_ok.json")["structured_output"], {67, 40, 44})
    assert [p.number for p in out] == [67, 40]  # 44 simply left for the next run


# ---- TRIAGE_OUTPUT_SCHEMA ----


def test_schema_reasoning_fields_capped_at_max_reasoning_chars():
    reasoning_props = tw.TRIAGE_OUTPUT_SCHEMA["properties"]["proposals"]["items"]["properties"][
        "reasoning"
    ]["properties"]
    for dim in ("value", "effort", "agent_fitness"):
        assert reasoning_props[dim]["maxLength"] == tw.MAX_REASONING_CHARS


def test_schema_forbids_interest_and_pin_on_proposals():
    """#267: interest and pin are human-only flags. The schema must not
    offer them to the model, and must close the object so a stray key
    can't ride in alongside the dimensions."""
    items = tw.TRIAGE_OUTPUT_SCHEMA["properties"]["proposals"]["items"]
    assert "interest" not in items["properties"]
    assert "pin" not in items["properties"]
    assert items["additionalProperties"] is False


@pytest.mark.parametrize("flag", ["interest", "pin"])
def test_parse_rejects_proposal_that_sets_a_human_only_flag(flag):
    """#267: a response that sets interest or pin rejects the WHOLE batch
    rather than silently dropping the key — the worker must never be a
    path onto the human-only flags."""
    structured = {
        "proposals": [
            {
                "number": 67,
                "value": 3,
                "effort": "S",
                "agent_fitness": "solo",
                "reasoning": {"value": "a", "effort": "b", "agent_fitness": "c"},
                flag: True,
            }
        ]
    }
    with pytest.raises(tw.TriageParseError, match=flag):
        tw.parse_proposals(structured, {67})


# ---- readme_head / build_prompt ----


def test_readme_head_truncates_and_tolerates_missing(tmp_path):
    (tmp_path / "README.md").write_text("\n".join(f"line {n}" for n in range(100)))
    head = tw.readme_head(tmp_path, max_lines=5)
    assert head.splitlines() == [f"line {n}" for n in range(5)]
    assert tw.readme_head(tmp_path / "nope") == ""
    assert tw.readme_head(None) == ""


def test_build_prompt_contains_rubric_issues_hints_and_context():
    issues = [
        _issue(67, title="dispatch view", body="B" * 5000, labels=["sentinel"]),
        _issue(40, title="filter bar"),
    ]
    records = {
        f"{REPO}#67": TriageRecord(issue_key=f"{REPO}#67", priority_hint="Need", updated_at="t")
    }
    prompt = tw.build_prompt(
        REPO, issues, records, description="Monitors agents in tmux", readme="# Agent HQ\nCLI tool"
    )
    assert (
        "ben-ralston/agent-hq" in prompt
        and "Monitors agents in tmux" in prompt
        and "# Agent HQ" in prompt
    )
    assert "#67" in prompt and "dispatch view" in prompt and "labels: sentinel" in prompt
    assert "#40" in prompt
    assert "previous priority label: Need (≈ value 4)" in prompt
    assert "B" * 2000 in prompt and "B" * 2001 not in prompt  # body capped
    for token in ("value", "effort", "agent_fitness", "solo", "pair", "needs_you", "S", "M", "L"):
        assert token in prompt
    assert "67, 40" in prompt  # explicit list of numbers to return


# ---- invoke_claude / run_triage ----


class FakeRunner:
    """Scripted stdout per call; records argv/prompt. Raise via exceptions list."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, prompt, timeout_s, cwd):
        self.calls.append((argv, prompt, timeout_s, cwd))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _ok_stdout():
    return (FIXTURES / "triage_ok.json").read_text()


def _bad_stdout():
    return (FIXTURES / "triage_malformed.json").read_text()


def _cfg(tmp_path, **kw):
    return TriageConfig(data_dir=tmp_path / "data", **kw)


def _repos(tmp_path):
    return {REPO: RepoConfig(REPO, tmp_path / "checkout", description="Monitors agents")}


def test_invoke_claude_argv_and_structured_output(tmp_path):
    runner = FakeRunner([_ok_stdout()])
    out = tw.invoke_claude("hi", model="sonnet", timeout_s=30, cwd=tmp_path, runner=runner)
    argv, prompt, timeout_s, cwd = runner.calls[0]
    assert argv[:2] == ["claude", "-p"]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"
    assert "--output-format" in argv and "json" in argv and "--json-schema" in argv
    # Pinned literally (not against the constant, which would be tautological):
    # dropping any containment flag must fail here.
    expected_flags = [
        "--output-format",
        "json",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--tools",
        "",
    ]
    assert tw.CLAUDE_HEADLESS_FLAGS == expected_flags
    start = argv.index("--output-format")
    assert argv[start : start + len(expected_flags)] == expected_flags  # contiguous, in order
    assert json.loads(argv[argv.index("--json-schema") + 1]) == tw.TRIAGE_OUTPUT_SCHEMA
    assert prompt == "hi" and timeout_s == 30 and cwd == tmp_path
    assert out["proposals"][0]["number"] == 67


def test_invoke_claude_bad_envelope_raises_invoke_error(tmp_path):
    with pytest.raises(tw.TriageInvokeError):
        tw.invoke_claude(
            "hi", model="sonnet", timeout_s=30, cwd=tmp_path, runner=FakeRunner(["not json"])
        )
    with pytest.raises(tw.TriageInvokeError):
        tw.invoke_claude(
            "hi",
            model="sonnet",
            timeout_s=30,
            cwd=tmp_path,
            runner=FakeRunner([json.dumps({"type": "result", "is_error": True, "result": "boom"})]),
        )


def test_invoke_claude_missing_structured_output_raises_invoke_error(tmp_path):
    with pytest.raises(tw.TriageInvokeError):
        tw.invoke_claude(
            "hi",
            model="sonnet",
            timeout_s=30,
            cwd=tmp_path,
            runner=FakeRunner([json.dumps({"type": "result", "is_error": False})]),
        )


def test_run_triage_writes_proposed_records_with_metadata(tmp_path):
    store = TriageStore(tmp_path / "data")
    issues = [_issue(67, title="dispatch view"), _issue(40, title="filter bar")]
    runner = FakeRunner([_ok_stdout()])
    report = tw.run_triage(
        issues,
        store,
        _cfg(tmp_path),
        _repos(tmp_path),
        runner=runner,
        now="2026-09-03T00:00:00+00:00",
    )
    assert report.proposed == 2 and report.failed == 0 and len(runner.calls) == 1
    assert runner.calls[0][3] == store.data_dir  # run from the data dir, not a worktree
    rec = store.get(f"{REPO}#67")
    assert rec.state is TriageState.PROPOSED and rec.value == 4 and rec.effort is Effort.M
    assert rec.provenance == {"value": "ai", "effort": "ai", "agent_fitness": "ai"}
    assert rec.reasoning["agent_fitness"].startswith("Agent builds")
    assert rec.model == _cfg(tmp_path).ai_model  # the configured model, whatever the default
    assert rec.proposed_at == "2026-09-03T00:00:00+00:00"
    assert rec.body_hash == tw.body_hash(issues[0])


def test_run_triage_confirmed_record_is_untouchable_even_if_model_mentions_it(tmp_path):
    """#67 is human-confirmed -> not in the batch. The fixture still returns a
    #67 entry, so the parser rejects the batch (unknown number) twice and
    nothing is written - the human value survives and the failure is visible."""
    store = TriageStore(tmp_path / "data")
    store.upsert(
        TriageRecord(
            issue_key=f"{REPO}#67",
            value=1,
            effort=Effort.S,
            agent_fitness=AgentFitness.SOLO,
            updated_at="t",
        ),
        actor="human",
    )
    store.confirm(f"{REPO}#67", now="t")
    runner = FakeRunner([_ok_stdout(), _ok_stdout()])
    report = tw.run_triage(
        [_issue(67), _issue(40)], store, _cfg(tmp_path), _repos(tmp_path), runner=runner
    )
    assert report.repos[0].targeted == 1 and len(runner.calls) == 2
    assert report.failed == 1 and report.proposed == 0
    assert store.get(f"{REPO}#67").value == 1
    assert store.get(f"{REPO}#67").state is TriageState.CONFIRMED
    assert store.get(f"{REPO}#40") is None


def test_run_triage_retries_once_on_parse_error_then_fails_visibly(tmp_path, monkeypatch):
    emitted = []
    monkeypatch.setattr(
        tw.eventlog,
        "emit",
        lambda level, msg, extra=None, **kw: emitted.append((level, msg, extra)),
    )
    store = TriageStore(tmp_path / "data")
    runner = FakeRunner([_bad_stdout(), _bad_stdout()])
    report = tw.run_triage([_issue(67)], store, _cfg(tmp_path), _repos(tmp_path), runner=runner)
    assert len(runner.calls) == 2
    assert "rejected" in runner.calls[1][1]  # retry prompt carries the parser error
    assert report.failed == 1 and report.proposed == 0
    assert store.get(f"{REPO}#67") is None  # nothing written
    assert ("error", "triage worker output rejected") in [(lvl, m) for lvl, m, _ in emitted]


def test_run_triage_retry_success_writes(tmp_path):
    store = TriageStore(tmp_path / "data")
    runner = FakeRunner([_bad_stdout(), _ok_stdout()])
    report = tw.run_triage(
        [_issue(67), _issue(40)], store, _cfg(tmp_path), _repos(tmp_path), runner=runner
    )
    assert report.proposed == 2 and report.failed == 0


def test_run_triage_invoke_error_no_retry(tmp_path, monkeypatch):
    emitted = []
    monkeypatch.setattr(
        tw.eventlog, "emit", lambda level, msg, extra=None, **kw: emitted.append((level, msg))
    )
    store = TriageStore(tmp_path / "data")
    runner = FakeRunner([tw.TriageInvokeError("claude exited 1")])
    report = tw.run_triage([_issue(67)], store, _cfg(tmp_path), _repos(tmp_path), runner=runner)
    assert len(runner.calls) == 1 and report.failed == 1
    assert ("warn", "triage worker invoke failed") in emitted


def test_run_triage_dry_run_writes_nothing_but_reports_proposals(tmp_path):
    store = TriageStore(tmp_path / "data")
    runner = FakeRunner([_ok_stdout()])
    report = tw.run_triage(
        [_issue(67), _issue(40)],
        store,
        _cfg(tmp_path),
        _repos(tmp_path),
        runner=runner,
        dry_run=True,
    )
    assert report.dry_run and report.proposed == 2
    assert [p.number for p in report.repos[0].proposals] == [67, 40]
    assert store.all_records() == []


def _empty_stdout():
    """A well-formed envelope whose schema-valid output proposes nothing."""
    envelope = json.loads(_ok_stdout())
    envelope["structured_output"] = {"proposals": []}
    return json.dumps(envelope)


def test_run_triage_empty_proposals_is_rejected_not_silent_success(tmp_path, monkeypatch):
    """Final-review finding 1: a schema-valid but EMPTY proposals list is zero
    progress — the same targets are selected next tick, so a web-hosted run
    would re-kick forever. Treat it as a parse error: retry once, then fail
    visibly (which trips the caller's cooldown) and write nothing."""
    emitted = []
    monkeypatch.setattr(
        tw.eventlog,
        "emit",
        lambda level, msg, extra=None, **kw: emitted.append((level, msg, extra)),
    )
    store = TriageStore(tmp_path / "data")
    runner = FakeRunner([_empty_stdout(), _empty_stdout()])
    report = tw.run_triage(
        [_issue(67), _issue(40)], store, _cfg(tmp_path), _repos(tmp_path), runner=runner
    )
    assert len(runner.calls) == 2  # retried once
    assert report.failed == 1 and report.proposed == 0
    assert store.all_records() == []  # nothing written
    assert ("error", "triage worker output rejected") in [(lvl, m) for lvl, m, _ in emitted]


def test_run_triage_partial_proposals_still_succeed(tmp_path):
    """Guard the boundary of the fix above: the model may legitimately omit
    SOME issues (they're retried next run) — only an empty batch is rejected."""
    envelope = json.loads(_ok_stdout())
    envelope["structured_output"]["proposals"] = envelope["structured_output"]["proposals"][:1]
    store = TriageStore(tmp_path / "data")
    runner = FakeRunner([json.dumps(envelope)])
    report = tw.run_triage(
        [_issue(67), _issue(40)], store, _cfg(tmp_path), _repos(tmp_path), runner=runner
    )
    assert len(runner.calls) == 1 and report.proposed == 1 and report.failed == 0


def test_run_triage_no_targets_makes_no_calls(tmp_path):
    store = TriageStore(tmp_path / "data")
    store.upsert(
        TriageRecord(
            issue_key=f"{REPO}#67",
            value=3,
            effort=Effort.S,
            agent_fitness=AgentFitness.SOLO,
            updated_at="t",
        ),
        actor="human",
    )
    store.confirm(f"{REPO}#67", now="t")
    runner = FakeRunner([])
    report = tw.run_triage([_issue(67)], store, _cfg(tmp_path), _repos(tmp_path), runner=runner)
    assert runner.calls == [] and report.repos == []


def test_run_triage_only_repo_and_cap(tmp_path):
    store = TriageStore(tmp_path / "data")
    issues = [_issue(67), _issue(40), _issue(9, repo="o/other")]
    # cap=1 trims the repo's batch to #67; the fixture answers for #67 AND #40
    # so that batch is then rejected and retried — irrelevant here, this pins
    # selection (both calls are scripted so the retry can't IndexError).
    runner = FakeRunner([_ok_stdout(), _ok_stdout()])
    report = tw.run_triage(
        issues, store, _cfg(tmp_path), _repos(tmp_path), runner=runner, only_repo=REPO, cap=1
    )
    assert [r.repo for r in report.repos] == [REPO]  # o/other filtered out
    assert report.repos[0].targeted == 1
    assert "Return proposals for numbers: 67\n" in runner.calls[0][1]  # #40 capped out


def test_run_triage_prompt_includes_readme_from_repo_config(tmp_path):
    (tmp_path / "checkout").mkdir()
    (tmp_path / "checkout" / "README.md").write_text("# Agent HQ readme")
    store = TriageStore(tmp_path / "data")
    runner = FakeRunner([_ok_stdout()])
    tw.run_triage([_issue(67), _issue(40)], store, _cfg(tmp_path), _repos(tmp_path), runner=runner)
    assert "# Agent HQ readme" in runner.calls[0][1] and "Monitors agents" in runner.calls[0][1]


def test_run_triage_unwritable_store_reports_write_failure(tmp_path, monkeypatch):
    """A skipped write (upsert -> None) must be reported, not counted as
    proposed, and must stop the batch rather than plough on."""
    emitted = []
    monkeypatch.setattr(
        tw.eventlog, "emit", lambda level, msg, extra=None, **kw: emitted.append((level, msg))
    )
    store = TriageStore(tmp_path / "data")
    monkeypatch.setattr(TriageStore, "upsert", lambda self, record, *, actor: None)
    runner = FakeRunner([_ok_stdout()])
    report = tw.run_triage(
        [_issue(67), _issue(40)], store, _cfg(tmp_path), _repos(tmp_path), runner=runner
    )
    assert report.proposed == 0 and report.failed == 1
    assert report.repos[0].error == "write: repo file unreadable"
    assert ("warn", "triage write failed") in emitted


def test_default_runner_wraps_os_error_as_invoke_error(tmp_path, monkeypatch):
    """A missing/unusable cwd raises OSError from subprocess — it must surface
    as TriageInvokeError so run_triage's per-repo error path handles it."""

    def _boom(*a, **kw):
        raise FileNotFoundError("cwd")

    monkeypatch.setattr(tw.shutil, "which", lambda _: "/x/claude")
    monkeypatch.setattr(tw.subprocess, "run", _boom)
    with pytest.raises(tw.TriageInvokeError):
        tw.default_runner(["claude", "-p"], "hi", 5, tmp_path / "missing")


def test_run_triage_write_exception_is_reported_not_raised(tmp_path, monkeypatch):
    """An OSError escaping upsert (e.g. _atomic_write_json) must land in the
    report, not propagate out of the poll loop."""
    emitted = []
    monkeypatch.setattr(
        tw.eventlog, "emit", lambda level, msg, extra=None, **kw: emitted.append((level, msg))
    )
    store = TriageStore(tmp_path / "data")

    def _boom(record, *, actor):
        raise OSError("disk full")

    monkeypatch.setattr(store, "upsert", _boom)
    runner = FakeRunner([_ok_stdout()])
    report = tw.run_triage(
        [_issue(67), _issue(40)], store, _cfg(tmp_path), _repos(tmp_path), runner=runner
    )
    assert report.failed == 1 and report.proposed == 0
    assert "disk full" in report.repos[0].error
    assert ("warn", "triage write failed") in emitted


def test_invoke_claude_accepts_a_custom_schema(tmp_path):
    runner = FakeRunner([_ok_stdout()])
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    tw.invoke_claude("hi", model="opus", timeout_s=5, cwd=tmp_path, runner=runner, schema=schema)
    argv = runner.calls[0][0]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema
