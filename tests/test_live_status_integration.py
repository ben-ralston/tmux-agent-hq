"""Every live consumer must replace cached and hook-derived state."""

from unittest.mock import Mock

import pytest

from agent_hq.models import Agent, AgentState, AgentType


def test_enrichment_never_uses_hooks_or_terminal_status(monkeypatch):
    from agent_hq import enrichment

    monkeypatch.setattr(
        "agent_hq.hooks.read_hook_state", Mock(return_value=(AgentState.RUNNING, "valid"))
    )
    content = Mock(return_value="Working...")
    monkeypatch.setattr(enrichment, "get_pane_content", content)
    assert (
        enrichment.get_agent_state(
            "%1", AgentType.CLAUDE, current_session_id="session", agent_pid=123
        )
        is AgentState.UNKNOWN
    )
    content.assert_not_called()


def test_cache_consumers_do_not_trust_serialized_state(monkeypatch):
    from agent_hq import cache

    agent = Agent("%1", "s", 0, 0, AgentType.CLAUDE, "/tmp", state=AgentState.RUNNING)
    monkeypatch.setattr(cache, "read_cache", lambda: ([agent], 1.0))
    assert cache.get_agents()[0][0].state is AgentState.UNKNOWN


@pytest.mark.parametrize("command", [None, "claude", "codex"])
def test_daemon_owns_collector_lifecycle(monkeypatch, command):
    from agent_hq import daemon, status_collector

    collector = Mock()
    monkeypatch.setattr(status_collector, "RuntimeStatusCollector", lambda: collector)
    monkeypatch.setattr(daemon, "get_all_panes", lambda: [])
    raw = [{"pane_id": "%1", "command": command}] if command else []
    monkeypatch.setattr(daemon, "discover_agents", lambda: raw)
    monkeypatch.setattr(daemon, "enrich_all_agents", lambda _: [])
    monkeypatch.setattr(daemon.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    daemon.run_poll_loop()
    collector.start.assert_called_once()
    collector.stop.assert_called_once()
    collector.set_claude_present.assert_called_with(command == "claude")


def test_claude_rename_uses_native_identity(monkeypatch, tmp_path):
    import json
    import time

    from agent_hq import actions, enrichment, status_collector
    from agent_hq.status_types import LiveStatus

    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    (sessions / '42.json').write_text(json.dumps({'sessionId': 'current'}))
    monkeypatch.setattr(enrichment, 'CLAUDE_SESSIONS_DIR', sessions)
    monkeypatch.setattr(actions, '_get_pane_agent_info', lambda _: (AgentType.CLAUDE, 42))
    monkeypatch.setattr(status_collector, '_alive', lambda pid: pid == 42)
    send = Mock()
    monkeypatch.setattr(actions.subprocess, 'run', send)
    path = tmp_path / 'status.json'
    monkeypatch.setattr(status_collector, 'STATUS_FILE', path)
    for native, expected in [('running', False), ('waiting', False), ('idle', True)]:
        now, ns = time.time(), time.monotonic_ns()
        state = LiveStatus(native, native, native.title(), '', 'claude', 'current', 42, now, ns)
        path.write_text(json.dumps(dict(version=1, heartbeat_at=now, heartbeat_ns=ns,
                                       observations=[{'status': state.to_dict()}],
                                       source_errors={})))
        send.reset_mock()
        assert actions.rename_session('%1') is expected
        assert send.called is expected
    # A stale session file must not authorize sending /rename into a new session.
    (sessions / '42.json').write_text(json.dumps({'sessionId': 'previous'}))
    assert actions.rename_session('%1') is False


def test_kill_failure_reports_native_state_for_current_process(monkeypatch, tmp_path):
    import json
    import time

    from agent_hq import actions, enrichment, status_collector, tools
    from agent_hq.status_types import LiveStatus

    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    (sessions / '42.json').write_text(json.dumps({'sessionId': 'current'}))
    monkeypatch.setattr(enrichment, 'CLAUDE_SESSIONS_DIR', sessions)
    monkeypatch.setattr(status_collector, '_alive', lambda pid: pid == 42)
    monkeypatch.setattr(actions, 'kill_agent', lambda _: True)
    monkeypatch.setattr(actions, 'verify_agent_killed', lambda _: (False, AgentType.CLAUDE))
    path = tmp_path / 'status.json'
    monkeypatch.setattr(status_collector, 'STATUS_FILE', path)
    now, ns = time.time(), time.monotonic_ns()
    state = LiveStatus('waiting', 'waiting', 'Waiting', '', 'claude', 'current', 42, now, ns)
    path.write_text(json.dumps(dict(version=1, heartbeat_at=now, heartbeat_ns=ns,
                                   observations=[{'status': state.to_dict()}], source_errors={})))
    agent = Agent('%1', 's', 0, 0, AgentType.CLAUDE, '/tmp', agent_pid=42)
    context = tools.ToolContext(None, None, [], [agent], 'agents', set())
    result = tools.kill_agent(context, '%1', confirm=True)
    assert result.execute() is False
    assert 'state: waiting' in result.outcome
