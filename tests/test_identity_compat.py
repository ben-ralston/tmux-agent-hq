"""V10 gate: persisted-data compatibility scenarios for #205 Stage 1B.

Tasks 1-7 delivered generic identity (``SessionMetadata``, dual old+new
cache serialization with new-wins-on-disagreement, Codex thread-UUID
capture, snapshot ``stable_id``, ``recover.record_identity``,
``Agent.override_session_id`` everywhere, and dual-key pin membership).
This file is the dedicated gate proving all of that actually survives:

* tmux pane renumbering (pin membership, snapshot/recover identity,
  and phase-override matching), for BOTH the Claude and Codex harnesses.
* version-mixed cache records (old-only / new-only / both-agree /
  both-disagree, new wins) round-tripping through ``Agent.from_dict``/
  ``to_dict``.
* old (pre-``stable_id``) snapshot records read by the new
  ``recover.record_identity``.

Every scenario here is expected to PASS against the current code — a
failure means a real regression in Tasks 1-7, not a test to weaken.
"""

from __future__ import annotations

from unittest.mock import patch

from agent_hq.models import Agent, AgentType, DevPhase
from agent_hq.recover import compute_missing, record_identity
from agent_hq.resolver import resolve_phase_snapshot


def _codex_agent(*, pane_id: str = "%1", cwd: str = "/w") -> Agent:
    return Agent(
        pane_id=pane_id,
        session_name="live",
        window_index=0,
        pane_index=0,
        agent_type=AgentType.CODEX,
        cwd=cwd,
        raw_command="codex",
    )


def _claude_agent(
    *, pane_id: str = "%1", cwd: str = "/w", transcript_id: str | None = None
) -> Agent:
    return Agent(
        pane_id=pane_id,
        session_name="live",
        window_index=0,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd=cwd,
        raw_command="claude",
        claude_transcript_session_id=transcript_id,
    )


def _rec(*, session_id: str | None, stable_id: str | None = None, tool: str, cwd: str) -> dict:
    """Minimal snapshot-record dict — only the fields ``record_identity``/
    ``compute_missing`` actually read."""
    return {
        "session_id": session_id,
        "stable_id": stable_id,
        "tool": tool,
        "cwd": cwd,
    }


def _record(*agent_recs: dict, timestamp: float = 100.0) -> dict:
    return {"timestamp": timestamp, "agents": list(agent_recs)}


def _minimal_dict(**kw) -> dict:
    d = dict(
        pane_id="%1",
        session_name="s",
        window_index=0,
        pane_index=0,
        agent_type="claude",
        cwd="/x",
        raw_command="",
        state="unknown",
    )
    d.update(kw)
    return d


class TestPaneRenumberSurvival:
    """Identity must decouple from the volatile tmux pane_id (#132, #205)."""

    def test_codex_pin_survives_renumber(self):
        before = _codex_agent(pane_id="%1", cwd="/w")
        before.codex_thread_id = "u7"
        store_keys = {before.stable_key}
        after = _codex_agent(pane_id="%99", cwd="/w")
        after.codex_thread_id = "u7"
        assert after.is_pinned_in(store_keys)

    def test_codex_snapshot_identity_survives_renumber(self):
        # Record written pre-renumber (pane %1); live agent now at a
        # different pane (%99) with the same thread UUID → not missing.
        rec = _rec(session_id="uuid-7", stable_id="uuid-7", tool="codex", cwd="/wt/c")
        live = _codex_agent(pane_id="%99", cwd="/wt/c")
        live.codex_thread_id = "uuid-7"
        assert compute_missing(_record(rec), [live]) == []

    def test_claude_pin_survives_renumber(self):
        before = _claude_agent(pane_id="%1", cwd="/w", transcript_id="sess-7")
        store_keys = {before.stable_key}
        after = _claude_agent(pane_id="%99", cwd="/w", transcript_id="sess-7")
        assert after.is_pinned_in(store_keys)

    def test_claude_snapshot_identity_survives_renumber(self):
        rec = _rec(session_id="sess-7", stable_id="sess-7", tool="claude", cwd="/wt/a")
        live = _claude_agent(pane_id="%99", cwd="/wt/a", transcript_id="sess-7")
        assert compute_missing(_record(rec), [live]) == []


