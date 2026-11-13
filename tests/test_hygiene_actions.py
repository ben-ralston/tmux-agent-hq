"""hygiene_actions (#249): one plan renders the preview AND drives execution."""

from agent_hq import hygiene_actions as ha
from agent_hq.github import GHError
from agent_hq.hygiene_store import Finding, FindingType
from agent_hq.models import Issue, Priority
from agent_hq.triage_store import TriageRecord, TriageStore

REPO = "ben-ralston/agent-hq"
NOW = "2026-09-12T10:00:00+00:00"


def _issue(n, title=None, labels=()):
    return Issue(number=n, title=title or f"issue {n}", repo=REPO, status="Todo",
                 priority=Priority.IDEA, labels=list(labels))


class _Cache:
    def __init__(self):
        self.closed = []
        self.closed_keys = set()

    def mark_closed(self, repo, number):
        self.closed.append((repo, number))

    def get_closed_keys_cached(self, repos):
        return set(self.closed_keys), True


class _Gh:
    """Records every GitHub write; ``fail`` names the call that returns False/raises."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def comment(self, repo, number, body):
        self.calls.append(("comment", repo, number, body))
        return self.fail != "comment"

    def close(self, repo, number):
        self.calls.append(("close", repo, number))
        return self.fail != "close"

    def create(self, repo, title, body, labels=None):
        self.calls.append(("create", repo, title, body, labels))
        if self.fail == "create":
            raise GHError("boom")
        return f"https://github.com/{repo}/issues/900"


def _wire(monkeypatch, gh):
    monkeypatch.setattr(ha, "comment_issue", gh.comment)
    monkeypatch.setattr(ha, "close_issue_not_planned", gh.close)
    monkeypatch.setattr(ha, "create_issue", gh.create)


def _plan(monkeypatch, finding, issues, gh=None, store=None):
    gh = gh or _Gh()
    _wire(monkeypatch, gh)
    cache = _Cache()
    steps = ha.plan_steps(
        finding,
        issues_by_key={i.key: i for i in issues},
        github_cache=cache,
        triage_store=store,
        now=NOW,
    )
    return steps, gh, cache


def _dup():
    return Finding.new(FindingType.DUPLICATE, [f"{REPO}#1", f"{REPO}#2"],
                       action={
                           "kind": "close_as_duplicate",
                           "close": f"{REPO}#2",
                           "of": f"{REPO}#1",
                       },
                       reasoning="same")


def test_split_key():
    assert ha.split_key(f"{REPO}#12") == (REPO, 12)


def test_duplicate_plan_labels_and_execution(monkeypatch):
    steps, gh, cache = _plan(monkeypatch, _dup(), [_issue(1), _issue(2)])
    assert [s.label for s in steps] == [
        "Comment on agent-hq#2: Duplicate of #1 (closed by the agent-hq hygiene sweep)",
        "Close agent-hq#2 as not planned",
    ]
    result = ha.execute(_dup(), steps, now=NOW)
    assert result["ok"] is True and result["at"] == NOW
    assert [s["ok"] for s in result["steps"]] == [True, True]
    assert gh.calls == [
        ("comment", REPO, 2, "Duplicate of #1 (closed by the agent-hq hygiene sweep)"),
        ("close", REPO, 2),
    ]
    assert cache.closed == [(REPO, 2)]


def test_execution_stops_at_first_failure(monkeypatch):
    steps, gh, cache = _plan(monkeypatch, _dup(), [_issue(1), _issue(2)], gh=_Gh(fail="comment"))
    result = ha.execute(_dup(), steps, now=NOW)
    assert result["ok"] is False
    assert [s["ok"] for s in result["steps"]] == [False]
    assert result["steps"][0]["detail"]
    assert gh.calls == [
        ("comment", REPO, 2, "Duplicate of #1 (closed by the agent-hq hygiene sweep)")
    ]
    assert cache.closed == []


def test_stale_plan(monkeypatch):
    f = Finding.new(FindingType.STALE, [f"{REPO}#8"],
                    action={"kind": "close_stale", "close": f"{REPO}#8",
                            "last_activity": "2026-01-01T00:00:00Z", "value": 1},
                    reasoning="old")
    steps, gh, cache = _plan(monkeypatch, f, [_issue(8)])
    assert [s.label for s in steps] == [
        "Comment on agent-hq#8: Closing as stale: no activity since 2026-01-01, value 1 "
        "(agent-hq hygiene sweep)",
        "Close agent-hq#8 as not planned",
    ]
    assert ha.execute(f, steps, now=NOW)["ok"] is True
    assert cache.closed == [(REPO, 8)]


def test_cluster_plan_threads_the_umbrella_number(monkeypatch):
    f = Finding.new(FindingType.CLUSTER, [f"{REPO}#3", f"{REPO}#4"],
                    action={"kind": "merge_cluster", "repo": REPO, "umbrella_title": "Booms",
                            "children": [f"{REPO}#3", f"{REPO}#4"]},
                    reasoning="one cause")
    issues = [
        _issue(3, "[sentinel] boom", ["sentinel"]),
        _issue(4, "[sentinel] boom 2", ["sentinel"]),
    ]
    steps, gh, cache = _plan(monkeypatch, f, issues)
    assert [s.label for s in steps] == [
        f"Create issue in {REPO}: Booms (label sentinel, body lists #3, #4)",
        "Comment on agent-hq#3: Tracked by the umbrella issue created above",
        "Close agent-hq#3 as not planned",
        "Comment on agent-hq#4: Tracked by the umbrella issue created above",
        "Close agent-hq#4 as not planned",
    ]
    result = ha.execute(f, steps, now=NOW)
    assert result["ok"] is True
    create = gh.calls[0]
    assert create[:3] == ("create", REPO, "Booms") and create[4] == ["sentinel"]
    assert "#3" in create[3] and "[sentinel] boom 2" in create[3]
    assert ("comment", REPO, 3, "Tracked by #900 (agent-hq hygiene sweep)") in gh.calls
    assert ("comment", REPO, 4, "Tracked by #900 (agent-hq hygiene sweep)") in gh.calls
    assert cache.closed == [(REPO, 3), (REPO, 4)]
    assert "#900" in result["steps"][0]["detail"]


def test_cluster_create_failure_stops_everything(monkeypatch):
    f = Finding.new(FindingType.CLUSTER, [f"{REPO}#3", f"{REPO}#4"],
                    action={"kind": "merge_cluster", "repo": REPO, "umbrella_title": "Booms",
                            "children": [f"{REPO}#3", f"{REPO}#4"]},
                    reasoning="x")
    issues = [_issue(3, labels=["sentinel"]), _issue(4, labels=["sentinel"])]
    steps, gh, cache = _plan(monkeypatch, f, issues, gh=_Gh(fail="create"))
    result = ha.execute(f, steps, now=NOW)
    assert result["ok"] is False and len(result["steps"]) == 1
    assert "boom" in result["steps"][0]["detail"]
    assert gh.calls == [("create", REPO, "Booms", gh.calls[0][3], ["sentinel"])]


def test_mismatch_plan_writes_human_value(monkeypatch, tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(TriageRecord(issue_key=f"{REPO}#5", value=2, updated_at="t"), actor="ai")
    f = Finding.new(FindingType.MISMATCH, [f"{REPO}#5"],
                    action={"kind": "bump_value", "key": f"{REPO}#5", "current": 2, "proposed": 5},
                    reasoning="security")
    steps, gh, cache = _plan(monkeypatch, f, [_issue(5)], store=store)
    assert [s.label for s in steps] == ["Set value 5 on agent-hq#5 (was 2)"]
    assert ha.execute(f, steps, now=NOW)["ok"] is True
    rec = store.get(f"{REPO}#5")
    assert rec.value == 5 and rec.provenance["value"] == "human"
    assert gh.calls == []


def test_plan_records_a_gap_when_the_issue_is_gone(monkeypatch):
    steps, gh, cache = _plan(monkeypatch, _dup(), [_issue(1)])  # #2 closed since
    assert len(steps) == 1 and "no longer open" in steps[0].label
    result = ha.execute(_dup(), steps, now=NOW)
    assert result["ok"] is False and gh.calls == []


def test_cluster_retry_reuses_the_umbrella_from_the_prior_attempt(monkeypatch):
    """Review P1: after create succeeded and a later step failed, a retry must
    not open a second umbrella."""
    f = Finding.new(
        FindingType.CLUSTER,
        [f"{REPO}#3", f"{REPO}#4"],
        action={
            "kind": "merge_cluster",
            "repo": REPO,
            "umbrella_title": "Booms",
            "children": [f"{REPO}#3", f"{REPO}#4"],
        },
        reasoning="x",
    )
    issues = [_issue(3, labels=["sentinel"]), _issue(4, labels=["sentinel"])]
    steps, gh, cache = _plan(monkeypatch, f, issues, gh=_Gh(fail="comment"))
    first = ha.execute(f, steps, now=NOW)
    assert first["ok"] is False and first["umbrella"] == 900
    assert [c[0] for c in gh.calls] == ["create", "comment"]
    gh2 = _Gh()
    _wire(monkeypatch, gh2)
    steps = ha.plan_steps(
        f,
        issues_by_key={i.key: i for i in issues},
        github_cache=cache,
        triage_store=None,
        now=NOW,
        prior_execution=first,
    )
    assert steps[0].label == "Reuse umbrella #900 created by the previous attempt"
    second = ha.execute(f, steps, now=NOW, prior_execution=first)
    assert second["ok"] is True and second["umbrella"] == 900
    assert [c[0] for c in gh2.calls] == ["comment", "close", "comment", "close"]  # no create
    assert ("comment", REPO, 3, "Tracked by #900 (agent-hq hygiene sweep)") in gh2.calls



# ---- hand-test + Codex round (2026-09-12) ----


def test_cross_repo_duplicate_comment_names_the_other_repo(monkeypatch):
    """Codex P2: a duplicate pair spanning repos must link ``owner/repo#N``,
    not a bare ``#N`` that resolves inside the wrong repo."""
    other = "ben-ralston/gizmos#12"
    f = Finding.new(FindingType.DUPLICATE, [other, f"{REPO}#2"],
                    action={"kind": "close_as_duplicate", "close": f"{REPO}#2", "of": other},
                    reasoning="same")
    keep = Issue(number=12, title="k", repo="ben-ralston/gizmos",
                 status="Todo", priority=Priority.IDEA)
    steps, gh, cache = _plan(monkeypatch, f, [_issue(2), keep])
    ha.execute(f, steps, now=NOW)
    assert gh.calls[0][:3] == ("comment", REPO, 2)
    assert gh.calls[0][3].startswith("Duplicate of ben-ralston/gizmos#12 ")
    # Same-repo pairs keep the short form.
    steps, gh, cache = _plan(monkeypatch, _dup(), [_issue(1), _issue(2)])
    ha.execute(_dup(), steps, now=NOW)
    assert gh.calls[0][3].startswith("Duplicate of #1 ")


