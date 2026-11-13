"""Remote (claude.ai / Duet) tools: categorization and cache-only reads (#396)."""

import json
import subprocess
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from agent_hq import mcp_server
from agent_hq.github import FetchStatus
from agent_hq.models import Agent, AgentState, AgentType
from agent_hq.remote import tools as remote_tools
from agent_hq.status_types import LiveStatus

RECORD_KEYS = {
    "agent_type",
    "state",
    "native_status",
    "waiting_for",
    "waiting_since",
    "phase",
    "repo",
    "branch",
    "worktree",
    "issue",
    "session_name",
    "session_name_source",
    "session_id",
    "remote_control_url",
    "muted",
}


def _agent(
    pane_id,
    state=AgentState.IDLE,
    branch="feature/396-remote",
    reason=None,
    agent_type_override=AgentType.CLAUDE,
    **kw,
):
    agent = Agent(
        pane_id=pane_id,
        session_name="agent-hq",
        window_index=1,
        pane_index=0,
        agent_type=agent_type_override,
        cwd="/Users/x/Programming/agent-hq",
        repo_name="agent-hq",
        branch=branch,
        worktree_name="agent-hq.feature-396",
        state=state,
        **kw,
    )
    if reason is not None:
        agent.status_snapshot = LiveStatus(
            state="waiting",
            native_value="waiting",
            label="Waiting",
            reason=reason,
            source="claude",
            session_id="sid",
            pid=1,
            observed_at=time.time(),
            observed_ns=time.monotonic_ns(),
        )
    return agent


@pytest.fixture
def no_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"remote tool ran a subprocess: {a!r}")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


@pytest.fixture
def cached(monkeypatch):
    """Patch the cache readers; returns a setter for the agent list."""
    state = {"agents": [], "muted": set()}
    monkeypatch.setattr(
        remote_tools,
        "read_cache",
        lambda max_age: None if state["agents"] is None else (state["agents"], time.time()),
    )
    monkeypatch.setattr(remote_tools, "apply_live_status", lambda agents: agents)
    monkeypatch.setattr(
        remote_tools, "load_inactive_state", lambda: {p: object() for p in state["muted"]}
    )
    return state


def test_every_stdio_tool_is_categorized():
    names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    remote_tools.check_categorized(names)
    read, write = remote_tools.REMOTE_READ, remote_tools.REMOTE_WRITE
    never = remote_tools.NOT_REMOTE
    assert read | write | never == names
    assert not (read & write or read & never or write & never)


def test_remote_writes_are_exactly_the_approved_four():
    """#397: dispatch, triage and mute; kill/jump/chat/status/confirm/refresh never."""
    assert remote_tools.REMOTE_WRITE == {
        "dispatch_issues",
        "set_triage",
        "mute_agent",
        "unmute_agent",
    }
    for never in ("kill_agent", "jump_to_agent", "open_chat_session", "set_status"):
        assert never in remote_tools.NOT_REMOTE
    assert {"confirm_triage", "refresh_issues"} <= remote_tools.NOT_REMOTE


def test_tool_source_adds_writes_only_when_enabled():
    names = {t.name for t in remote_tools.build_tool_source(writes=True)._tool_manager.list_tools()}
    assert names == (
        remote_tools.REMOTE_READ
        | remote_tools.REMOTE_ONLY_READ
        | remote_tools.REMOTE_WRITE
        | remote_tools.REMOTE_ONLY_WRITE
    )
    read_only = {t.name for t in remote_tools.build_tool_source()._tool_manager.list_tools()}
    assert read_only == remote_tools.REMOTE_READ | remote_tools.REMOTE_ONLY_READ


def test_remote_only_writes_are_the_issue_tools_and_never_stdio_names():
    """#414: create/comment/close exist only remotely; a stdio tool of the
    same name would make the categories ambiguous."""
    assert remote_tools.REMOTE_ONLY_WRITE == {
        "create_issue",
        "comment_issue",
        "close_issue",
        "start_session",  # #429
    }
    stdio = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert not remote_tools.REMOTE_ONLY_WRITE & stdio


def test_issue_writes_carry_explicit_annotations():
    """app.py falls back to read-only annotations for an unknown name."""
    from agent_hq.remote.app import _WRITE_ANNOTATIONS, remote_tool_copies

    assert remote_tools.REMOTE_ONLY_WRITE <= set(_WRITE_ANNOTATIONS)
    tools = {
        t.name: t.annotations
        for t in remote_tool_copies(remote_tools.build_tool_source(writes=True), writes=True)
    }
    for name in remote_tools.REMOTE_ONLY_WRITE:
        ann = tools[name]
        assert ann.readOnlyHint is False and ann.openWorldHint is True, name
        assert ann.idempotentHint is False, name
    assert tools["create_issue"].destructiveHint is False
    assert tools["comment_issue"].destructiveHint is False
    assert tools["close_issue"].destructiveHint is True


