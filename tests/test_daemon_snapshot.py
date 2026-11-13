"""The daemon poll loop appends a durable agent snapshot each tick. #125"""

from __future__ import annotations

from agent_hq import daemon, snapshots
from agent_hq.models import Agent, AgentState, AgentType


def _agent() -> Agent:
    return Agent(
        pane_id="%5",
        session_name="agent-hq-125",
        window_index=1,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd="/wt/a",
        repo_name="agent-hq",
        branch="feature/125-x",
        raw_command="claude",
        state=AgentState.RUNNING,
        claude_transcript_session_id="sess-abc",
    )


def test_poll_loop_appends_snapshot(tmp_path, monkeypatch):
    snap_file = tmp_path / "agent-snapshots.jsonl"
    monkeypatch.setattr(snapshots, "_DEFAULT_SNAPSHOTS_FILE", snap_file)

    agent = _agent()
    monkeypatch.setattr(daemon, "get_all_panes", lambda: [])
    monkeypatch.setattr(daemon, "discover_agents", lambda: [])
    monkeypatch.setattr(daemon, "enrich_all_agents", lambda raw, **kw: [agent])
    monkeypatch.setattr(daemon, "read_overrides", lambda: ({}, 0))
    monkeypatch.setattr(daemon, "prune_stale_overrides", lambda agents: None)
    monkeypatch.setattr(daemon, "prune_stale_hook_state_files", lambda ids, by_pane: None)
    monkeypatch.setattr(daemon, "resolve_phase_snapshot", lambda **kw: None)
    monkeypatch.setattr(daemon, "write_cache", lambda *a, **k: None)

    # Break the loop after exactly one tick (sleep raises -> loop breaks).
    def _stop(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", _stop)

    daemon.run_poll_loop(interval=0.0)

    recs = snapshots.SnapshotStore(snap_file).records()
    assert len(recs) == 1
    assert recs[0]["agents"][0]["pane"] == "%5"
    assert recs[0]["agents"][0]["session_id"] == "sess-abc"