def _cluster(children):
    return Finding.new(FindingType.CLUSTER, list(children),
                       action={"kind": "merge_cluster", "repo": REPO, "umbrella_title": "Booms",
                               "children": list(children)},
                       reasoning="one cause")


def test_cluster_plan_skips_children_already_closed(monkeypatch):
    """Hand-test: two of four children were closed by earlier duplicate
    accepts. The umbrella still makes sense for the two that remain — closed
    children become non-actionable "skipping" steps, not a dead plan."""
    f = _cluster([f"{REPO}#3", f"{REPO}#4", f"{REPO}#5", f"{REPO}#6"])
    issues = [_issue(3, labels=["sentinel"]), _issue(6, labels=["sentinel"])]  # 4, 5 closed
    steps, gh, cache = _plan(monkeypatch, f, issues)
    labels = [s.label for s in steps]
    assert labels[0] == f"Create issue in {REPO}: Booms (label sentinel, body lists #3, #6)"
    assert "agent-hq#4 is already closed — skipping" in labels
    assert "agent-hq#5 is already closed — skipping" in labels
    assert [s.actionable for s in steps].count(False) == 2
    result = ha.execute(f, steps, now=NOW)
    assert result["ok"] is True
    assert [c[:3] for c in gh.calls] == [
        ("create", REPO, "Booms"),
        ("comment", REPO, 3), ("close", REPO, 3),
        ("comment", REPO, 6), ("close", REPO, 6),
    ]
    assert "#4" not in gh.calls[0][3] and "#3" in gh.calls[0][3]


