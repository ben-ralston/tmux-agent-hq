"""Remote write tools for Claude Duet (#397): fail-closed log, duet provenance,
duplicate guards, nothing local (pane ids, paths) in what leaves the Mac."""

import dataclasses
import inspect
import json
import time
from pathlib import Path

import pytest

from agent_hq import dispatch, enrichment, eventlog, mcp_server, state
from agent_hq.config import Config, RepoConfig
from agent_hq.dispatch import DispatchResult
from agent_hq.models import Agent, AgentState, AgentType, Issue
from agent_hq.remote import actions
from agent_hq.remote import tools as remote_tools
from agent_hq.triage_store import AgentFitness, Effort, TriageRecord, TriageState, TriageStore


@pytest.fixture
def emitted(monkeypatch):
    calls = []

    def fake(level, message, extra=None, **kw):
        calls.append((level, message, extra, kw))
        return True

    monkeypatch.setattr(eventlog, "emit", fake)
    return calls


def _log_lines():
    path = actions.ActionLog().path
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# --- the _remote_write wrapper ----------------------------------------------


def test_write_records_attempt_then_result_and_emits(emitted):
    ran = []

    def run(action_id):
        ran.append(action_id)
        return remote_tools.WriteOutcome(True, "ok", "Done: o/r#1", detail="pane %5 local")

    out = remote_tools._remote_write("set_triage", {"issue_key": "o/r#1"}, run)
    assert out == "Done: o/r#1"
    attempt, result = _log_lines()
    assert ran == [attempt["action_id"]]
    assert attempt["phase"] == "attempt" and attempt["args"] == {"issue_key": "o/r#1"}
    assert result["phase"] == "result" and result["action_id"] == attempt["action_id"]
    assert (result["ok"], result["code"], result["outcome"]) == (True, "ok", "pane %5 local")
    ((level, message, extra, kw),) = emitted
    assert (level, message) == ("info", "remote action")
    assert extra == {"tool": "set_triage", "ok": True, "code": "ok", "issue_key": "o/r#1"}
    assert kw.get("min_interval_s") == 0
    assert "%5" not in json.dumps(extra)


def test_write_refused_when_log_unavailable(emitted, monkeypatch):
    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(actions.ActionLog, "attempt", boom)
    ran = []
    out = remote_tools._remote_write(
        "dispatch_issues",
        {"issue_keys": ["o/r#1"]},
        lambda _id: ran.append(1) or remote_tools.WriteOutcome(True, "ok", "x"),
    )
    assert out == remote_tools.ACTION_LOG_DOWN
    assert ran == []
    assert [(lvl, msg) for lvl, msg, *_ in emitted] == [("warn", "remote action log unavailable")]


def test_write_error_outcome_is_recorded(emitted):
    out = remote_tools._remote_write(
        "mute_agent",
        {"session": "a"},
        lambda _id: remote_tools.WriteOutcome(False, "not_found", "Error: no session a"),
    )
    assert out == "Error: no session a"
    result = _log_lines()[-1]
    assert (result["ok"], result["code"]) == (False, "not_found")
    assert emitted[-1][2]["code"] == "not_found"


def test_write_crash_is_recorded_and_reraised(emitted):
    def run(_id):
        raise RuntimeError("tmux exploded in /Users/x/secret")

    with pytest.raises(RuntimeError):
        remote_tools._remote_write("dispatch_issues", {"issue_keys": ["o/r#1"]}, run)
    result = _log_lines()[-1]
    assert (result["ok"], result["code"]) == (False, "crashed")
    extra = emitted[-1][2]
    assert extra["code"] == "crashed"
    assert "/Users/x" not in json.dumps(extra)


def test_emit_carries_only_safe_arg_fields(emitted):
    remote_tools._remote_write(
        "set_triage",
        {"issue_key": "o/r#1", "value": 3, "reasoning": "long text " * 10},
        lambda _id: remote_tools.WriteOutcome(True, "ok", "ok"),
    )
    extra = emitted[-1][2]
    assert extra["value"] == 3
    assert "reasoning" not in extra


def test_log_timestamps_are_wall_clock(emitted):
    before = time.time()
    remote_tools._remote_write(
        "set_triage",
        {"issue_key": "o/r#1"},
        lambda _id: remote_tools.WriteOutcome(True, "ok", "ok"),
    )
    assert _log_lines()[0]["ts"] >= before


# --- dispatch_issues ----------------------------------------------------------


def _issue(number, repo="owner/repo"):
    return Issue(number=number, title=f"Issue {number}", repo=repo, status="Todo", priority=None)


def _live(branch, repo_name="repo", pane="%9", **kw):
    return Agent(
        pane_id=pane,
        session_name="repo",
        window_index=1,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd="/Users/x/wt/repo.feature",
        repo_name=repo_name,
        branch=branch,
        state=AgentState.RUNNING,
        **kw,
    )


@pytest.fixture
def dispatch_env(monkeypatch, emitted):
    env = {"agents": [], "dispatched": [], "result": DispatchResult(success=True, branch="b")}
    config = Config(
        repos={"owner/repo": RepoConfig("owner/repo", Path("/tmp/checkouts/repo"))},
        projects=[],
        worktree_root=Path("/tmp/worktrees"),
    )
    monkeypatch.setattr(mcp_server, "_get_config", lambda: config)
    monkeypatch.setattr(
        mcp_server,
        "_get_all_issues",
        lambda force=False: [_issue(1), _issue(2), _issue(3, "owner/other")],
    )
    monkeypatch.setattr(
        remote_tools,
        "read_cache",
        lambda max_age: None if env["agents"] is None else (env["agents"], time.time()),
    )
    monkeypatch.setattr(remote_tools, "apply_live_status", lambda agents: agents)

    def fake_dispatch(batches, cfg, **kw):
        env["dispatched"].append([i.key for b in batches for i in b])
        return env["result"]

    monkeypatch.setattr(dispatch, "dispatch_via_claude", fake_dispatch)
    env["worktrees"] = set()
    monkeypatch.setattr(
        remote_tools,
        "_worktree_issue_numbers",
        lambda path: None if env["worktrees"] is None else set(env["worktrees"]),
    )
    return env


def test_dispatch_starts_the_issue(dispatch_env):
    out = remote_tools.dispatch_issues(["owner/repo#1"])
    assert dispatch_env["dispatched"] == [["owner/repo#1"]]
    assert out.startswith("Started")
    assert "owner/repo#1" in out
    assert _log_lines()[-1]["code"] == "ok"


def test_dispatch_refuses_issue_already_on_a_live_branch(dispatch_env):
    dispatch_env["agents"] = [_live("feature/1-something")]
    out = remote_tools.dispatch_issues(["owner/repo#1"])
    assert out.startswith("Error") and "already has a live session" in out
    assert dispatch_env["dispatched"] == []
    assert _log_lines()[-1]["code"] == "duplicate"


def test_live_branch_guard_is_per_repo(dispatch_env):
    dispatch_env["agents"] = [_live("feature/1-something", repo_name="other")]
    assert remote_tools.dispatch_issues(["owner/repo#1"]).startswith("Started")


