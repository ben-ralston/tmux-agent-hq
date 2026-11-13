import pytest

from agent_hq.autopilot import (
    NO_PR,
    SKIP_ATTEMPTED,
    SKIP_GITHUB,
    SKIP_NOT_CONFIRMED,
    SKIP_NOT_OPEN,
    SKIP_REPO,
    SKIP_THROTTLED,
    SKIP_WORKTREE,
    SKIP_WORKTREE_UNKNOWN,
    PrInfo,
    compute_outcomes,
    record_still_eligible,
    select,
)
from agent_hq.autopilot_store import Attempt, Outcome
from agent_hq.config import AutopilotConfig
from agent_hq.models import Issue, Priority
from agent_hq.triage_store import AgentFitness, Effort, TriageRecord, TriageState

REPO = "ben-ralston/agent-hq"
OTHER = "ben-ralston/other"
CFG = AutopilotConfig(enabled=True, per_night=2, max_open=3, repos=(REPO, OTHER))


def _issue(n, repo=REPO):
    return Issue(number=n, title=f"issue {n}", repo=repo, status="Todo", priority=Priority.NO_PRI)


def _rec(n, *, value=3, autopilot=True, state=TriageState.CONFIRMED, repo=REPO, tomb=None):
    return TriageRecord(
        issue_key=f"{repo}#{n}",
        value=value,
        effort=Effort.S,
        agent_fitness=AgentFitness.SOLO,
        autopilot=autopilot,
        state=state,
        tombstoned_at=tomb,
    )


def _sel(issues, records, blocked=(), in_flight=0, active=(), unsafe=None, cfg=CFG):
    return select(
        issues,
        {r.issue_key: r for r in records},
        set(blocked),
        in_flight=in_flight,
        active_issue_keys=set(active),
        unsafe_repos=unsafe or {},
        cfg=cfg,
    )


def test_rank_order_and_per_night_cap():
    s = _sel(
        [_issue(1), _issue(2), _issue(3)],
        [_rec(1, value=2), _rec(2, value=5), _rec(3, value=4)],
    )
    assert [i.number for i in s.launch] == [2, 3] and s.throttled is False


@pytest.mark.parametrize("flag", [None, False])
def test_unflagged_records_are_not_candidates(flag):
    s = _sel([_issue(1)], [_rec(1, autopilot=flag)])
    assert s.launch == [] and s.skipped == []  # removing the opt-in guard must fail this


def test_skip_reasons():
    s = _sel(
        [_issue(1), _issue(3), _issue(4), _issue(6), _issue(7, repo=OTHER), _issue(8, repo="x/z")],
        [
            _rec(1, state=TriageState.PROPOSED),
            _rec(2),
            _rec(3),
            _rec(4),
            _rec(6),
            _rec(7, repo=OTHER),
            _rec(8, repo="x/z"),
        ],
        blocked={f"{REPO}#3"},
        active={f"{REPO}#4"},
        unsafe={REPO: SKIP_GITHUB, OTHER: SKIP_WORKTREE_UNKNOWN},
    )
    assert dict(s.skipped) == {
        f"{REPO}#1": SKIP_NOT_CONFIRMED,
        # Absent from an unsafe repo: the fetch failure, not "closed".
        f"{REPO}#2": SKIP_GITHUB,
        f"{REPO}#3": SKIP_ATTEMPTED,
        f"{REPO}#4": SKIP_WORKTREE,
        f"{REPO}#6": SKIP_GITHUB,
        f"{OTHER}#7": SKIP_WORKTREE_UNKNOWN,
        "x/z#8": SKIP_REPO,
    }
    assert s.launch == []


def test_closed_issue_skipped():
    assert dict(_sel([], [_rec(7)]).skipped) == {f"{REPO}#7": SKIP_NOT_OPEN}


def test_absent_issue_in_unsafe_non_allowlisted_repo_is_not_open():
    # A fetch failure in a repo autopilot can't launch from must not turn a
    # closed flagged issue into a rule-9 failure.
    s = _sel([], [_rec(5, repo="x/z")], unsafe={"x/z": SKIP_GITHUB})
    assert dict(s.skipped) == {"x/z#5": SKIP_NOT_OPEN}


def test_tombstoned_record_skipped_as_not_open():
    assert dict(_sel([_issue(7)], [_rec(7, tomb="x")]).skipped) == {f"{REPO}#7": SKIP_NOT_OPEN}