def test_cluster_plan_with_fewer_than_two_open_children_is_a_gap(monkeypatch):
    f = _cluster([f"{REPO}#3", f"{REPO}#4", f"{REPO}#5"])
    steps, gh, cache = _plan(monkeypatch, f, [_issue(3, labels=["sentinel"])])
    assert len(steps) == 1 and not steps[0].actionable
    assert "fewer than two" in steps[0].label
    assert ha.execute(f, steps, now=NOW)["ok"] is False and gh.calls == []


def test_gap_steps_are_not_actionable(monkeypatch):
    steps, gh, cache = _plan(monkeypatch, _dup(), [_issue(1)])
    assert steps[0].actionable is False
    steps, gh, cache = _plan(monkeypatch, _dup(), [_issue(1), _issue(2)])
    assert all(s.actionable for s in steps)


def test_cluster_retry_resumes_past_children_closed_by_the_first_attempt(monkeypatch):
    """Codex P2: after child #3 was commented + closed and the comment on #4
    failed, the retry must skip #3 (now closed) and finish #4 — not report
    the whole plan as a gap."""
    f = _cluster([f"{REPO}#3", f"{REPO}#4"])
    issues = [_issue(3, labels=["sentinel"]), _issue(4, labels=["sentinel"])]

    class _GhSecondCommentFails(_Gh):
        def comment(self, repo, number, body):
            self.calls.append(("comment", repo, number, body))
            return number != 4

    steps, gh, cache = _plan(monkeypatch, f, issues, gh=_GhSecondCommentFails())
    first = ha.execute(f, steps, now=NOW)
    assert first["ok"] is False and cache.closed == [(REPO, 3)]
    gh2 = _Gh()
    _wire(monkeypatch, gh2)
    steps = ha.plan_steps(
        f,
        issues_by_key={issues[1].key: issues[1]},  # #3 dropped from the open set
        github_cache=cache,
        triage_store=None,
        now=NOW,
        prior_execution=first,
    )
    labels = [s.label for s in steps]
    assert labels[0] == "Reuse umbrella #900 created by the previous attempt"
    assert "agent-hq#3 is already closed — skipping" in labels
    second = ha.execute(f, steps, now=NOW, prior_execution=first)
    assert second["ok"] is True
    assert [c[:3] for c in gh2.calls] == [("comment", REPO, 4), ("close", REPO, 4)]