class TestMixedRecordRoundTrip:
    """Old+new cache records must resolve consistently through
    ``Agent.from_dict`` — new field wins on disagreement (Task 5/7)."""

    def test_old_only_record(self):
        d = _minimal_dict(agent_type="claude", claude_transcript_session_id="sess-1")
        assert Agent.from_dict(d).claude_transcript_session_id == "sess-1"

    def test_new_only_record(self):
        d = _minimal_dict(agent_type="codex", stable_id="uuid-7")
        agent = Agent.from_dict(d)
        assert agent.codex_thread_id == "uuid-7"
        assert agent.stable_key == "uuid-7"

    def test_both_agree_record(self):
        d = _minimal_dict(
            agent_type="claude", claude_transcript_session_id="sess-1", stable_id="sess-1"
        )
        assert Agent.from_dict(d).claude_transcript_session_id == "sess-1"

    def test_both_disagree_new_wins(self):
        d = _minimal_dict(
            agent_type="claude",
            claude_transcript_session_id="old-sess",
            stable_id="new-sess",
        )
        assert Agent.from_dict(d).claude_transcript_session_id == "new-sess"

    def test_roundtrip_reemits_both_name_sets(self):
        # A record written with only the new name must still emit BOTH
        # the old and new names on the way back out, so an older reader
        # (or a rollback) doesn't regress.
        d = _minimal_dict(agent_type="codex", stable_id="uuid-7")
        back = Agent.from_dict(d).to_dict()
        assert back["harness"] == "codex" and back["agent_type"] == "codex"
        assert back["stable_id"] == "uuid-7" and back["codex_thread_id"] == "uuid-7"


class TestOldSnapshotRecordThroughRecover:
    """A pre-#205 snapshot record has no ``stable_id`` key at all —
    ``record_identity`` must fall back to ``session_id``/``tool:cwd``."""

    def test_old_record_falls_back_to_session_id(self):
        rec = {"session_id": "sess-1", "tool": "claude", "cwd": "/w"}
        assert record_identity(rec) == "sess-1"

    def test_old_sessionless_record_falls_back_to_tool_cwd(self):
        rec = {"tool": "codex", "cwd": "/w"}
        assert record_identity(rec) == "codex:/w"


class TestOverrideSessionMatching:
    """Task 6 switched override matching to ``Agent.override_session_id``
    (codex thread UUID) instead of the claude-only bridge id, so a stored
    entry is matched by thread-UUID *content*, not by the claude-only
    bridge id.

    This is NOT key-survival across a renumber: ``overrides.json`` is
    keyed by the literal ``pane_id`` string (both in
    ``resolve_phase_snapshot``'s ``overrides_table.get(agent.pane_id)``
    lookup and in ``prune_stale_overrides``'s ``by_pane`` match), and
    nothing in the codebase re-keys an entry when its pane's ``%N``
    changes. So an override entry filed under a pane number that later
    gets renumbered does NOT survive for any harness — that's a
    pre-existing property of the pane-keyed store, unchanged by Tasks
    1-7. What Task 6 fixed is narrower and still worth gating here: once
    an entry *is* looked up for a given pane, the session check that
    decides whether it still applies goes through the harness-generic
    ``override_session_id`` instead of the claude-only bridge id, so a
    stale codex entry can't wrongly "apply" (or wrongly get treated as a
    match) via a false both-None comparison."""

    def test_codex_override_entry_matches_on_thread_id(self):
        entry = {
            "action": "override",
            "phase": "qa",
            "auto_phase": "implement",
            "set_at": "2026-05-10T00:00:00Z",
            "note": None,
            "session_id": "uuid-7",
        }
        agent = Agent(
            pane_id="%99",
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CODEX,
            cwd="/repo/wt",
            repo_root="/repo",
            branch="feature/x",
            raw_command="codex",
            codex_thread_id="uuid-7",
        )
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = resolve_phase_snapshot(
                agent=agent,
                overrides_table={"%99": entry},
                overrides_revision=1,
                producer="daemon",
                producer_epoch=0.0,
                sequence=0,
            )
        assert snap.effective_phase is DevPhase.QA
        assert snap.feedback is not None
        assert snap.feedback.action == "override"
        assert snap.feedback.phase is DevPhase.QA