def test_dispatch_refuses_a_recent_attempt_then_allows_after(dispatch_env, monkeypatch):
    remote_tools.dispatch_issues(["owner/repo#1"])
    out = remote_tools.dispatch_issues(["owner/repo#1"])
    assert out.startswith("Error") and "already dispatched" in out
    assert len(dispatch_env["dispatched"]) == 1
    real_time = time.time
    monkeypatch.setattr(remote_tools.time, "time", lambda: real_time() + 601)
    assert remote_tools.dispatch_issues(["owner/repo#1"]).startswith("Started")


def test_recent_attempt_guard_is_per_issue(dispatch_env):
    remote_tools.dispatch_issues(["owner/repo#1"])
    assert remote_tools.dispatch_issues(["owner/repo#2"]).startswith("Started")


def test_dispatch_refuses_with_daemon_down(dispatch_env):
    dispatch_env["agents"] = None
    assert remote_tools.dispatch_issues(["owner/repo#1"]) == remote_tools.DAEMON_DOWN
    assert dispatch_env["dispatched"] == []


def test_dispatch_validation_errors_pass_through(dispatch_env):
    out = remote_tools.dispatch_issues(["owner/repo#1", "owner/other#3"])
    assert out.startswith("Error") and "multiple repos" in out
    assert "not found" in remote_tools.dispatch_issues(["owner/repo#99"]).lower()
    assert remote_tools.dispatch_issues([]).startswith("Error")
    assert dispatch_env["dispatched"] == []


def test_slow_start_maps_to_launch_uncertain_without_pane_ids(dispatch_env, emitted):
    dispatch_env["result"] = DispatchResult(
        success=False,
        launched=True,
        error="Claude did not start in pane %42 within 15s - pane left open",
    )
    out = remote_tools.dispatch_issues(["owner/repo#1"])
    assert "%42" not in out and "may still start" in out
    assert _log_lines()[-1]["code"] == "launch_uncertain"
    assert "%42" in _log_lines()[-1]["outcome"]  # the full text stays local
    assert "%42" not in json.dumps(emitted[-1][2])


def test_plain_dispatch_failure_has_no_pane_ids(dispatch_env):
    dispatch_env["result"] = DispatchResult(success=False, error="Could not create pane %3")
    out = remote_tools.dispatch_issues(["owner/repo#1"])
    assert out.startswith("Error") and "%3" not in out
    assert _log_lines()[-1]["code"] == "failed"


# --- set_triage -----------------------------------------------------------------


@pytest.fixture
def triage_env(monkeypatch, tmp_path, emitted):
    store = TriageStore(tmp_path / "data")
    monkeypatch.setattr(mcp_server, "_get_triage_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_get_config", lambda: None)
    monkeypatch.setattr(mcp_server, "_get_all_issues", lambda force=False: [_issue(1)])
    return store


KEY = "owner/repo#1"


def test_set_triage_writes_as_duet_and_confirms_when_all_three_given(triage_env):
    out = remote_tools.set_triage(KEY, value=4, effort="S", fitness="solo", reasoning="Quick win")
    assert out.startswith("Set triage") and "confirmed" in out
    rec = triage_env.get(KEY)
    assert (rec.value, rec.effort, rec.agent_fitness) == (4, Effort.S, AgentFitness.SOLO)
    assert set(rec.provenance.values()) == {"duet"}
    assert rec.reasoning["value"] == "Quick win"
    assert rec.state is TriageState.CONFIRMED
    assert _log_lines()[-1]["code"] == "ok"


def test_partial_edit_on_a_proposal_stays_in_the_inbox(triage_env):
    triage_env.upsert(
        TriageRecord(
            issue_key=KEY, value=2, effort=Effort.M, agent_fitness=AgentFitness.PAIR, updated_at="t"
        ),
        actor="ai",
    )
    out = remote_tools.set_triage(KEY, value=5)
    assert "confirmed" not in out
    rec = triage_env.get(KEY)
    assert rec.state is TriageState.PROPOSED
    assert rec.provenance == {"value": "duet", "effort": "ai", "agent_fitness": "ai"}


def test_duet_confirm_commits_bens_own_staged_pin(triage_env):
    """Review round 1: the staged pin is the owner's own inbox tap; leaving it
    staged on a CONFIRMED record would orphan it (confirm skips those)."""
    triage_env.upsert(
        TriageRecord(
            issue_key=KEY, value=2, effort=Effort.M, agent_fitness=AgentFitness.PAIR, updated_at="t"
        ),
        actor="ai",
    )
    triage_env.upsert(TriageRecord(issue_key=KEY, pin_staged=True, updated_at="t"), actor="human")
    remote_tools.set_triage(KEY, value=3, effort="L", fitness="needs_you")
    rec = triage_env.get(KEY)
    assert rec.state is TriageState.CONFIRMED
    assert rec.pin is True and rec.pin_staged is None


def test_set_triage_has_no_flag_parameters():
    params = set(inspect.signature(remote_tools.set_triage).parameters)
    assert params == {"issue_key", "value", "effort", "fitness", "reasoning"}


def test_set_triage_validation_and_missing_issue(triage_env):
    assert "value must be" in remote_tools.set_triage(KEY, value=9)
    assert "not found" in remote_tools.set_triage("owner/repo#99", value=3).lower()
    assert remote_tools.set_triage(KEY).startswith("Error")
    assert triage_env.get(KEY) is None


def test_set_triage_without_store(monkeypatch, emitted):
    monkeypatch.setattr(mcp_server, "_get_triage_store", lambda: None)
    monkeypatch.setattr(mcp_server, "_get_config", lambda: None)
    monkeypatch.setattr(mcp_server, "_get_all_issues", lambda force=False: [_issue(1)])
    assert "triage store not configured" in remote_tools.set_triage(KEY, value=3)


# --- mute_agent / unmute_agent --------------------------------------------------


@pytest.fixture
def mute_env(monkeypatch, emitted):
    env = {
        "agents": [
            _live(
                "feature/5-a",
                pane="%5",
                native_name="fix-login",
                claude_transcript_session_id="sid-5",
            ),
            _live(
                "feature/6-b",
                pane="%6",
                native_name="twin",
                claude_transcript_session_id="sid-6",
            ),
            _live(
                "feature/7-c",
                pane="%7",
                native_name="twin",
                claude_transcript_session_id="sid-7",
            ),
        ]
    }
    monkeypatch.setattr(
        remote_tools,
        "read_cache",
        lambda max_age: None if env["agents"] is None else (env["agents"], time.time()),
    )
    monkeypatch.setattr(enrichment, "compute_content_fingerprint", lambda pane: f"fp-{pane}")
    return env


def test_mute_by_name_and_by_session_id(mute_env):
    out = remote_tools.mute_agent("@fix-login")
    assert out.startswith("Muted") and "%5" not in out
    assert "%5" in state.load_inactive_state()
    assert remote_tools.unmute_agent("sid-5").startswith("Unmuted")
    assert "%5" not in state.load_inactive_state()


def test_repeated_mute_stays_muted(mute_env):
    remote_tools.mute_agent("fix-login")
    out = remote_tools.mute_agent("fix-login")
    assert out.startswith("Muted")  # a set, not a toggle
    assert "%5" in state.load_inactive_state()