def test_execute_reports_each_step_to_on_step(monkeypatch):
    steps, gh, cache = _plan(monkeypatch, _dup(), [_issue(1), _issue(2)])
    seen = []
    ha.execute(
        _dup(), steps, now=NOW, on_step=lambda log, facts: seen.append([s["ok"] for s in log])
    )
    assert seen == [[True], [True, True]]
    steps, gh, cache = _plan(monkeypatch, _dup(), [_issue(1), _issue(2)], gh=_Gh(fail="comment"))
    seen = []
    ha.execute(_dup(), steps, now=NOW, on_step=lambda log, facts: seen.append(len(log)))
    assert seen == [1]  # stopped at the first failure


def test_execute_survives_a_failing_on_step(monkeypatch):
    steps, gh, cache = _plan(monkeypatch, _dup(), [_issue(1), _issue(2)])

    def boom(log, facts):
        raise OSError("disk")

    assert ha.execute(_dup(), steps, now=NOW, on_step=boom)["ok"] is True


# ---- Codex round 2 (2026-09-13): revalidate before closing ----


def test_duplicate_plan_is_a_gap_when_the_keeper_closed(monkeypatch):
    """Codex P2: closing the last open member of a pair because the keeper
    went away would delete the tracking issue, not the duplicate."""
    steps, gh, cache = _plan(monkeypatch, _dup(), [_issue(2)])  # keeper #1 closed
    assert len(steps) == 1 and not steps[0].actionable and "#1" in steps[0].label
    assert ha.execute(_dup(), steps, now=NOW)["ok"] is False and gh.calls == []


def _stale(value=1):
    return Finding.new(FindingType.STALE, [f"{REPO}#8"],
                       action={"kind": "close_stale", "close": f"{REPO}#8",
                               "last_activity": "2026-01-01T00:00:00Z", "value": value},
                       reasoning="old")


def test_stale_plan_refuses_an_issue_with_newer_activity(monkeypatch):
    issue = Issue(number=8, title="issue 8", repo=REPO, status="Todo", priority=Priority.IDEA,
                  updated_at="2026-08-01T00:00:00Z")
    steps, gh, cache = _plan(monkeypatch, _stale(), [issue])
    assert len(steps) == 1 and not steps[0].actionable
    assert "activity" in steps[0].label and "2026-08-01" in steps[0].label
    assert ha.execute(_stale(), steps, now=NOW)["ok"] is False and gh.calls == []