def test_write_instructions_name_the_issue_tools():
    for name in remote_tools.REMOTE_ONLY_WRITE:
        assert name in remote_tools.INSTRUCTIONS_WRITE
        assert name not in remote_tools.INSTRUCTIONS
    assert "public" in remote_tools.INSTRUCTIONS_WRITE


def test_write_tools_refused_when_writes_are_off():
    from agent_hq.remote.app import remote_tool_copies

    with pytest.raises(ValueError, match="non-remote tools"):
        remote_tool_copies(remote_tools.build_tool_source(writes=True), writes=False)


def test_write_tools_carry_write_annotations():
    from agent_hq.remote.app import remote_tool_copies

    tools = {
        t.name: t.annotations
        for t in remote_tool_copies(remote_tools.build_tool_source(writes=True), writes=True)
    }
    assert tools["list_agents"].readOnlyHint is True
    for name in remote_tools.REMOTE_WRITE:
        assert tools[name].readOnlyHint is False, name
        assert tools[name].destructiveHint is False, name
    assert tools["dispatch_issues"].openWorldHint is True
    assert tools["dispatch_issues"].idempotentHint is False
    assert tools["set_triage"].idempotentHint is True
    assert tools["mute_agent"].idempotentHint is True


def test_uncategorized_tool_refuses():
    with pytest.raises(ValueError, match="new_tool"):
        remote_tools.check_categorized(["list_agents", "new_tool"])


def test_only_pure_reads_are_remote():
    assert remote_tools.REMOTE_READ == {"list_agents", "list_issues"}
    assert "refresh_issues" in remote_tools.NOT_REMOTE


def test_tool_source_registers_exactly_the_remote_reads():
    source = remote_tools.build_tool_source()
    assert {t.name for t in source._tool_manager.list_tools()} == (
        remote_tools.REMOTE_READ | remote_tools.REMOTE_ONLY_READ
    )


def test_remote_only_reads_are_the_health_tools_and_never_stdio_names():
    """#412: no stdio twin, so outside check_categorized, like REMOTE_ONLY_WRITE."""
    assert remote_tools.REMOTE_ONLY_READ == {"automations_health", "recent_errors"}
    stdio = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert not remote_tools.REMOTE_ONLY_READ & stdio


def test_health_tools_are_served_read_only_with_writes_off_and_on():
    from agent_hq.remote.app import remote_tool_copies

    for writes in (False, True):
        tools = {
            t.name: t
            for t in remote_tool_copies(
                remote_tools.build_tool_source(writes=writes), writes=writes
            )
        }
        for name in remote_tools.REMOTE_ONLY_READ:
            assert tools[name].annotations.readOnlyHint is True
            assert tools[name].annotations.destructiveHint is False


def test_instructions_name_the_health_tools_and_treat_logs_as_data():
    for text in (remote_tools.INSTRUCTIONS, remote_tools.INSTRUCTIONS_WRITE):
        for name in remote_tools.REMOTE_ONLY_READ:
            assert name in text
        assert "log text" in text


def test_list_agents_reads_cache_only(no_subprocess, cached):
    cached["agents"] = [_agent("%1", AgentState.WAITING, reason="approval"), _agent("%2")]
    cached["muted"] = {"%2"}
    records = json.loads(remote_tools.remote_list_agents(format="json"))
    assert len(records) == 1
    (rec,) = records
    assert set(rec) == RECORD_KEYS
    assert rec["state"] == "waiting"
    assert rec["waiting_for"] == "approval"
    assert rec["native_status"] == "waiting"
    assert rec["issue"] == 396
    assert rec["repo"] == "agent-hq"


def test_list_agents_text_has_summary_header(no_subprocess, cached):
    cached["agents"] = [
        _agent("%1", AgentState.WAITING, reason="approval"),
        _agent("%2", AgentState.RUNNING),
        _agent("%3", AgentState.IDLE, branch="main"),
    ]
    lines = remote_tools.remote_list_agents().splitlines()
    assert lines[0] == "1 waiting on you · 1 running · 1 idle"
    assert len(lines) == 4


def test_list_agents_header_counts_unknown_only_when_present(no_subprocess, cached):
    cached["agents"] = [_agent("%1", AgentState.UNKNOWN)]
    assert remote_tools.remote_list_agents().splitlines()[0] == (
        "0 waiting on you · 0 running · 0 idle · 1 unknown"
    )