def test_ambiguous_and_unknown_sessions_are_refused(mute_env):
    out = remote_tools.mute_agent("twin")
    assert out.startswith("Error") and "sid-6" in out and "sid-7" in out
    assert "%6" not in out
    assert remote_tools.mute_agent("nobody").startswith("Error")
    assert remote_tools.mute_agent("fix").startswith("Error")  # exact names only
    assert state.load_inactive_state() == {}
    assert _log_lines()[-1]["code"] == "not_found"


def test_mute_uses_set_inactive_not_the_toggle(mute_env, monkeypatch):
    def toggle(*a, **k):
        raise AssertionError("remote mute must not toggle")

    monkeypatch.setattr(state, "toggle_inactive", toggle)
    assert remote_tools.mute_agent("sid-5").startswith("Muted")


def test_mute_daemon_down(mute_env):
    mute_env["agents"] = None
    assert remote_tools.mute_agent("sid-5") == remote_tools.DAEMON_DOWN


# --- review round 1 ---------------------------------------------------------------


def test_attempt_without_a_result_still_blocks_a_retry(dispatch_env):
    """A crash between launch and the result line leaves result_code None: may
    have started, so a retry inside the window is refused."""
    actions.ActionLog().attempt("dispatch_issues", {"issue_keys": ["owner/repo#1"]})
    out = remote_tools.dispatch_issues(["owner/repo#1"])
    assert "already dispatched" in out
    assert dispatch_env["dispatched"] == []


def test_refused_attempts_do_not_block_a_retry(dispatch_env):
    dispatch_env["agents"] = [_live("feature/1-x")]
    assert "live session" in remote_tools.dispatch_issues(["owner/repo#1"])
    dispatch_env["agents"] = []
    assert remote_tools.dispatch_issues(["owner/repo#1"]).startswith("Started")


@pytest.mark.parametrize("model", ["opus #2; run curl x|sh", "opus\nrm", "a" * 65, ""])
def test_remote_model_must_be_a_plain_model_id(dispatch_env, model):
    out = remote_tools.dispatch_issues(["owner/repo#1"], model=model)
    assert out.startswith("Error") and "model" in out
    assert dispatch_env["dispatched"] == []


def test_plain_model_ids_pass(dispatch_env):
    assert remote_tools.dispatch_issues(["owner/repo#1"], model="claude-opus-5-5[1m]").startswith(
        "Started"
    )


def test_leftover_worktree_blocks_dispatch(dispatch_env):
    dispatch_env["worktrees"] = {1}
    out = remote_tools.dispatch_issues(["owner/repo#1"])
    assert "already has a worktree" in out
    assert dispatch_env["dispatched"] == []


def test_unreadable_worktree_list_refuses(dispatch_env):
    dispatch_env["worktrees"] = None
    out = remote_tools.dispatch_issues(["owner/repo#1"])
    assert out.startswith("Error") and "could not check" in out
    assert dispatch_env["dispatched"] == []


PORCELAIN = """worktree /Users/x/repo
HEAD abc
branch refs/heads/main

worktree /Users/x/wt/repo.bugfix-42-fix
HEAD def
branch refs/heads/bugfix/42-fix

worktree /Users/x/wt/repo.docs-7-8-notes
HEAD 123
branch refs/heads/docs/7-8-notes

worktree /Users/x/wt/detached
HEAD 456
detached
"""


def test_worktree_scan_covers_every_typed_prefix(monkeypatch):
    calls = []

    class Done:
        returncode, stdout = 0, PORCELAIN

    def fake_run(argv, **kw):
        calls.append(argv)
        assert kw.get("timeout")
        return Done()

    monkeypatch.setattr(remote_tools.subprocess, "run", fake_run)
    assert remote_tools._worktree_issue_numbers(Path("/Users/x/repo")) == {42, 7, 8}
    assert calls == [["git", "-C", "/Users/x/repo", "worktree", "list", "--porcelain"]]


def test_worktree_scan_failure_is_none(monkeypatch):
    def boom(argv, **kw):
        raise remote_tools.subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(remote_tools.subprocess, "run", boom)
    assert remote_tools._worktree_issue_numbers(Path("/x")) is None


def test_triage_store_error_text_never_reaches_duet(triage_env, monkeypatch):
    def boom(*a, **k):
        raise OSError("[Errno 13] Permission denied: '/Users/x/triage/o__r.json'")

    monkeypatch.setattr(triage_env, "upsert", boom)
    out = remote_tools.set_triage(KEY, value=3)
    assert out.startswith("Error") and "/Users/" not in out
    assert "/Users/x" in _log_lines()[-1]["outcome"]  # kept locally


def test_partial_duet_edits_confirm_once_no_ai_dims_remain(triage_env):
    remote_tools.set_triage(KEY, value=3)
    remote_tools.set_triage(KEY, effort="M")
    assert triage_env.get(KEY).state is TriageState.UNTRIAGED
    out = remote_tools.set_triage(KEY, fitness="solo")
    assert "confirmed" in out
    assert triage_env.get(KEY).state is TriageState.CONFIRMED


def test_mute_never_captures_the_pane(mute_env, monkeypatch):
    def boom(pane):
        raise AssertionError("remote mute must not capture-pane")

    monkeypatch.setattr(enrichment, "compute_content_fingerprint", boom)
    assert remote_tools.mute_agent("sid-5").startswith("Muted")


def test_unmute_with_unreadable_state_reports_failure(mute_env):
    state.INACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state.INACTIVE_FILE.write_text("{corrupt")
    out = remote_tools.unmute_agent("sid-5")
    assert out.startswith("Error")
    assert _log_lines()[-1]["code"] == "failed"


def test_codex_sessions_are_addressable_by_thread_id(mute_env):
    codex = _live("feature/8-d", pane="%8", codex_thread_id="0199-codex-thread")
    codex.agent_type = AgentType.CODEX
    mute_env["agents"].append(codex)
    assert remote_tools.mute_agent("0199-codex-thread").startswith("Muted")
    assert "%8" in state.load_inactive_state()


# --- #414: create_issue / comment_issue / close_issue ----------------------------

from agent_hq import gh_budget, github  # noqa: E402
from agent_hq.config import ProjectConfig  # noqa: E402
from agent_hq.github import FieldInfo, GHError, GHTimeoutError  # noqa: E402


class _FakeCache:
    _graphql_floor = 500

    def __init__(self):
        self.closed = []
        self.refetched = []
        self.field_info = FieldInfo(
            project_node_id="PVT_1", field_id="F_1", options={"Todo": "OPT_todo"}
        )

    def get_field_info(self, number, owner, name):
        return self.field_info

    def get_issues(self, number, owner, force=False):
        self.refetched.append((number, owner, force))
        return []

    def mark_closed(self, repo, number):
        self.closed.append((repo, number))