def test_throttle_partial_and_full():
    issues, recs = [_issue(1), _issue(2)], [_rec(1), _rec(2, value=4)]
    s = _sel(issues, recs, in_flight=2)
    assert [i.number for i in s.launch] == [2] and dict(s.skipped) == {f"{REPO}#1": SKIP_THROTTLED}
    s = _sel(issues, recs, in_flight=3)
    assert s.launch == [] and s.throttled is True


def test_record_still_eligible():
    assert record_still_eligible(_rec(1)) is True
    assert record_still_eligible(None) is False
    assert record_still_eligible(_rec(1, autopilot=False)) is False
    assert record_still_eligible(_rec(1, state=TriageState.PROPOSED)) is False
    assert record_still_eligible(_rec(1, tomb="x")) is False


def _attempt(n, outcome, session="s", branch=None, aid=None):
    return Attempt(
        issue_key=f"{REPO}#{n}",
        repo=REPO,
        branch=branch or f"feature/{n}-x",
        run_id="r",
        dispatched_at="t",
        outcome=outcome,
        attempt_id=aid or f"a{n}",
        session_name=session,
    )


def test_compute_outcomes_transitions():
    attempts = {
        "a": _attempt(1, Outcome.RUNNING, session="s1"),
        "b": _attempt(2, Outcome.RUNNING, session="s2"),
        "c": _attempt(3, Outcome.RUNNING, session="s3"),
        "d": _attempt(4, Outcome.PR_OPEN, session="s4"),
        "e": _attempt(5, Outcome.UNKNOWN, session="s5"),
        "f": _attempt(6, Outcome.UNKNOWN, session=None),
        "g": _attempt(7, Outcome.LAUNCHING, session=None),
        "h": _attempt(8, Outcome.MERGED),
    }
    prs = {"feature/1-x": PrInfo("OPEN", "u1"), "feature/4-x": PrInfo("MERGED", "u4")}
    alive = {"s2": True, "s3": False, "s5": True}
    out = compute_outcomes(
        attempts, lambda repo, branch: prs.get(branch, NO_PR), lambda name: alive.get(name)
    )
    assert {k: (u.expected, u.new) for k, u in out.items()} == {
        "a": (Outcome.RUNNING, Outcome.PR_OPEN),
        "c": (Outcome.RUNNING, Outcome.ENDED_WITHOUT_PR),
        "d": (Outcome.PR_OPEN, Outcome.MERGED),
    }  # b alive; e alive UNKNOWN stays (needs Retry); f no session -> stays
    # unknown; g launching untouched; h final
    assert out["a"].attempt_id == "a1" and out["a"].pr_url == "u1"


def test_unknown_moves_only_on_positive_evidence():
    # Spec rule 7: UNKNOWN needs the owner's Retry. A live session is not evidence
    # the launch succeeded; a real PR or a positively gone session is.
    attempts = {
        "alive": _attempt(1, Outcome.UNKNOWN, session="s1"),
        "gone": _attempt(2, Outcome.UNKNOWN, session="s2"),
        "pr": _attempt(3, Outcome.UNKNOWN, session="s3"),
        "merged": _attempt(4, Outcome.UNKNOWN, session="s4"),
        "closed": _attempt(5, Outcome.UNKNOWN, session="s5"),
    }
    prs = {
        "feature/3-x": PrInfo("OPEN", "u3"),
        "feature/4-x": PrInfo("MERGED", "u4"),
        "feature/5-x": PrInfo("CLOSED", "u5"),
    }
    alive = {"s1": True, "s2": False, "s3": True, "s4": True, "s5": True}
    out = compute_outcomes(
        attempts, lambda repo, branch: prs.get(branch, NO_PR), lambda name: alive.get(name)
    )
    assert {k: u.new for k, u in out.items()} == {
        "gone": Outcome.ENDED_WITHOUT_PR,
        "pr": Outcome.PR_OPEN,
        "merged": Outcome.MERGED,
        "closed": Outcome.CLOSED,
    }


def test_pr_fetch_failure_keeps_outcomes():
    attempts = {"a": _attempt(1, Outcome.RUNNING, session="s1")}
    assert compute_outcomes(attempts, lambda r, b: None, lambda name: False) == {}


def test_unknown_session_probe_keeps_outcome():
    attempts = {"a": _attempt(1, Outcome.RUNNING, session="s1")}
    assert compute_outcomes(attempts, lambda r, b: NO_PR, lambda name: None) == {}