def test_list_agents_never_emits_pane_cwd_or_summary(no_subprocess, cached):
    cached["agents"] = [_agent("%7", latest_summary="SECRET-SUMMARY")]
    for fmt in ("text", "json"):
        out = remote_tools.remote_list_agents(format=fmt)
        assert "SECRET-SUMMARY" not in out
        assert "%7" not in out
        assert "/Users/x" not in out


def test_list_agents_daemon_down(no_subprocess, cached):
    cached["agents"] = None
    assert remote_tools.remote_list_agents() == remote_tools.DAEMON_DOWN


def test_list_agents_filters_and_bad_filter(no_subprocess, cached):
    cached["agents"] = [_agent("%1", AgentState.RUNNING), _agent("%2", AgentState.IDLE)]
    assert len(json.loads(remote_tools.remote_list_agents(state="running", format="json"))) == 1
    assert "Unknown state" in remote_tools.remote_list_agents(state="bogus")
    assert "Invalid format" in remote_tools.remote_list_agents(format="xml")


def test_waiting_for_is_capped(no_subprocess, cached):
    cached["agents"] = [_agent("%1", AgentState.WAITING, reason="x" * 500)]
    (rec,) = json.loads(remote_tools.remote_list_agents(format="json"))
    assert len(rec["waiting_for"]) <= 200


def test_list_agents_carries_remote_control_link_for_claude_only(no_subprocess, cached):
    """#397: the owner answers a waiting session directly via its Remote Control link."""
    cached["agents"] = [
        _agent("%1", AgentState.WAITING, claude_session_id="session_01ABC"),
        _agent("%2", agent_type_override=AgentType.CODEX, codex_thread_id="0199-thread"),
        _agent("%3"),
    ]
    recs = json.loads(remote_tools.remote_list_agents(format="json"))
    assert [r["remote_control_url"] for r in recs] == [
        "https://claude.ai/code/session_01ABC",
        None,
        None,
    ]
    text = remote_tools.remote_list_agents()
    assert "open: https://claude.ai/code/session_01ABC" in text
    assert "codex://" not in text


def test_list_agents_include_muted(no_subprocess, cached):
    cached["agents"] = [_agent("%1"), _agent("%2", branch="feature/9-x")]
    cached["muted"] = {"%2"}
    assert len(json.loads(remote_tools.remote_list_agents(format="json"))) == 1
    # the default view still tells Duet that something is hidden
    assert remote_tools.remote_list_agents().splitlines()[0].endswith("· 1 muted")
    recs = json.loads(remote_tools.remote_list_agents(include_muted=True, format="json"))
    assert [(r["issue"], r["muted"]) for r in recs] == [(396, False), (9, True)]
    text = remote_tools.remote_list_agents(include_muted=True)
    assert "(muted)" in text
    assert text.splitlines()[0] == "0 waiting on you · 0 running · 1 idle · 1 muted"


def test_codex_session_id_is_its_thread_id(no_subprocess, cached):
    cached["agents"] = [
        _agent("%2", agent_type_override=AgentType.CODEX, codex_thread_id="0199-thread")
    ]
    (rec,) = json.loads(remote_tools.remote_list_agents(format="json"))
    assert rec["session_id"] == "0199-thread"


class _FakeCache:
    def __init__(self, status):
        self._status = status

    def cached_at(self, number, owner):
        return 1_700_000_000.0 + number

    def fetch_status(self):
        return self._status


@pytest.fixture
def issues_env(monkeypatch):
    calls = []
    env = SimpleNamespace(calls=calls, status=FetchStatus())

    def fake_all(**kw):
        calls.append(kw)
        return []

    monkeypatch.setattr(mcp_server, "_get_all_issues", fake_all)
    monkeypatch.setattr(
        mcp_server,
        "_get_config",
        lambda: SimpleNamespace(projects=[SimpleNamespace(number=7, owner="@me")]),
    )
    monkeypatch.setattr(mcp_server, "_get_github_cache", lambda: _FakeCache(env.status))
    monkeypatch.setattr(mcp_server, "_get_triage_store", lambda: None)
    return env


def test_list_issues_never_forces(issues_env):
    out = remote_tools.remote_list_issues()
    assert issues_env.calls == [{"force": False}]
    assert out.startswith("Issues as of ")


def test_list_issues_stale_notice(issues_env):
    issues_env.status = FetchStatus(ok=False, message="rate limited until 22:00")
    out = remote_tools.remote_list_issues()
    assert "GitHub refresh failed; showing cached data (rate limited until 22:00)" in out