@pytest.fixture
def gh_env(monkeypatch, tmp_path, emitted):
    """The owner's config (one repo on a board, one without), a fake cache, a
    triage store and fake GitHub writes that record what they were asked."""
    env = {
        "issues": [
            Issue(number=5, title="Existing bug", repo="Owner/Repo", status="Todo", priority=None)
        ],
        "labels": {"bug", "Enhancement", "sentinel", "deep-audit"},
        "summaries": {},
        "created": [],
        "comments": [],
        "closes": [],
        "added": [],
        "fields": [],
        "budget_ok": True,
        "create_error": None,
        "add_error": None,
    }
    config = Config(
        repos={
            "owner/repo": RepoConfig("owner/repo", Path("/tmp/checkouts/repo"), project="Board"),
            "owner/nob": RepoConfig("owner/nob", Path("/tmp/checkouts/nob")),
        },
        projects=[ProjectConfig("Board", 5, "@me")],
        worktree_root=Path("/tmp/worktrees"),
    )
    cache = _FakeCache()
    store = TriageStore(tmp_path / "data")
    env["cache"], env["store"] = cache, store
    monkeypatch.setattr(mcp_server, "_get_config", lambda: config)
    monkeypatch.setattr(mcp_server, "_get_github_cache", lambda: cache)
    monkeypatch.setattr(mcp_server, "_get_triage_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_get_all_issues", lambda force=False: list(env["issues"]))
    monkeypatch.setattr(gh_budget, "allow_background_refresh", lambda floor=None: env["budget_ok"])
    monkeypatch.setattr(github, "fetch_label_names", lambda repo: env["labels"])

    def summary(repo, number):
        default = {"title": "t", "state": "open", "is_pr": False}
        # Keyed case-insensitively: the tools pass GitHub's spelling.
        by_key = {k.lower(): v for k, v in env["summaries"].items()}
        return by_key.get(f"{repo}#{number}".lower(), default)

    monkeypatch.setattr(github, "fetch_issue_summary", summary)

    def create(repo, title, body, labels=None):
        if env["create_error"]:
            raise env["create_error"]
        env["created"].append((repo, title, body, labels))
        return "https://github.com/Owner/Repo/issues/42"

    monkeypatch.setattr(github, "create_issue", create)

    def add(number, owner, url):
        if env["add_error"]:
            raise env["add_error"]
        env["added"].append((number, owner, url))
        return "ITEM_1"

    monkeypatch.setattr(github, "add_to_project", add)
    monkeypatch.setattr(
        github,
        "set_field_value",
        lambda node, item, field, option=None: env["fields"].append((node, item, field, option))
        or True,
    )

    def comment(repo, number, body):
        env["comments"].append((repo, number, body))
        return f"https://github.com/{repo}/issues/{number}#issuecomment-1"

    monkeypatch.setattr(github, "post_issue_comment", comment)
    monkeypatch.setattr(
        github, "close_issue", lambda repo, number, **kw: env["closes"].append((repo, number, kw))
    )
    return env


# create_issue


def test_create_files_lands_on_board_seeds_triage_and_refetches(gh_env):
    out = remote_tools.create_issue("owner/repo", "New thing", "Details", labels=["bug"])
    ((repo, title, body, labels),) = gh_env["created"]
    assert (repo, title, labels) == ("owner/repo", "New thing", ["bug"])
    assert body.startswith("Details") and body.endswith(remote_tools.ISSUE_FOOTER)
    assert gh_env["added"] == [(5, "@me", "https://github.com/Owner/Repo/issues/42")]
    assert gh_env["fields"] == [("PVT_1", "ITEM_1", "F_1", "OPT_todo")]
    assert gh_env["cache"].refetched == [(5, "@me", True)]
    # GitHub's spelling, from the URL, not the config's.
    rec = gh_env["store"].get("Owner/Repo#42")
    assert rec is not None and rec.state is TriageState.UNTRIAGED
    assert "Owner/Repo#42" in out and "https://github.com/Owner/Repo/issues/42" in out
    assert "untriaged" in out
    assert _log_lines()[-1]["code"] == "ok"


def test_create_skips_the_refetch_when_the_budget_is_low(gh_env):
    gh_env["budget_ok"] = False
    out = remote_tools.create_issue("owner/repo", "New thing", "Details")
    assert gh_env["cache"].refetched == []
    assert "within a few minutes" in out


def test_create_refuses_reserved_labels(gh_env):
    for label in ("sentinel", "Deep-Audit"):
        out = remote_tools.create_issue("owner/repo", "New thing", "Details", labels=[label])
        assert out.startswith("Error") and "reserved" in out
    assert gh_env["created"] == []


def test_create_refuses_a_label_the_repo_lacks(gh_env):
    out = remote_tools.create_issue("owner/repo", "New thing", "Details", labels=["nope"])
    assert out.startswith("Error") and "nope" in out
    assert gh_env["created"] == []


def test_create_uses_the_repos_label_spelling(gh_env):
    remote_tools.create_issue("owner/repo", "New thing", "Details", labels=["enhancement"])
    assert gh_env["created"][0][3] == ["Enhancement"]


def test_create_refuses_when_labels_cannot_be_checked(gh_env):
    gh_env["labels"] = None
    out = remote_tools.create_issue("owner/repo", "New thing", "Details", labels=["bug"])
    assert out.startswith("Error") and gh_env["created"] == []
    assert _log_lines()[-1]["code"] == "failed"


def test_create_without_labels_never_looks_them_up(gh_env, monkeypatch):
    monkeypatch.setattr(github, "fetch_label_names", lambda repo: pytest.fail("looked up"))
    assert remote_tools.create_issue("owner/repo", "New thing", "Details").startswith("Filed")


def test_create_refuses_an_unconfigured_repo(gh_env):
    out = remote_tools.create_issue("someone/else", "New thing", "Details")
    assert out.startswith("Error") and "configured" in out
    assert _log_lines()[-1]["code"] == "invalid"


@pytest.mark.parametrize(
    "title, body",
    [("", "b"), ("  ", "b"), ("a\nb", "b"), ("x" * 257, "b"), ("ok", ""), ("ok", "x" * 20_001)],
)
def test_create_validates_title_and_body(gh_env, title, body):
    assert remote_tools.create_issue("owner/repo", title, body).startswith("Error")
    assert gh_env["created"] == []


def test_create_refuses_a_near_duplicate_title_on_the_board(gh_env):
    out = remote_tools.create_issue("OWNER/repo", "  existing   BUG. ", "Details")
    assert out.startswith("Error") and "Owner/Repo#5" in out
    assert gh_env["created"] == []
    assert _log_lines()[-1]["code"] == "duplicate"


def test_create_title_duplicate_is_per_repo(gh_env):
    gh_env["issues"][0].repo = "owner/nob"
    assert remote_tools.create_issue("owner/repo", "Existing bug", "x").startswith("Filed")


def test_create_refuses_a_repeat_within_ten_minutes(gh_env, monkeypatch):
    remote_tools.create_issue("owner/repo", "New thing", "Details")
    out = remote_tools.create_issue("owner/repo", "new thing!", "Other details")
    assert out.startswith("Error") and "already filed" in out
    assert len(gh_env["created"]) == 1
    real_time = time.time
    monkeypatch.setattr(remote_tools.time, "time", lambda: real_time() + 601)
    assert remote_tools.create_issue("owner/repo", "New thing", "Details").startswith("Filed")


