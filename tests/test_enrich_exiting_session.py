"""An exiting Claude session keeps its session id for its last ticks (#433).

Claude Code deletes its registry file (``~/.claude/sessions/<pid>.json``) as
soon as it starts shutting down, about a second before the process exits
(measured 2026-11-10: 0.17 s after ``/exit`` the file was gone, the process
lived to 1.14 s; on SIGTERM 0.05 s and 1.37 s). A daemon tick in that window
still discovers a live ``claude`` in the pane but reads no session id, so the
snapshot store recorded ``session_id: null`` for it — and on a reboot that
record is the last full roster before the drop, which ``recover`` picks as
its baseline (#410). The previous tick's record for the same process (same
pane, same known pid) still knows the id; these pin that it is carried.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent_hq import enrichment
from agent_hq.models import Agent, AgentState, AgentType


def _raw(pane_id: str = "%7", pid: int | None = 4242) -> dict:
    return {
        "pane_id": pane_id,
        "session_name": "s",
        "window_index": 0,
        "pane_index": 0,
        "command": "claude",
        "agent_pid": pid,
    }


def _enrich_from_registry(raw: dict) -> Agent:
    """``enrich_agent`` reduced to its session-id read: the real registry
    lookup, nothing else (git, tmux and live status are out of scope)."""
    bridge, transcript = enrichment.get_claude_session_ids(raw.get("agent_pid"))
    return Agent(
        pane_id=raw["pane_id"],
        session_name=raw["session_name"],
        window_index=raw["window_index"],
        pane_index=raw["pane_index"],
        agent_type=AgentType.from_command(raw["command"]),
        cwd="/work",
        agent_pid=raw.get("agent_pid"),
        state=AgentState.UNKNOWN,
        claude_session_id=bridge,
        claude_transcript_session_id=transcript,
    )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(enrichment, "CLAUDE_SESSIONS_DIR", sessions)

    def write(pid: int, session_id: str, bridge: str | None = "bridge-1") -> None:
        record = {"pid": pid, "sessionId": session_id}
        if bridge:
            record["bridgeSessionId"] = bridge
        (sessions / f"{pid}.json").write_text(json.dumps(record))

    def remove(pid: int) -> None:
        (sessions / f"{pid}.json").unlink()

    write.remove = remove
    return write


def _tick(raws, previous=None):
    with patch.object(enrichment, "enrich_agent", side_effect=_enrich_from_registry):
        agents = enrichment.enrich_all_agents(raws, previous=previous)
    return {a.pane_id: a for a in agents}


class TestExitingSessionKeepsItsId:
    def test_registry_removed_before_exit_keeps_previous_ids(self, registry):
        registry(4242, "sess-a")
        first = _tick([_raw()])
        assert first["%7"].claude_transcript_session_id == "sess-a"

        registry.remove(4242)  # shutdown began; the process is still alive
        second = _tick([_raw()], previous=first)

        agent = second["%7"]
        assert agent.claude_transcript_session_id == "sess-a"
        assert agent.claude_session_id == "bridge-1"
        # Same identity: snapshots keep the id, the web card isn't re-keyed.
        assert agent.stable_key == first["%7"].stable_key

    def test_carried_id_survives_consecutive_registry_less_ticks(self, registry):
        registry(4242, "sess-a")
        ticks = [_tick([_raw()])]
        registry.remove(4242)
        for _ in range(3):
            ticks.append(_tick([_raw()], previous=ticks[-1]))
        assert ticks[-1]["%7"].claude_transcript_session_id == "sess-a"

    @pytest.mark.parametrize(
        "prior_pid, raw_pid", [(4242, 5151), (None, None)], ids=["other-process", "pid-unknown"]
    )
    def test_another_or_unknown_process_never_inherits_the_id(self, registry, prior_pid, raw_pid):
        prior = _enrich_from_registry(_raw(pid=prior_pid))
        prior.claude_transcript_session_id = "sess-old"
        prior.claude_session_id = "bridge-old"

        agent = _tick([_raw(pid=raw_pid)], previous={"%7": prior})["%7"]

        assert agent.claude_transcript_session_id is None
        assert agent.claude_session_id is None

    def test_registry_value_wins_over_the_previous_tick(self, registry):
        """``/clear`` rewrites the registry in place with a new sessionId."""
        registry(4242, "sess-a")
        first = _tick([_raw()])
        registry(4242, "sess-b", bridge="bridge-2")

        agent = _tick([_raw()], previous=first)["%7"]

        assert agent.claude_transcript_session_id == "sess-b"
        assert agent.claude_session_id == "bridge-2"

    def test_startup_before_the_registry_is_written_has_no_id(self, registry):
        """Nothing to carry: no tick has seen this process with an id yet."""
        agent = _tick([_raw()])["%7"]
        assert agent.claude_transcript_session_id is None


class TestCarryReviewRound1:
    def test_live_status_is_judged_against_the_carried_id(self, registry):
        """``enrich_agent`` overlaid live status while the id was None; the
        carry re-applies it so state and identity agree."""
        registry(4242, "sess-a")
        first = _tick([_raw()])
        registry.remove(4242)

        seen: list[str | None] = []

        def record(agents, **kwargs):
            seen.extend(a.claude_transcript_session_id for a in agents)
            return agents

        with patch("agent_hq.status_collector.apply_live_status", side_effect=record):
            _tick([_raw()], previous=first)

        assert seen == ["sess-a"]

    def test_unreadable_registry_file_is_not_an_exit(self, registry, tmp_path):
        """A torn read (``/clear`` rewriting in place) or malformed file means
        the registry is still there: no carry, the old session must not stick."""
        registry(4242, "sess-a")
        first = _tick([_raw()])
        (enrichment.CLAUDE_SESSIONS_DIR / "4242.json").write_text('{"pid": 4242, "sess')

        agent = _tick([_raw()], previous=first)["%7"]

        assert agent.claude_transcript_session_id is None
        assert agent.claude_session_id is None

    def test_missing_sessions_dir_is_an_outage_not_an_exit(self, registry):
        """Review round 2: with the whole registry gone every pane would look
        like it is exiting; that is a registry outage, so nothing is carried."""
        registry(4242, "sess-a")
        first = _tick([_raw()])
        registry.remove(4242)
        enrichment.CLAUDE_SESSIONS_DIR.rmdir()

        agent = _tick([_raw()], previous=first)["%7"]

        assert agent.claude_transcript_session_id is None