def test_list_issues_json_wrapper(issues_env):
    data = json.loads(remote_tools.remote_list_issues(format="json"))
    assert set(data) == {"as_of", "stale", "issues"}
    assert data["issues"] == []
    assert data["stale"] is None


def test_list_issues_budget_skip_is_not_called_a_failure(issues_env):
    issues_env.status = FetchStatus(
        ok=False, message="GraphQL headroom low — background refresh paused", budget_skipped=True
    )
    out = remote_tools.remote_list_issues()
    assert "failed" not in out.splitlines()[0]
    assert "refresh paused while the GitHub budget is low; showing cached data" in out


def test_as_of_is_unknown_when_a_board_was_never_cached(issues_env, monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "_get_config",
        lambda: SimpleNamespace(
            projects=[
                SimpleNamespace(number=7, owner="@me"),
                SimpleNamespace(number=0, owner="@me"),
            ]
        ),
    )
    monkeypatch.setattr(_FakeCache, "cached_at", lambda self, n, o: None if n == 0 else 1.7e9)
    assert remote_tools.remote_list_issues().startswith("Issues as of unknown")


def test_remote_config_default_port_follows_config_default():
    from agent_hq.config import DEFAULT_REMOTE_PORT
    from agent_hq.remote.config import RemoteConfig

    assert RemoteConfig("https://x", "a", "b").port == DEFAULT_REMOTE_PORT


def _waiting_since(agent, age_s, *, source="claude"):
    now = time.time()
    agent.status_snapshot = LiveStatus(
        "waiting",
        "waiting",
        "Waiting",
        "approval",
        source,
        "sid",
        1,
        now,
        time.monotonic_ns(),
        now - age_s,
    )
    return agent


def test_list_agents_text_says_how_long_a_session_has_waited(no_subprocess, cached):
    """#415: the morning brief's "scratch-69, waiting 3 h"."""
    cached["agents"] = [_waiting_since(_agent("%1", AgentState.WAITING), 3 * 3600 + 720)]
    line = remote_tools.remote_list_agents().splitlines()[1]
    assert "· waiting 3h12m: approval" in line


def test_list_agents_json_carries_waiting_since(no_subprocess, cached):
    agent = _waiting_since(_agent("%1", AgentState.WAITING), 120)
    cached["agents"] = [agent]
    (rec,) = json.loads(remote_tools.remote_list_agents(format="json"))
    parsed = datetime.fromisoformat(rec["waiting_since"])
    assert parsed.tzinfo is not None
    assert parsed.timestamp() == pytest.approx(agent.status_snapshot.waiting_since, abs=1)


def test_list_agents_waiting_since_covers_codex(no_subprocess, cached):
    agent = _waiting_since(
        _agent("%1", AgentState.WAITING, agent_type_override=AgentType.CODEX), 300, source="codex"
    )
    cached["agents"] = [agent]
    (rec,) = json.loads(remote_tools.remote_list_agents(format="json"))
    assert rec["waiting_for"] is None
    assert rec["waiting_since"] is not None
    assert "· waiting 5m" in remote_tools.remote_list_agents().splitlines()[1]


def test_list_agents_waiting_without_a_start_still_reads_as_before(no_subprocess, cached):
    cached["agents"] = [_agent("%1", AgentState.WAITING, reason="approval")]
    (rec,) = json.loads(remote_tools.remote_list_agents(format="json"))
    assert rec["waiting_since"] is None
    assert "· waiting: approval" in remote_tools.remote_list_agents().splitlines()[1]


def test_instructions_say_waiting_sessions_report_how_long():
    for text in (remote_tools.INSTRUCTIONS, remote_tools.INSTRUCTIONS_WRITE):
        assert "how long" in text


def test_start_session_is_a_non_destructive_write_served_only_with_writes():
    """#429: not read-only, not destructive; absent while remote.writes is off."""
    from agent_hq.remote.app import remote_tool_copies

    tools = {
        t.name: t.annotations
        for t in remote_tool_copies(remote_tools.build_tool_source(writes=True), writes=True)
    }
    ann = tools["start_session"]
    assert (ann.readOnlyHint, ann.destructiveHint, ann.idempotentHint) == (False, False, False)
    off = {t.name for t in remote_tool_copies(remote_tools.build_tool_source(), writes=False)}
    assert "start_session" not in off


def test_start_session_takes_only_a_name():
    """#429: no prompt, cwd, model or effort from the caller."""
    import inspect

    assert list(inspect.signature(remote_tools.start_session).parameters) == ["name"]