def test_a_refused_create_does_not_block_a_retry(gh_env):
    gh_env["create_error"] = GHError("issue create failed: boom")
    assert remote_tools.create_issue("owner/repo", "New thing", "D").startswith("Error")
    gh_env["create_error"] = None
    assert remote_tools.create_issue("owner/repo", "New thing", "D").startswith("Filed")


def test_a_timed_out_create_is_uncertain_and_blocks_a_retry(gh_env):
    gh_env["create_error"] = GHTimeoutError("issue create timed out after 10s")
    out = remote_tools.create_issue("owner/repo", "New thing", "D")
    assert "may still have been filed" in out
    assert _log_lines()[-1]["code"] == "uncertain"
    gh_env["create_error"] = None
    assert remote_tools.create_issue("owner/repo", "New thing", "D").startswith("Error")
    assert gh_env["created"] == []


def test_create_failure_keeps_gh_text_local(gh_env):
    gh_env["create_error"] = GHError("issue create failed: /Users/x/secret path")
    out = remote_tools.create_issue("owner/repo", "New thing", "D")
    assert "/Users" not in out
    assert "/Users/x/secret" in _log_lines()[-1]["outcome"]
    assert _log_lines()[-1]["code"] == "failed"


def test_board_failure_is_partial_not_failed(gh_env):
    gh_env["add_error"] = GHError("project-add failed: nope")
    out = remote_tools.create_issue("owner/repo", "New thing", "D")
    assert out.startswith("Filed") and "Not done: adding it to the Board board" in out
    assert _log_lines()[-1]["code"] == "partial"
    # A partial create landed: a repeat is refused.
    assert remote_tools.create_issue("owner/repo", "New thing", "D").startswith("Error")


def test_todo_failure_is_partial(gh_env):
    gh_env["cache"].field_info = None
    out = remote_tools.create_issue("owner/repo", "New thing", "D")
    assert "Status" in out and _log_lines()[-1]["code"] == "partial"


def test_repo_without_a_board_files_and_says_so(gh_env):
    out = remote_tools.create_issue("owner/nob", "New thing", "D")
    assert out.startswith("Filed") and "no board" in out
    assert gh_env["added"] == []
    assert _log_lines()[-1]["code"] == "ok"


def test_create_refused_when_action_log_is_down(gh_env, monkeypatch):
    def boom(self, *a, **kw):
        raise OSError("disk")

    monkeypatch.setattr(actions.ActionLog, "attempt", boom)
    assert remote_tools.create_issue("owner/repo", "x", "y") == remote_tools.ACTION_LOG_DOWN
    assert gh_env["created"] == []


def test_create_emits_no_title_or_body(gh_env, emitted):
    remote_tools.create_issue("owner/repo", "Secret title", "Secret body", labels=["bug"])
    extra = emitted[-1][2]
    assert extra["repo"] == "owner/repo" and "labels" not in extra
    assert "Secret" not in json.dumps(extra)


# comment_issue


def test_comment_posts_with_footer(gh_env):
    out = remote_tools.comment_issue("owner/repo#5", "Still happening")
    ((repo, number, body),) = gh_env["comments"]
    assert (repo, number) == ("Owner/Repo", 5)  # the board's spelling
    assert body.startswith("Still happening") and body.endswith(remote_tools.COMMENT_FOOTER)
    assert out.startswith("Commented") and "#issuecomment-1" in out


@pytest.mark.parametrize(
    "state, code",
    [
        ({"title": "", "state": "missing", "is_pr": False}, "not_found"),
        ({"title": "t", "state": "closed", "is_pr": False}, "not_open"),
        ({"title": "t", "state": "open", "is_pr": True}, "invalid"),
        (None, "failed"),
    ],
)
def test_comment_open_check_fails_closed(gh_env, state, code):
    gh_env["summaries"]["owner/repo#7"] = state
    out = remote_tools.comment_issue("owner/repo#7", "hi")
    assert out.startswith("Error") and gh_env["comments"] == []
    assert _log_lines()[-1]["code"] == code


def test_comment_refuses_unconfigured_repo_and_bad_keys(gh_env):
    for key in ("someone/else#1", "owner/repo", "owner/repo#x", "#5"):
        assert remote_tools.comment_issue(key, "hi").startswith("Error"), key
    assert remote_tools.comment_issue("owner/repo#5", "").startswith("Error")
    assert gh_env["comments"] == []


def test_comment_refuses_the_same_comment_twice_but_not_a_new_one(gh_env):
    remote_tools.comment_issue("owner/repo#5", "hi")
    assert remote_tools.comment_issue("Owner/Repo#5", "hi").startswith("Error")
    assert remote_tools.comment_issue("owner/repo#5", "something else").startswith("Commented")
    assert len(gh_env["comments"]) == 2
    assert "body_sha256" in _log_lines()[0]["args"]


# close_issue


@pytest.mark.parametrize(
    "reason, said", [("completed", "completed"), ("not planned", "not planned")]
)
def test_close_with_a_reason(gh_env, reason, said):
    out = remote_tools.close_issue("owner/repo#5", reason, "Done in #9")
    ((repo, number, kw),) = gh_env["closes"]
    assert (repo, number) == ("Owner/Repo", 5)
    assert kw["reason"] == reason.replace(" ", "_") and kw.get("duplicate_of_url") is None
    assert kw["comment"].endswith(remote_tools.COMMENT_FOOTER)
    assert gh_env["cache"].closed == [("Owner/Repo", 5)]
    assert out.startswith("Closed") and said in out
    assert "https://github.com/Owner/Repo/issues/5" in out


def test_close_as_duplicate_of_a_configured_repo(gh_env):
    out = remote_tools.close_issue("owner/repo#5", "duplicate", "Same as #3", "owner/nob#3")
    kw = gh_env["closes"][0][2]
    assert kw["reason"] == "duplicate"
    assert kw["duplicate_of_url"] == "https://github.com/owner/nob/issues/3"
    assert "duplicate of owner/nob#3" in out


@pytest.mark.parametrize(
    "reason, dup",
    [
        ("duplicate", None),
        ("duplicate", "someone/else#3"),
        ("duplicate", "owner/repo#5"),
        ("completed", "owner/repo#3"),
        ("wontfix", None),
    ],
)
def test_close_validation(gh_env, reason, dup):
    assert remote_tools.close_issue("owner/repo#5", reason, "c", dup).startswith("Error")
    assert gh_env["closes"] == []


def test_close_refuses_a_missing_duplicate_target(gh_env):
    gh_env["summaries"]["owner/repo#3"] = {"title": "", "state": "missing", "is_pr": False}
    assert remote_tools.close_issue("owner/repo#5", "duplicate", "c", "owner/repo#3").startswith(
        "Error"
    )
    assert gh_env["closes"] == []


def test_close_needs_a_comment(gh_env):
    assert remote_tools.close_issue("owner/repo#5", "completed", "  ").startswith("Error")


def test_close_refuses_an_already_closed_issue(gh_env):
    gh_env["summaries"]["owner/repo#5"] = {"title": "t", "state": "closed", "is_pr": False}
    out = remote_tools.close_issue("owner/repo#5", "completed", "c")
    assert "already closed" in out and _log_lines()[-1]["code"] == "not_open"