def test_stale_plan_refuses_an_issue_whose_value_was_raised(monkeypatch, tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(TriageRecord(issue_key=f"{REPO}#8", value=4, updated_at=NOW), actor="human")
    issue = Issue(number=8, title="issue 8", repo=REPO, status="Todo", priority=Priority.IDEA,
                  updated_at="2026-01-01T00:00:00Z")
    steps, gh, cache = _plan(monkeypatch, _stale(), [issue], store=store)
    assert len(steps) == 1 and not steps[0].actionable and "value" in steps[0].label
    # Unchanged activity and value: still actionable.
    store.upsert(TriageRecord(issue_key=f"{REPO}#8", value=1, updated_at=NOW), actor="human")
    steps, gh, cache = _plan(monkeypatch, _stale(), [issue], store=store)
    assert len(steps) == 2 and all(s.actionable for s in steps)


def test_cluster_retry_refuses_when_the_prior_umbrella_was_closed(monkeypatch):
    """Codex round 5: a retry must not close the remaining children under an
    umbrella someone closed between attempts — nothing would track them."""
    f = _cluster([f"{REPO}#3", f"{REPO}#4"])
    issues = [_issue(3, labels=["sentinel"]), _issue(4, labels=["sentinel"])]
    steps, gh, cache = _plan(monkeypatch, f, issues, gh=_Gh(fail="comment"))
    first = ha.execute(f, steps, now=NOW)
    assert first["umbrella"] == 900
    cache.closed_keys = {f"{REPO}#900"}
    gh2 = _Gh()
    _wire(monkeypatch, gh2)
    steps = ha.plan_steps(
        f,
        issues_by_key={i.key: i for i in issues},
        github_cache=cache,
        triage_store=None,
        now=NOW,
        prior_execution=first,
    )
    assert len(steps) == 1 and not steps[0].actionable
    assert "umbrella #900" in steps[0].label and "closed" in steps[0].label
    assert gh2.calls == []


def test_mismatch_plan_refuses_when_the_issue_is_no_longer_open(monkeypatch, tmp_path):
    """Codex round 5: the forced refetch dropped the issue; the value bump
    must not land on a closed (soon tombstoned) record."""
    store = TriageStore(tmp_path)
    store.upsert(TriageRecord(issue_key=f"{REPO}#5", value=2, updated_at=NOW), actor="human")
    f = Finding.new(FindingType.MISMATCH, [f"{REPO}#5"],
                    action={"kind": "bump_value", "key": f"{REPO}#5", "current": 2, "proposed": 5},
                    reasoning="security")
    steps, gh, cache = _plan(monkeypatch, f, [], store=store)
    assert len(steps) == 1 and not steps[0].actionable and "no longer open" in steps[0].label
    assert store.get(f"{REPO}#5").value == 2


def test_mismatch_plan_refuses_when_the_value_moved_since(monkeypatch, tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(TriageRecord(issue_key=f"{REPO}#5", value=3, updated_at=NOW), actor="human")
    f = Finding.new(FindingType.MISMATCH, [f"{REPO}#5"],
                    action={"kind": "bump_value", "key": f"{REPO}#5", "current": 2, "proposed": 5},
                    reasoning="security")
    steps, gh, cache = _plan(monkeypatch, f, [_issue(5)], store=store)
    assert len(steps) == 1 and not steps[0].actionable and "changed" in steps[0].label


def test_stale_retry_skips_its_own_comment_and_ignores_that_activity(monkeypatch):
    """Codex round 3: the first attempt's comment advanced updated_at; the
    retry must not read it as new activity, and must not comment twice."""
    issue_then = Issue(number=8, title="issue 8", repo=REPO, status="Todo",
                       priority=Priority.IDEA, updated_at="2026-01-01T00:00:00Z")
    steps, gh, cache = _plan(monkeypatch, _stale(), [issue_then], gh=_Gh(fail="close"))
    first = ha.execute(_stale(), steps, now=NOW)
    assert first["ok"] is False and first["commented_at"] == NOW
    # GitHub now shows our comment as the last activity (seconds after `at`).
    issue_now = Issue(number=8, title="issue 8", repo=REPO, status="Todo",
                      priority=Priority.IDEA, updated_at="2026-09-12T10:00:20+00:00")
    gh2 = _Gh()
    _wire(monkeypatch, gh2)
    steps = ha.plan_steps(_stale(), issues_by_key={issue_now.key: issue_now}, github_cache=cache,
                          triage_store=None, now=NOW, prior_execution=first)
    assert [s.actionable for s in steps] == [False, True]
    assert "already posted" in steps[0].label
    second = ha.execute(_stale(), steps, now=NOW, prior_execution=first)
    assert second["ok"] is True and [c[0] for c in gh2.calls] == ["close"]
    # Activity well after the attempt is still someone else's.
    issue_later = Issue(number=8, title="issue 8", repo=REPO, status="Todo",
                        priority=Priority.IDEA, updated_at="2026-09-13T10:00:00+00:00")
    steps = ha.plan_steps(_stale(), issues_by_key={issue_later.key: issue_later},
                          github_cache=cache, triage_store=None, now=NOW, prior_execution=first)
    assert len(steps) == 1 and not steps[0].actionable


def test_duplicate_retry_skips_its_own_comment(monkeypatch):
    steps, gh, cache = _plan(monkeypatch, _dup(), [_issue(1), _issue(2)], gh=_Gh(fail="close"))
    first = ha.execute(_dup(), steps, now=NOW)
    gh2 = _Gh()
    _wire(monkeypatch, gh2)
    steps = ha.plan_steps(_dup(), issues_by_key={i.key: i for i in (_issue(1), _issue(2))},
                          github_cache=cache, triage_store=None, now=NOW, prior_execution=first)
    ha.execute(_dup(), steps, now=NOW, prior_execution=first)
    assert [c[0] for c in gh2.calls] == ["close"]


# ---- Codex round 4 (2026-09-13) ----


def test_comment_fact_is_recorded_only_on_success(monkeypatch):
    """Codex round 4: a failed comment must not be remembered as posted, or
    the retry closes the issue without its explanation."""
    steps, gh, cache = _plan(monkeypatch, _dup(), [_issue(1), _issue(2)], gh=_Gh(fail="comment"))
    first = ha.execute(_dup(), steps, now=NOW)
    assert first["ok"] is False and "commented_at" not in first
    gh2 = _Gh()
    _wire(monkeypatch, gh2)
    steps = ha.plan_steps(_dup(), issues_by_key={i.key: i for i in (_issue(1), _issue(2))},
                          github_cache=cache, triage_store=None, now=NOW, prior_execution=first)
    ha.execute(_dup(), steps, now=NOW, prior_execution=first)
    assert [c[0] for c in gh2.calls] == ["comment", "close"]


def test_retry_activity_anchor_never_advances_without_a_new_comment(monkeypatch):
    """Codex round 4: the anchor is the successful comment's own timestamp,
    not the latest attempt's start — an attempt refused for human activity
    must not move the cutoff past that activity."""
    def _issue8(updated):
        return Issue(number=8, title="issue 8", repo=REPO, status="Todo",
                     priority=Priority.IDEA, updated_at=updated)

    steps, gh, cache = _plan(monkeypatch, _stale(), [_issue8("2026-01-01T00:00:00Z")],
                             gh=_Gh(fail="close"))
    first = ha.execute(_stale(), steps, now="2026-09-12T10:00:00+00:00")
    assert first["commented_at"] == "2026-09-12T10:00:00+00:00"
    human = "2026-09-13T10:00:00+00:00"  # someone comments a day later
    by_key = {f"{REPO}#8": _issue8(human)}
    steps = ha.plan_steps(_stale(), issues_by_key=by_key, github_cache=cache, triage_store=None,
                          now="2026-09-13T11:00:00+00:00", prior_execution=first)
    assert len(steps) == 1 and not steps[0].actionable
    second = ha.execute(_stale(), steps, now="2026-09-13T11:00:00+00:00", prior_execution=first)
    assert second["commented_at"] == first["commented_at"]  # carried, not re-stamped
    # Third attempt an hour after the refusal: the human activity is still
    # newer than OUR comment, so it is still refused.
    steps = ha.plan_steps(_stale(), issues_by_key=by_key, github_cache=cache, triage_store=None,
                          now="2026-09-13T12:00:00+00:00", prior_execution=second)
    assert len(steps) == 1 and not steps[0].actionable


def test_execute_hands_facts_to_on_step_as_they_land(monkeypatch):
    """Codex round 4: the umbrella number must be persisted the moment it
    exists, not when the whole plan returns — a process death in between
    would otherwise create a second umbrella after the lease expires."""
    f = _cluster([f"{REPO}#3", f"{REPO}#4"])
    issues = [_issue(3, labels=["sentinel"]), _issue(4, labels=["sentinel"])]
    steps, gh, cache = _plan(monkeypatch, f, issues)
    seen = []
    ha.execute(f, steps, now=NOW, on_step=lambda log, facts: seen.append((len(log), dict(facts))))
    assert seen[0] == (1, {"umbrella": 900})
    assert all(facts.get("umbrella") == 900 for _, facts in seen)


# ---- #273: add an off-board issue to its project board ----

GADGET = "ben-ralston/gadget"


def _offboard(key=f"{GADGET}#8"):
    return Finding.new(
        FindingType.OFFBOARD, [key],
        action={"kind": "add_to_board", "key": key, "project_number": 13,
                "project_owner": "@me", "project_name": "Gadget", "title": "sentinel misfiles"},
        reasoning="off the board",
    )


class _BoardCache(_Cache):
    """Field-info + item-edit side of the real cache, recorded."""

    def __init__(self, *, todo=True, field_info=True):
        super().__init__()
        self.todo = todo
        self.field_info = field_info
        self.edits = []

    def get_field_info(self, number, owner, name):
        from agent_hq.github import FieldInfo

        if not self.field_info:
            return None
        return FieldInfo(field_id="F1", project_node_id="PVT_1",
                         options={"Todo": "OPT_TODO"} if self.todo else {"Done": "OPT_DONE"})


def _wire_board(monkeypatch, *, fail=None):
    calls = []
    monkeypatch.setattr(
        ha, "fetch_issue_summary", lambda repo, n: {"title": "t", "state": "open"}
    )

    def add(number, owner, url):
        calls.append(("add", number, owner, url))
        if fail == "add":
            raise GHError("item-add boom")
        return "PVTI_1"

    def edit(project_node_id, item_id, field_id, option_id=None):
        calls.append(("edit", project_node_id, item_id, field_id, option_id))
        return fail != "edit"

    monkeypatch.setattr(ha, "add_to_project", add)
    monkeypatch.setattr(ha, "set_field_value", edit)
    return calls


def _board_plan(monkeypatch, finding, issues=(), *, cache=None, fail=None):
    calls = _wire_board(monkeypatch, fail=fail)
    cache = cache or _BoardCache()
    steps = ha.plan_steps(finding, issues_by_key={i.key: i for i in issues},
                          github_cache=cache, triage_store=None, now=NOW)
    return steps, calls, cache


def test_add_to_board_plan_adds_the_item_then_sets_todo(monkeypatch):
    steps, calls, _ = _board_plan(monkeypatch, _offboard())
    assert [s.label for s in steps] == [
        "Add gadget#8 to the Gadget board",
        "Set Status to Todo on the Gadget board",
    ]
    assert all(s.actionable for s in steps)
    result = ha.execute(_offboard(), steps, now=NOW)
    assert result["ok"] is True and result["item_id"] == "PVTI_1"
    assert calls == [
        ("add", 13, "@me", "https://github.com/ben-ralston/gadget/issues/8"),
        ("edit", "PVT_1", "PVTI_1", "F1", "OPT_TODO"),
    ]


def test_add_to_board_failure_stops_before_the_status_write(monkeypatch):
    steps, calls, _ = _board_plan(monkeypatch, _offboard(), fail="add")
    result = ha.execute(_offboard(), steps, now=NOW)
    assert result["ok"] is False and [s["ok"] for s in result["steps"]] == [False]
    assert "item-add boom" in result["steps"][0]["detail"]
    assert [c[0] for c in calls] == ["add"]


def test_add_to_board_reports_a_missing_todo_option(monkeypatch):
    steps, calls, _ = _board_plan(monkeypatch, _offboard(), cache=_BoardCache(todo=False))
    result = ha.execute(_offboard(), steps, now=NOW)
    assert result["ok"] is False and result["item_id"] == "PVTI_1"  # the add landed
    assert "Todo" in result["steps"][1]["detail"]
    assert [c[0] for c in calls] == ["add"]


def test_add_to_board_is_nothing_to_do_once_the_issue_is_on_the_board(monkeypatch):
    on_board = Issue(number=8, title="x", repo=GADGET, status="Todo", priority=Priority.IDEA,
                     project_number=13, project_owner="@me")
    steps, calls, _ = _board_plan(monkeypatch, _offboard(), [on_board])
    assert len(steps) == 1 and not steps[0].actionable and "already on the board" in steps[0].label
    assert calls == []


def test_add_to_board_is_a_gap_when_the_issue_closed(monkeypatch):
    cache = _BoardCache()
    cache.closed_keys = {f"{GADGET}#8"}
    steps, calls, _ = _board_plan(monkeypatch, _offboard(), cache=cache)
    assert len(steps) == 1 and not steps[0].actionable and "no longer open" in steps[0].label
    assert calls == []


def test_add_to_board_sets_todo_on_an_item_already_on_the_board_without_a_status(monkeypatch):
    """Review round 1: after a failed Todo write the board refetch shows the
    issue on the board (no Status) — the retry must finish the Todo write
    using the board item, not collapse to "already on the board"."""
    on_board = Issue(number=8, title="x", repo=GADGET, status=None, priority=Priority.IDEA,
                     item_id="PVTI_fromboard", project_number=13, project_owner="@me")
    steps, calls, _ = _board_plan(monkeypatch, _offboard(), [on_board])
    assert [s.label for s in steps] == ["Set Status to Todo on the Gadget board"]
    assert steps[0].actionable
    result = ha.execute(_offboard(), steps, now=NOW)
    assert result["ok"] is True
    assert calls == [("edit", "PVT_1", "PVTI_fromboard", "F1", "OPT_TODO")]


def test_add_to_board_is_nothing_to_do_for_an_item_parked_in_done(monkeypatch):
    """Review round 2: the fresh list drops Done, but the raw board holds the
    item — forcing Status → Todo would pull a hand-parked Done issue back."""
    done = Issue(number=8, title="x", repo=GADGET, status="Done", priority=Priority.IDEA,
                 item_id="PVTI_done")
    cache = _BoardCache()
    cache.get_issues_cached = lambda number, owner: ([done], True)
    steps, calls, _ = _board_plan(monkeypatch, _offboard(), [], cache=cache)
    assert len(steps) == 1 and not steps[0].actionable and "already on the board" in steps[0].label
    assert calls == []


def test_add_to_board_is_a_gap_when_github_says_the_issue_is_closed(monkeypatch):
    """Review round 4: the closed-keys cache is only filled for repos with an
    on-board issue — the headline case has none — so the plan asks GitHub
    (REST, 0 points) before adding a possibly just-closed issue."""
    calls = _wire_board(monkeypatch)
    monkeypatch.setattr(
        ha, "fetch_issue_summary", lambda repo, n: {"title": "t", "state": "closed"}
    )
    steps = ha.plan_steps(_offboard(), issues_by_key={}, github_cache=_BoardCache(),
                          triage_store=None, now=NOW)
    assert len(steps) == 1 and not steps[0].actionable and "no longer open" in steps[0].label
    assert calls == []


def test_add_to_board_proceeds_when_the_state_lookup_fails(monkeypatch):
    _wire_board(monkeypatch)
    monkeypatch.setattr(ha, "fetch_issue_summary", lambda repo, n: None)
    steps = ha.plan_steps(_offboard(), issues_by_key={}, github_cache=_BoardCache(),
                          triage_store=None, now=NOW)
    assert [s.label for s in steps][0] == "Add gadget#8 to the Gadget board"


def test_add_to_board_is_a_gap_when_github_says_the_issue_is_missing(monkeypatch):
    _wire_board(monkeypatch)
    monkeypatch.setattr(
        ha, "fetch_issue_summary", lambda repo, n: {"title": "", "state": "missing"}
    )
    steps = ha.plan_steps(_offboard(), issues_by_key={}, github_cache=_BoardCache(),
                          triage_store=None, now=NOW)
    assert len(steps) == 1 and not steps[0].actionable and "no longer open" in steps[0].label


def test_finish_only_todo_failure_keeps_the_item_id_fact(monkeypatch):
    """Review round 7: with the item id taken from the board (no add step),
    a failed Todo write must still record it, or the partial-attempt guard
    never sees the attempt and the next sweep drops the failed log."""
    on_board = Issue(number=8, title="x", repo=GADGET, status=None, priority=Priority.IDEA,
                     item_id="PVTI_fromboard", project_number=13, project_owner="@me")
    steps, calls, _ = _board_plan(monkeypatch, _offboard(), [on_board], fail="edit")
    result = ha.execute(_offboard(), steps, now=NOW)
    assert result["ok"] is False and result["item_id"] == "PVTI_fromboard"


def test_add_to_board_ignores_the_issue_sitting_on_a_different_board(monkeypatch):
    """Review round 8: the fresh list spans every configured board; an issue
    parked on ANOTHER repo's board is still off its own — plan the add,
    never the other board's Status field onto the other board's item."""
    elsewhere = Issue(number=8, title="x", repo=GADGET, status="Todo", priority=Priority.IDEA,
                      item_id="PVTI_other", project_number=99)
    steps, calls, _ = _board_plan(monkeypatch, _offboard(), [elsewhere])
    assert [s.label for s in steps] == [
        "Add gadget#8 to the Gadget board",
        "Set Status to Todo on the Gadget board",
    ]


def test_board_member_matches_case_insensitively(monkeypatch):
    on_board = Issue(number=8, title="x", repo="Ben-Ralston/Gadget", status="Todo",
                     priority=Priority.IDEA, item_id="PVTI_x", project_number=13,
                     project_owner="@me")
    cache = _BoardCache()
    cache.get_issues_cached = lambda number, owner: ([on_board], True)
    steps, calls, _ = _board_plan(monkeypatch, _offboard(), [], cache=cache)
    assert len(steps) == 1 and not steps[0].actionable and calls == []


def test_add_to_board_fresh_list_fallback_matches_case_insensitively(monkeypatch):
    on_board = Issue(number=8, title="x", repo="Ben-Ralston/Gadget", status="Todo",
                     priority=Priority.IDEA, item_id="PVTI_x", project_number=13,
                     project_owner="@me")
    cache = _BoardCache()
    cache.get_issues_cached = None  # no raw-board read: the fresh-list fallback decides
    steps, calls, _ = _board_plan(monkeypatch, _offboard(), [on_board], cache=cache)
    assert len(steps) == 1 and not steps[0].actionable and calls == []


def test_add_to_board_fallback_ignores_an_issue_on_another_owners_same_numbered_board(
    monkeypatch,
):
    """Codex review: two configured boards can share a number under different
    owners; the fresh-list fallback must match the owner too, or an issue on
    the other owner's board reads as already on its own."""
    elsewhere = Issue(number=8, title="x", repo=GADGET, status="Todo", priority=Priority.IDEA,
                      item_id="PVTI_other", project_number=13, project_owner="other-org")
    cache = _BoardCache()
    cache.get_issues_cached = None  # no raw-board read: the fresh-list fallback decides
    steps, calls, _ = _board_plan(monkeypatch, _offboard(), [elsewhere], cache=cache)
    assert [s.label for s in steps] == [
        "Add gadget#8 to the Gadget board",
        "Set Status to Todo on the Gadget board",
    ]


def test_finish_only_early_todo_failures_keep_the_item_id_fact(monkeypatch):
    """Codex review: a missing Status field or Todo option fails BEFORE the
    item-edit, and must still record the board item's id — otherwise the
    partial-attempt guard never sees the attempt and the card resolves."""
    on_board = Issue(number=8, title="x", repo=GADGET, status=None, priority=Priority.IDEA,
                     item_id="PVTI_fromboard", project_number=13, project_owner="@me")
    for cache in (_BoardCache(todo=False), _BoardCache(field_info=False)):
        steps, _, _ = _board_plan(monkeypatch, _offboard(), [on_board], cache=cache)
        result = ha.execute(_offboard(), steps, now=NOW)
        assert result["ok"] is False and result["item_id"] == "PVTI_fromboard"