def test_close_has_no_live_session_or_sentinel_guard(gh_env, monkeypatch):
    """The owner's call (#414): the claude.ai Ask-first rule is the only gate."""
    monkeypatch.setattr(remote_tools, "read_cache", lambda max_age: pytest.fail("checked sessions"))
    gh_env["issues"][0].labels = ["sentinel"]
    assert remote_tools.close_issue("owner/repo#5", "completed", "c").startswith("Closed")


def test_close_refuses_a_repeat(gh_env):
    remote_tools.close_issue("owner/repo#5", "completed", "c")
    assert remote_tools.close_issue("owner/repo#5", "not_planned", "c").startswith("Error")
    assert len(gh_env["closes"]) == 1


def test_timed_out_close_is_uncertain(gh_env, monkeypatch):
    def slow(repo, number, **kw):
        raise GHTimeoutError("issue close timed out after 10s")

    monkeypatch.setattr(github, "close_issue", slow)
    out = remote_tools.close_issue("owner/repo#5", "completed", "c")
    assert out.startswith("Error") and "may still have" in out
    assert _log_lines()[-1]["code"] == "uncertain"
    assert gh_env["cache"].closed == []


def test_close_emits_reason_not_comment(gh_env, emitted):
    remote_tools.close_issue("owner/repo#5", "duplicate", "Secret words", "owner/repo#3")
    extra = emitted[-1][2]
    assert extra["reason"] == "duplicate" and extra["duplicate_of"] == "owner/repo#3"
    assert "Secret" not in json.dumps(extra)


# --- #414 review round 1 ---------------------------------------------------------


def test_a_close_github_refused_may_have_posted_its_comment(gh_env, monkeypatch):
    """gh posts --comment before closing: a refused close may have left the
    public comment, so it counts toward the repeat guard."""

    def refused(repo, number, **kw):
        raise GHError("issue close failed: duplicate target rejected")

    monkeypatch.setattr(github, "close_issue", refused)
    out = remote_tools.close_issue("owner/repo#5", "completed", "c")
    assert out.startswith("Error") and "comment may already be posted" in out
    assert _log_lines()[-1]["code"] == "close_failed"
    out = remote_tools.close_issue("owner/repo#5", "not_planned", "c")
    assert out.startswith("Error") and "already attempted" in out


def test_an_open_check_failure_does_not_block_a_close_retry(gh_env):
    gh_env["summaries"]["owner/repo#5"] = None
    assert remote_tools.close_issue("owner/repo#5", "completed", "c").startswith("Error")
    gh_env["summaries"].pop("owner/repo#5")
    assert remote_tools.close_issue("owner/repo#5", "completed", "c").startswith("Closed")


def test_free_text_in_safe_fields_never_reaches_the_logger(gh_env, emitted):
    remote_tools.close_issue("owner/repo#5", "duplicate of Sam's lease email", "c")
    remote_tools.close_issue("owner/repo#5", "duplicate", "c", "the thing in Sam's email")
    remote_tools.create_issue("a sentence about Sam's lease", "t", "b")
    remote_tools.create_issue("owner/repo", "t", "b", labels=["x" * 200])
    dumped = json.dumps([e[2] for e in emitted])
    assert "Sam" not in dumped and "x" * 200 not in dumped
    assert all(e[2]["tool"] in ("close_issue", "create_issue") for e in emitted)


def test_create_repeat_guard_matches_a_whitespace_spelled_repo(gh_env):
    gh_env["create_error"] = GHTimeoutError("issue create timed out after 10s")
    remote_tools.create_issue(" owner/repo ", "New thing", "D")
    gh_env["create_error"] = None
    out = remote_tools.create_issue("owner/repo", "New thing", "D")
    assert out.startswith("Error") and "already filed" in out
    assert gh_env["created"] == []


def test_a_seed_that_wrote_nothing_is_partial(gh_env, monkeypatch):
    monkeypatch.setattr(gh_env["store"], "seed_missing", lambda *a, **kw: 0)
    out = remote_tools.create_issue("owner/repo", "New thing", "D")
    assert _log_lines()[-1]["code"] == "partial"
    assert "triage record" in out and "untriaged" not in out


def test_partial_text_reads_as_a_sentence(gh_env):
    gh_env["cache"].field_info = None
    out = remote_tools.create_issue("owner/repo", "New thing", "D")
    assert "Not done: setting its Status to Todo on the Board board." in out
    assert "but it is" not in out


def test_duplicate_checks_run_before_the_label_lookup(gh_env, monkeypatch):
    monkeypatch.setattr(github, "fetch_label_names", lambda repo: pytest.fail("looked up"))
    out = remote_tools.create_issue("owner/repo", "Existing bug", "x", labels=["bug"])
    assert out.startswith("Error") and "Owner/Repo#5" in out


@pytest.mark.parametrize("bad", [" ", " ", "\x85", "\x9b"])
def test_create_refuses_unicode_line_breaks_and_controls_in_titles(gh_env, bad):
    assert remote_tools.create_issue("owner/repo", f"a{bad}b", "x").startswith("Error")
    assert gh_env["created"] == []


def test_logger_fields_are_deny_by_default(emitted):
    """#414 security review: Duet's raw args are logged before validation,
    so every emitted field must match a strict shape; labels never go out."""
    remote_tools._remote_write(
        "set_triage",
        {
            "issue_key": "the issue from Sam's email",
            "value": "five, because Sam said so",
            "effort": "S",
            "session": "@ok-name",
            "harness": "claude",
            "labels": ["bug"],
        },
        lambda _id: remote_tools.WriteOutcome(True, "ok", "x"),
    )
    extra = emitted[-1][2]
    assert "issue_key" not in extra and "value" not in extra and "labels" not in extra
    assert extra["effort"] == "S" and extra["session"] == "@ok-name"
    assert extra["harness"] == "claude"
    assert set(remote_tools._EMIT_FIELDS) <= set(remote_tools._EMIT_SHAPES)


@pytest.mark.parametrize(
    "fields",
    [
        {"repo": "sams-lease/the_email_about_rent"},
        {"issue_key": "sams-lease/the-email#1"},
        {"session": "@sams_lease_email"},
        {"duplicate_of": "a/b#1"},
    ],
)
def test_unresolved_identifiers_never_reach_the_logger(emitted, fields):
    """#414 security review round 2: a hyphen/underscore-joined sentence
    fits an identifier shape, so an identifier is emitted only when the
    call resolved it (never on invalid / not_found)."""
    for code in ("invalid", "not_found"):
        remote_tools._remote_write(
            "close_issue", fields, lambda _id, c=code: remote_tools.WriteOutcome(False, c, "x")
        )
        assert not set(fields) & set(emitted[-1][2]), code


def test_identifier_shapes_are_ascii_and_github_sized(emitted):
    for bad in ("ownér/repo#1", "o/r#12345678901", "x" * 40 + "/r#1"):
        remote_tools._remote_write(
            "close_issue",
            {"issue_key": bad},
            lambda _id: remote_tools.WriteOutcome(True, "ok", "x"),
        )
        assert "issue_key" not in emitted[-1][2], bad
    remote_tools._remote_write(
        "close_issue",
        {"issue_key": "ben-ralston/agent-hq#414"},
        lambda _id: remote_tools.WriteOutcome(True, "ok", "x"),
    )
    assert emitted[-1][2]["issue_key"] == "ben-ralston/agent-hq#414"


def test_an_unexpected_status_step_error_is_partial_not_a_crash(gh_env, monkeypatch):
    """Review round 4: the issue is filed by then; Duet must hear that."""

    def boom(*a, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(gh_env["cache"], "get_field_info", boom)
    out = remote_tools.create_issue("owner/repo", "New thing", "D")
    assert out.startswith("Filed") and "Not done: setting its Status to Todo" in out
    assert _log_lines()[-1]["code"] == "partial"


@pytest.mark.parametrize(
    "code", ["failed", "daemon_down", "crashed", "log_unavailable", "invalid", "not_found"]
)
def test_identifiers_only_go_out_on_resolved_outcomes(emitted, code):
    """#414 security review round 3: allowlist, not deny-list."""
    args = {"issue_key": "o/r#1", "repo": "o/r", "session": "@s", "duplicate_of": "o/r#2"}
    remote_tools._remote_write(
        "close_issue", args, lambda _id: remote_tools.WriteOutcome(False, code, "x")
    )
    assert not set(args) & set(emitted[-1][2])


@pytest.mark.parametrize(
    "code", ["ok", "partial", "uncertain", "close_failed", "not_open", "duplicate"]
)
def test_identifiers_go_out_on_resolved_outcomes(emitted, code):
    remote_tools._remote_write(
        "close_issue",
        {"issue_key": "o/r#1"},
        lambda _id: remote_tools.WriteOutcome(True, code, "x"),
    )
    assert emitted[-1][2]["issue_key"] == "o/r#1"


def test_config_unavailable_create_emits_no_repo(gh_env, monkeypatch, emitted):
    monkeypatch.setattr(mcp_server, "_get_config", lambda: None)
    remote_tools.create_issue("sams-lease/the-email", "t", "b")
    assert "repo" not in emitted[-1][2]


# --- start_session (#429) -----------------------------------------------------


def _settings(sessions):
    from agent_hq.config import RemoteSettings

    return RemoteSettings(
        public_url="https://agents.example.com",
        allowed_github_login="ben",
        allowed_github_id=1,
        github_client_id="cid",
        writes=True,
        sessions=sessions,
    )


@pytest.fixture
def session_env(monkeypatch, emitted, tmp_path):
    from agent_hq.config import DispatchConfig, RemoteSession

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    sessions = {
        "daily-review": RemoteSession(name="daily-review", prompt="/daily-review"),
        "weekly": RemoteSession(
            name="weekly", prompt="/weekly-summary", cwd=other, model="opus", effort="high"
        ),
        "gone": RemoteSession(name="gone", prompt="x", cwd=tmp_path / "missing"),
    }
    config = Config(
        repos={},
        projects=[],
        worktree_root=tmp_path / "wt",
        dispatch=DispatchConfig(scratch_cwd=scratch),
        remote=_settings(sessions),
    )
    env = {
        "agents": [],
        "opened": [],
        "pane": "%77",
        "started": True,
        "existing": "scratch",
        "scratch": scratch,
        "other": other,
        "config": config,
    }
    monkeypatch.setattr(mcp_server, "_get_config", lambda: env["config"])
    monkeypatch.setattr(
        remote_tools,
        "read_cache",
        lambda max_age: None if env["agents"] is None else (env["agents"], time.time()),
    )
    monkeypatch.setattr(remote_tools, "apply_live_status", lambda agents: agents)
    monkeypatch.setattr(dispatch, "_find_session_by_name", lambda name: env["existing"])

    def fake_open(**kw):
        env["opened"].append(kw)
        if env.get("appears") is not None:  # the daemon sees the new session
            env["agents"] = [*env["agents"], env["appears"]]
        return env["pane"]

    monkeypatch.setattr(dispatch, "open_chat_window", fake_open)
    monkeypatch.setattr(
        dispatch, "_wait_for_harness_process", lambda pane, h="claude": env["started"]
    )
    monkeypatch.setattr(remote_tools, "SESSION_LINK_WAIT_S", 0.0)
    return env


def test_start_session_opens_the_allowed_prompt_in_the_scratch_session(session_env):
    out = remote_tools.start_session("daily-review")
    assert session_env["opened"] == [
        {
            "existing_session": "scratch",
            "new_session_name": "scratch",
            "cwd": session_env["scratch"],
            "harness": "claude",
            "model": None,
            "effort": None,
            "prompt": "/daily-review",
            "name": "daily-review",
        }
    ]
    assert out.startswith("Started daily-review") and "@daily-review" in out
    assert "list_agents" in out
    attempt, result = _log_lines()
    assert attempt["tool"] == "start_session" and attempt["args"] == {"name": "daily-review"}
    assert result["code"] == "ok"


def test_start_session_uses_the_entrys_cwd_model_and_effort(session_env):
    session_env["existing"] = None
    remote_tools.start_session("weekly")
    (kw,) = session_env["opened"]
    assert kw["existing_session"] is None
    assert (kw["cwd"], kw["model"], kw["effort"]) == (session_env["other"], "opus", "high")
    assert kw["prompt"] == "/weekly-summary"


def test_start_session_returns_the_remote_control_link_once_cached(session_env):
    session_env["agents"] = [
        _live(
            "main",
            pane="%1",
            native_name="other",
            claude_session_id="session_zzz",
        ),
    ]
    session_env["appears"] = _live(
        "main", pane="%77", native_name="daily-review", claude_session_id="session_abc"
    )
    out = remote_tools.start_session("daily-review")
    assert len(session_env["opened"]) == 1 and out.startswith("Started daily-review")
    assert "open: https://claude.ai/code/session_abc" in out
    assert "session_zzz" not in out
    assert "@daily-review" in out


def test_start_session_refuses_an_unknown_name_and_lists_the_allowed(session_env, emitted):
    out = remote_tools.start_session("rm -rf")
    assert out.startswith("Error") and "not a session the owner has allowed" in out
    assert "daily-review, gone, weekly" in out
    assert session_env["opened"] == []
    assert _log_lines()[-1]["code"] == "not_allowed"
    assert "name" not in emitted[-1][2]  # caller text never reaches the logger


@pytest.mark.parametrize("name", ["", None, 3, "DAILY-REVIEW", "daily-review ", "x" * 500])
def test_start_session_refuses_anything_but_an_exact_name(session_env, name):
    out = remote_tools.start_session(name)
    assert out.startswith("Error") and len(out) < 400
    assert session_env["opened"] == []


def test_start_session_with_no_allowlist_refuses_everything(session_env):
    session_env["config"] = Config(repos={}, projects=[], worktree_root=Path("/tmp/wt"))
    out = remote_tools.start_session("daily-review")
    assert out.startswith("Error") and "remote.sessions" in out
    session_env["config"] = None
    assert remote_tools.start_session("daily-review").startswith("Error")
    assert session_env["opened"] == []


def test_start_session_refuses_a_repeat_within_ten_minutes(session_env, monkeypatch):
    remote_tools.start_session("daily-review")
    out = remote_tools.start_session("daily-review")
    assert out.startswith("Error") and "already started" in out and "10 minutes" in out
    assert "still starting, or it was closed" in out  # true in both cases
    assert _log_lines()[-1]["code"] == "duplicate"
    assert remote_tools.start_session("weekly").startswith("Started")  # per name
    assert len(session_env["opened"]) == 2
    real_time = time.time
    monkeypatch.setattr(remote_tools.time, "time", lambda: real_time() + 601)
    assert remote_tools.start_session("daily-review").startswith("Started")


def test_a_refused_start_does_not_block_a_retry(session_env):
    session_env["agents"] = None
    assert remote_tools.start_session("daily-review") == remote_tools.DAEMON_DOWN
    assert session_env["opened"] == []
    session_env["agents"] = []
    assert remote_tools.start_session("daily-review").startswith("Started")


def test_a_start_with_no_result_line_blocks_a_retry(session_env):
    actions.ActionLog().attempt("start_session", {"name": "daily-review"})
    assert "already started" in remote_tools.start_session("daily-review")
    assert session_env["opened"] == []


def test_slow_session_start_is_uncertain_and_blocks_a_retry(session_env, emitted):
    session_env["started"] = False
    out = remote_tools.start_session("daily-review")
    assert out.startswith("Error") and "may still start" in out and "%77" not in out
    result = _log_lines()[-1]
    assert result["code"] == "launch_uncertain" and "%77" in result["outcome"]
    assert emitted[-1][2]["name"] == "daily-review"
    assert "already started" in remote_tools.start_session("daily-review")


def test_failed_window_is_failed_and_does_not_block_a_retry(session_env):
    session_env["pane"] = None
    out = remote_tools.start_session("daily-review")
    assert out.startswith("Error") and "was not started" in out
    assert "nothing was started" not in out  # an empty window may be left behind
    assert _log_lines()[-1]["code"] == "start_failed"
    session_env["pane"] = "%77"
    assert remote_tools.start_session("daily-review").startswith("Started")


def test_missing_cwd_refuses_without_naming_the_path(session_env):
    out = remote_tools.start_session("gone")
    assert out.startswith("Error")
    assert str(session_env["scratch"].parent) not in out
    assert session_env["opened"] == []
    result = _log_lines()[-1]
    assert result["code"] == "start_failed" and "missing" in result["outcome"]


def test_a_failed_start_still_names_the_entry_in_the_logger(session_env, emitted):
    """Review round 1: the name is already resolved, so a broken entry is
    identifiable from the Errors tab."""
    remote_tools.start_session("gone")
    assert emitted[-1][2] == {
        "tool": "start_session",
        "ok": False,
        "code": "start_failed",
        "name": "gone",
    }


def test_start_text_does_not_claim_the_prompt_ran(session_env):
    """Review round 1: claude being up doesn't mean the prompt was accepted
    (a folder-trust dialog can hold it)."""
    out = remote_tools.start_session("daily-review")
    assert "is running" not in out and "opened" in out
    assert "dialog" in out


def test_session_name_shape_is_the_config_rule():
    from agent_hq.config import REMOTE_SESSION_NAME_RE

    assert remote_tools._EMIT_SHAPES["name"]("a" * 40)
    assert not remote_tools._EMIT_SHAPES["name"]("a" * 41)
    assert REMOTE_SESSION_NAME_RE.fullmatch("daily-review")


def test_start_session_refused_when_action_log_is_down(session_env, monkeypatch):
    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(actions.ActionLog, "attempt", boom)
    assert remote_tools.start_session("daily-review") == remote_tools.ACTION_LOG_DOWN
    assert session_env["opened"] == []


def test_start_session_emits_the_name_and_nothing_local(session_env, emitted):
    remote_tools.start_session("daily-review")
    extra = emitted[-1][2]
    assert extra == {"tool": "start_session", "ok": True, "code": "ok", "name": "daily-review"}


def test_open_chat_window_passes_the_session_name_to_claude(monkeypatch):
    """#429: the name rides claude's own --name flag, shell-quoted."""
    ran = []

    class _Done:
        returncode = 0
        stdout = "%5\n"

    monkeypatch.setattr(dispatch.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or _Done())
    pane = dispatch.open_chat_window(
        existing_session="scratch",
        new_session_name="scratch",
        cwd=Path("/tmp"),
        prompt="/daily-review",
        name="daily-review",
    )
    assert pane == "%5"
    assert ran[-1][:4] == ["tmux", "send-keys", "-t", "%5"]
    assert ran[-1][4] == "claude --name daily-review -- /daily-review"


# The owner's call on PR #431: a name with a live session returns that session.


def test_a_live_session_of_that_name_is_returned_not_relaunched(session_env, emitted):
    session_env["agents"] = [
        _live("main", pane="%1", native_name="weekly", claude_session_id="session_w"),
        _live("main", pane="%2", native_name="daily-review", claude_session_id="session_live"),
    ]
    out = remote_tools.start_session("daily-review")
    assert session_env["opened"] == []
    assert not out.startswith("Error")
    assert "existing session @daily-review" in out and "no new session" in out
    assert "open: https://claude.ai/code/session_live" in out
    assert "session_w" not in out and "%2" not in out
    attempt, result = _log_lines()
    assert attempt["args"] == {"name": "daily-review"}
    assert (result["ok"], result["code"]) == (True, "existing") and "%2" in result["outcome"]
    assert emitted[-1][2] == {
        "tool": "start_session",
        "ok": True,
        "code": "existing",
        "name": "daily-review",
    }


def test_an_existing_session_without_a_link_is_still_returned(session_env):
    session_env["agents"] = [_live("main", pane="%2", native_name="daily-review")]
    out = remote_tools.start_session("daily-review")
    assert session_env["opened"] == []
    assert "existing session @daily-review" in out and "list_agents" in out and "open:" not in out


def test_only_a_claude_session_with_exactly_that_name_counts(session_env):
    session_env["agents"] = [
        _live("main", pane="%2", native_name="daily-review-2", claude_session_id="s1"),
        dataclasses.replace(
            _live("main", pane="%3", native_name="daily-review"), agent_type=AgentType.CODEX
        ),
    ]
    assert remote_tools.start_session("daily-review").startswith("Started daily-review")
    assert len(session_env["opened"]) == 1


def test_a_double_tap_gets_the_session_once_it_is_seen_else_the_refusal(session_env):
    """The 10-minute guard still covers a start too new to be in the cache;
    once the session is visible a second call returns it."""
    assert remote_tools.start_session("daily-review").startswith("Started")
    assert "already started" in remote_tools.start_session("daily-review")
    session_env["agents"] = [
        _live("main", pane="%77", native_name="daily-review", claude_session_id="session_new")
    ]
    out = remote_tools.start_session("daily-review")
    assert "existing session @daily-review" in out and "session_new" in out
    assert len(session_env["opened"]) == 1


def test_returning_an_existing_session_does_not_block_a_later_start(session_env):
    session_env["agents"] = [_live("main", pane="%2", native_name="daily-review")]
    remote_tools.start_session("daily-review")
    session_env["agents"] = []  # the owner closed it
    assert remote_tools.start_session("daily-review").startswith("Started")
