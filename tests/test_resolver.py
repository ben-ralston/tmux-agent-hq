"""Tests for ``agent_hq.resolver.resolve_phase_snapshot``.

The resolver is the single boundary that turns hook state + overrides
into a ``PhaseSnapshot``. Pure read: it must not write to disk or mutate
state files. Coverage here walks every override branch (5-state matrix),
every detected source (``tag`` → ``rule`` → ``none``), confirm-vs-
override ``effective_phase`` semantics, and the pure-read invariant.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_hq.models import Agent, AgentState, AgentType, DevPhase
from agent_hq.resolver import resolve_phase_snapshot
from agent_hq.transcript import transcript_path_for


def _agent(
    pane_id: str = "%5",
    *,
    repo_root: str | None = "/repo",
    branch: str | None = "feature/x",
    claude_session_id: str | None = "bridge-A",
    claude_transcript_session_id: str | None = "trans-A",
) -> Agent:
    return Agent(
        pane_id=pane_id,
        session_name="s",
        window_index=0,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd="/repo/wt",
        repo_root=repo_root,
        repo_name="repo",
        branch=branch,
        raw_command="claude",
        state=AgentState.IDLE,
        claude_session_id=claude_session_id,
        claude_transcript_session_id=claude_transcript_session_id,
    )


def _resolve(agent, *, overrides=None, overrides_rev=0):
    return resolve_phase_snapshot(
        agent=agent,
        overrides_table=overrides or {},
        overrides_revision=overrides_rev,
        producer="daemon",
        producer_epoch=100.0,
        sequence=7,
    )


@pytest.fixture
def hook_dir(tmp_path):
    """Isolate both resolver inputs into ``tmp_path``:

    * ``HOOK_STATE_DIR`` → ``tmp_path/states`` (revision + session validity)
    * ``transcript.CLAUDE_PROJECTS_DIR`` → ``tmp_path/projects`` (phase)

    Yields the state dir (its historical contract). With an empty
    projects dir, ``transcript_path_for`` resolves to a non-existent
    file and the resolver deterministically falls back to the branch
    rule — no chance of reading the developer's real ~/.claude.
    """
    state_dir = tmp_path / "states"
    state_dir.mkdir()
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    with (
        patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
        patch("agent_hq.transcript.CLAUDE_PROJECTS_DIR", projects_dir),
    ):
        yield state_dir


def _write_state(hook_dir, pane_num, payload):
    (hook_dir / f"{pane_num}.json").write_text(json.dumps(payload))


def _write_transcript(tmp_path, agent, *, records=None, assistant_texts=None):
    """Write ``agent``'s JSONL transcript under the patched projects dir.

    Uses ``transcript_path_for`` itself for the path so the test's
    encoding can never silently drift from production's (the dot bug
    in fddba81 escaped precisely because a helper hand-rolled the
    ``/``-only encoding). Writes to the primary (current-scheme)
    candidate — index 0 — matching what a live Claude Code process
    writes today. Pass ``records`` for full control (mixed
    assistant/system/user shapes, e.g. the #73 Test A repro) or
    ``assistant_texts`` for the common all-assistant case. Returns the
    written path.
    """
    tpath = Path(transcript_path_for(agent.cwd, agent.claude_transcript_session_id)[0])
    tpath.parent.mkdir(parents=True, exist_ok=True)
    if records is None:
        records = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}
            for t in (assistant_texts or [])
        ]
    tpath.write_text("\n".join(json.dumps(r) for r in records))
    return tpath


# --- detected-phase sources ---------------------------------------------------


class TestDetectedSource:
    def test_transcript_tag_wins(self, hook_dir, tmp_path):
        """A ``<phase>`` tag in the transcript is authoritative — the
        branch rule is never consulted."""
        agent = _agent("%5")
        _write_transcript(tmp_path, agent, assistant_texts=["Did the thing.\n\n<phase>qa</phase>"])
        with patch("agent_hq.resolver.detect_branch_phase") as rule:
            snap = _resolve(agent)
        assert snap.detected_phase is DevPhase.QA
        assert snap.phase_source == "tag"
        rule.assert_not_called()

    def test_last_assistant_tag_wins_skipping_system_record(self, hook_dir, tmp_path):
        """#73 Test A regression. Transcript order: ``qa`` (assistant),
        then ``brainstorm`` (assistant — what Claude last emitted), then
        a trailing ``system`` ``done`` notice. The resolver must report
        ``brainstorm`` — the last *assistant* tag — not the stale ``qa``
        nor the ``system`` ``done``."""
        agent = _agent("%5")
        _write_transcript(
            tmp_path,
            agent,
            records=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Testing.\n\n<phase>qa</phase>"}]
                    },
                },
                {"type": "user", "message": {"content": "now emit brainstorm"}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Okay.\n\n<phase>brainstorm</phase>"}]
                    },
                },
                {"type": "system", "content": "<phase>done</phase>"},
            ],
        )
        with patch("agent_hq.resolver.detect_branch_phase") as rule:
            snap = _resolve(agent)
        # Legacy ``<phase>brainstorm</phase>`` tag normalizes to design (#108).
        assert snap.detected_phase is DevPhase.DESIGN
        assert snap.phase_source == "tag"
        rule.assert_not_called()

    def test_stat_cache_skips_rescan_when_transcript_unchanged(self, hook_dir, tmp_path):
        """When ``previous_snapshot`` observed the same transcript
        mtime+size for the same sessionId, the scan is skipped and the
        prior phase is reused (the daemon/watcher fast-path)."""
        agent = _agent("%5")
        _write_transcript(agent=agent, tmp_path=tmp_path, assistant_texts=["<phase>plan</phase>"])
        first = _resolve(agent)
        # Legacy ``<phase>plan</phase>`` tag normalizes to design (#108).
        assert first.detected_phase is DevPhase.DESIGN

        with patch("agent_hq.resolver.get_adapter") as adapter_lookup:
            second = resolve_phase_snapshot(
                agent=agent,
                overrides_table={},
                overrides_revision=0,
                producer="daemon",
                producer_epoch=100.0,
                sequence=8,
                previous_snapshot=first,
            )
        # Fast path never even needs the adapter — no transcript re-scan.
        adapter_lookup.assert_not_called()
        assert second.detected_phase is DevPhase.DESIGN
        assert second.phase_source == "tag"
        assert (
            second.inputs_observed.transcript_mtime_ns == first.inputs_observed.transcript_mtime_ns
        )

    def test_stat_cache_no_tag_skips_rescan_but_reruns_rule(self, hook_dir, tmp_path):
        """The ``elif reuse_ok: pass`` branch: transcript byte-identical
        but it previously yielded no tag (phase_source=='rule'). The scan
        is skipped (it provably still has no tag) yet the branch rule is
        re-run, since rule inputs (PR state, commits) change independently
        of the transcript."""
        agent = _agent("%5")
        _write_transcript(tmp_path, agent, assistant_texts=["no tag here at all"])
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            first = _resolve(agent)
        assert first.detected_phase is DevPhase.IMPLEMENT
        assert first.phase_source == "rule"

        with (
            patch("agent_hq.resolver.get_adapter") as adapter_lookup,
            patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.REVIEW) as rule,
        ):
            second = resolve_phase_snapshot(
                agent=agent,
                overrides_table={},
                overrides_revision=0,
                producer="daemon",
                producer_epoch=100.0,
                sequence=9,
                previous_snapshot=first,
            )
        adapter_lookup.assert_not_called()  # unchanged + no prior tag → no rescan
        rule.assert_called_once()  # but the rule re-runs (its inputs move freely)
        assert second.detected_phase is DevPhase.REVIEW
        assert second.phase_source == "rule"

    def test_dotted_worktree_cwd_resolves_transcript(self, hook_dir, tmp_path):
        """Resolver-level regression for fddba81: a cwd containing ``.``
        (every ``repo.feature-N`` worktree) must still resolve its
        transcript. The helper now routes through ``transcript_path_for``,
        so an encoding regression fails here, not just in live QA."""
        agent = _agent(
            "%7",
            claude_transcript_session_id="trans-dot",
        )
        agent.cwd = "/Users/x/.claude-worktrees/agent-hq.feature-73-part2"
        _write_transcript(tmp_path, agent, assistant_texts=["Working.\n\n<phase>qa</phase>"])
        with patch("agent_hq.resolver.detect_branch_phase") as rule:
            snap = _resolve(agent)
        assert snap.detected_phase is DevPhase.QA
        assert snap.phase_source == "tag"
        rule.assert_not_called()

    def test_underscore_cwd_resolves_transcript(self, hook_dir, tmp_path):
        """AHQ-003 (#180): a cwd containing ``_`` must still resolve its
        transcript. Current Claude Code (~mid-2026) replaces ``_`` with
        ``-`` in the project dir name; before this fix the resolver's
        stat 404'd, every ``<phase>`` tag was silently ignored, and phase
        fell back to the branch rule forever."""
        agent = _agent(
            "%8",
            claude_transcript_session_id="trans-underscore",
        )
        agent.cwd = "/Users/x/agent_hq"
        _write_transcript(tmp_path, agent, assistant_texts=["Working.\n\n<phase>qa</phase>"])
        with patch("agent_hq.resolver.detect_branch_phase") as rule:
            snap = _resolve(agent)
        assert snap.detected_phase is DevPhase.QA
        assert snap.phase_source == "tag"
        rule.assert_not_called()

    def test_underscore_cwd_falls_back_to_legacy_transcript_path(self, hook_dir, tmp_path):
        """A transcript directory created by a pre-change Claude Code
        version keeps its original underscore-preserving name on disk
        forever. The resolver must fall back to that legacy path when
        the current-scheme directory doesn't exist."""
        agent = _agent(
            "%9",
            claude_transcript_session_id="trans-legacy",
        )
        agent.cwd = "/Users/x/agent_hq"
        legacy_path = Path(transcript_path_for(agent.cwd, agent.claude_transcript_session_id)[1])
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Done.\n\n<phase>ready</phase>"}]
                    },
                }
            )
        )
        with patch("agent_hq.resolver.detect_branch_phase") as rule:
            snap = _resolve(agent)
        assert snap.detected_phase is DevPhase.READY
        assert snap.phase_source == "tag"
        rule.assert_not_called()

    def test_no_transcript_falls_back_to_rule(self, hook_dir):
        """sessionId present but no JSONL on disk → rule fallback,
        transcript axis stays 0."""
        with patch(
            "agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT
        ) as rule:
            snap = _resolve(_agent("%5"))
        rule.assert_called_once()
        assert snap.detected_phase is DevPhase.IMPLEMENT
        assert snap.phase_source == "rule"
        assert snap.inputs_observed.transcript_mtime_ns == 0
        assert snap.inputs_observed.transcript_size == 0

    def test_unknown_transcript_session_id_falls_back_to_rule(self, hook_dir, tmp_path):
        """No transcript sessionId (older Claude) → path None → rule."""
        agent = _agent("%5", claude_transcript_session_id=None)
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(agent)
        assert snap.detected_phase is DevPhase.IMPLEMENT
        assert snap.phase_source == "rule"
        assert snap.inputs_observed.transcript_mtime_ns == 0

    def test_falls_back_to_rule_when_no_hook_phase(self, hook_dir):
        # No state file → hook validity "missing" → rule fallback.
        with patch(
            "agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT
        ) as rule:
            snap = _resolve(_agent("%5"))
        rule.assert_called_once()
        assert snap.detected_phase is DevPhase.IMPLEMENT
        assert snap.phase_source == "rule"
        assert snap.inputs_observed.hook_state_revision == 0

    def test_none_when_no_hook_no_repo(self, hook_dir):
        snap = _resolve(_agent("%5", repo_root=None, branch=None))
        assert snap.detected_phase is None
        assert snap.phase_source == "none"

    def test_none_when_rule_returns_none(self, hook_dir):
        with patch("agent_hq.resolver.detect_branch_phase", return_value=None):
            snap = _resolve(_agent("%5"))
        assert snap.detected_phase is None
        assert snap.phase_source == "none"

    def test_hook_mismatch_falls_through_to_rule(self, hook_dir):
        """A state file from a different transcript session → ``mismatch``
        validity → resolver ignores it and uses the rule fallback."""
        _write_state(
            hook_dir,
            "5",
            {
                "revision": 9,
                "phase": "qa",
                "phase_source": "tag",
                "session_id": "trans-OLD",
                "timestamp": 1.0,
            },
        )
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(_agent("%5"))
        assert snap.detected_phase is DevPhase.IMPLEMENT
        assert snap.phase_source == "rule"
        # Revision still travels back even on mismatch.
        assert snap.inputs_observed.hook_state_revision == 9

    def test_tombstone_falls_through_to_rule_carrying_revision(self, hook_dir):
        _write_state(
            hook_dir,
            "5",
            {"revision": 11, "tombstone": True, "session_id": None, "timestamp": 1.0},
        )
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.DESIGN):
            snap = _resolve(_agent("%5"))
        assert snap.detected_phase is DevPhase.DESIGN
        assert snap.inputs_observed.hook_state_revision == 11

    def test_rule_fallback_passes_real_idle_flag(self, hook_dir):
        """The rule fallback receives the agent's actual IDLE-ness, not a
        hardcoded ``False`` — so ``detect_branch_phase``'s idle-gated
        branches see the truth (regression: the resolver used to pin it
        to ``False``)."""
        idle = _agent("%5")  # _agent defaults state=AgentState.IDLE
        running = _agent("%6")
        running.state = AgentState.RUNNING
        with patch(
            "agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT
        ) as rule:
            _resolve(idle)
            _resolve(running)
        assert rule.call_args_list[0].kwargs["agent_state_idle"] is True
        assert rule.call_args_list[1].kwargs["agent_state_idle"] is False


# --- override / confirm 5-state matrix ---------------------------------------


class TestOverrideMatrix:
    def _override_entry(
        self, *, phase="qa", auto="implement", action="override", session_id="bridge-A"
    ):
        return {
            "action": action,
            "phase": phase,
            "auto_phase": auto,
            "set_at": "2026-05-10T00:00:00Z",
            "note": None,
            "session_id": session_id,
        }

    def test_override_with_matching_session_applies(self, hook_dir):
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(
                _agent("%5", claude_session_id="bridge-A"),
                overrides={"%5": self._override_entry(session_id="bridge-A")},
                overrides_rev=4,
            )
        assert snap.detected_phase is DevPhase.IMPLEMENT
        assert snap.effective_phase is DevPhase.QA
        assert snap.feedback is not None
        assert snap.feedback.action == "override"
        assert snap.feedback.phase is DevPhase.QA
        assert snap.feedback.auto_phase is DevPhase.IMPLEMENT
        assert snap.inputs_observed.overrides_revision == 4

    def test_codex_override_matches_on_thread_id(self, hook_dir):
        """Codex agents match override entries via codex_thread_id (#205
        Task 6): the resolver's session comparison must go through
        ``Agent.override_session_id``, not the claude-only bridge id."""
        agent = Agent(
            pane_id="%5",
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CODEX,
            cwd="/repo/wt",
            repo_root="/repo",
            branch="feature/x",
            raw_command="codex",
            state=AgentState.IDLE,
            codex_thread_id="uuid-7",
        )
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(
                agent,
                overrides={"%5": self._override_entry(session_id="uuid-7")},
                overrides_rev=4,
            )
        assert snap.detected_phase is DevPhase.IMPLEMENT
        assert snap.effective_phase is DevPhase.QA
        assert snap.feedback is not None
        assert snap.feedback.action == "override"
        assert snap.feedback.phase is DevPhase.QA
        assert snap.feedback.auto_phase is DevPhase.IMPLEMENT
        assert snap.inputs_observed.overrides_revision == 4

    def test_override_both_session_ids_none_applies(self, hook_dir):
        """Non-Claude agents: stored=None, current=None → apply."""
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(
                _agent("%5", claude_session_id=None),
                overrides={"%5": self._override_entry(session_id=None)},
            )
        assert snap.effective_phase is DevPhase.QA
        assert snap.feedback.action == "override"

    def test_override_positive_mismatch_not_applied(self, hook_dir):
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(
                _agent("%5", claude_session_id="bridge-NEW"),
                overrides={"%5": self._override_entry(session_id="bridge-OLD")},
            )
        # Mismatch → no feedback, effective == detected.
        assert snap.feedback is None
        assert snap.effective_phase is DevPhase.IMPLEMENT

    def test_override_exactly_one_none_not_applied(self, hook_dir):
        """Transient bridge-id read failure (agent's id is None) → don't
        apply, don't surface feedback. The next successful read resolves it."""
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(
                _agent("%5", claude_session_id=None),
                overrides={"%5": self._override_entry(session_id="bridge-A")},
            )
        assert snap.feedback is None
        assert snap.effective_phase is DevPhase.IMPLEMENT

    def test_override_missing_session_key_not_applied(self, hook_dir):
        """Pre-#65 entry with no ``session_id`` key → ``legacy`` → ignored."""
        entry = self._override_entry()
        del entry["session_id"]
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(_agent("%5"), overrides={"%5": entry})
        assert snap.feedback is None
        assert snap.effective_phase is DevPhase.IMPLEMENT

    def test_confirm_does_not_change_effective_phase(self, hook_dir):
        """A confirm acknowledges the auto-detected value — effective
        stays detected — but still surfaces in ``feedback`` (so the UI
        shows the badge and the fingerprint changes)."""
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(
                _agent("%5", claude_session_id="bridge-A"),
                overrides={
                    "%5": self._override_entry(action="confirm", phase=None, session_id="bridge-A")
                },
            )
        assert snap.detected_phase is DevPhase.IMPLEMENT
        assert snap.effective_phase is DevPhase.IMPLEMENT  # unchanged
        assert snap.feedback is not None
        assert snap.feedback.action == "confirm"
        assert snap.feedback.phase is None

    def test_unknown_action_ignored(self, hook_dir):
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(
                _agent("%5"),
                overrides={"%5": self._override_entry(action="frobozz")},
            )
        assert snap.feedback is None

    def test_no_override_entry_for_pane(self, hook_dir):
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(_agent("%5"), overrides={"%9": self._override_entry()})
        assert snap.feedback is None
        assert snap.effective_phase is DevPhase.IMPLEMENT

    def test_override_with_unknown_phase_string_falls_back_to_detected(self, hook_dir):
        """Corrupt ``phase`` value in the entry → feedback.phase is None →
        no effective override."""
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(
                _agent("%5", claude_session_id="bridge-A"),
                overrides={"%5": self._override_entry(phase="not-a-phase", session_id="bridge-A")},
            )
        # feedback still recorded (action + auto_phase), but no usable phase.
        assert snap.feedback is not None
        assert snap.feedback.phase is None
        assert snap.effective_phase is DevPhase.IMPLEMENT

    def _codex_agent(self, *, codex_thread_id):
        return Agent(
            pane_id="%5",
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CODEX,
            cwd="/repo/wt",
            repo_root="/repo",
            branch="feature/x",
            raw_command="codex",
            state=AgentState.IDLE,
            codex_thread_id=codex_thread_id,
        )

    def test_codex_override_applies_when_stored_none_current_uuid(self, hook_dir):
        """Override written pre-capture (stored None) still applies once the
        codex thread id is captured (#205 fix-wave Fix 2)."""
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(
                self._codex_agent(codex_thread_id="uuid-7"),
                overrides={"%5": self._override_entry(session_id=None)},
            )
        assert snap.feedback is not None
        assert snap.feedback.action == "override"
        assert snap.effective_phase is DevPhase.QA

    def test_codex_override_applies_when_stored_uuid_current_none(self, hook_dir):
        """Override stored with a captured UUID still applies when the
        current thread id has since been cleared (dedupe / 3-day cutoff /
        capture-after-write) -- codex ids flap, unlike claude bridge ids."""
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(
                self._codex_agent(codex_thread_id=None),
                overrides={"%5": self._override_entry(session_id="uuid-7")},
            )
        assert snap.feedback is not None
        assert snap.feedback.action == "override"
        assert snap.effective_phase is DevPhase.QA

    def test_codex_override_positive_mismatch_not_applied(self, hook_dir):
        """A genuine UUID mismatch (not a None flap) still drops for codex,
        same as claude."""
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.IMPLEMENT):
            snap = _resolve(
                self._codex_agent(codex_thread_id="uuid-NEW"),
                overrides={"%5": self._override_entry(session_id="uuid-OLD")},
            )
        assert snap.feedback is None
        assert snap.effective_phase is DevPhase.IMPLEMENT


# --- snapshot metadata + pure-read invariant ----------------------------------


class TestSnapshotMetadata:
    def test_carries_producer_epoch_sequence(self, hook_dir):
        snap = resolve_phase_snapshot(
            agent=_agent("%5", repo_root=None, branch=None),
            overrides_table={},
            overrides_revision=0,
            producer="watcher",
            producer_epoch=999.0,
            sequence=12,
        )
        assert snap.producer == "watcher"
        assert snap.producer_epoch == 999.0
        assert snap.sequence == 12
        assert snap.monotonic_ns > 0
        assert snap.timestamp > 0

    def test_phase_session_id_is_transcript_id(self, hook_dir):
        snap = _resolve(_agent("%5", claude_transcript_session_id="trans-XYZ"))
        assert snap.phase_session_id == "trans-XYZ"

    def test_pure_read_no_disk_writes(self, hook_dir):
        """The resolver must not write or mutate any file. Snapshot the
        state dir before and after — it must be byte-identical."""
        _write_state(
            hook_dir,
            "5",
            {
                "revision": 2,
                "phase": "qa",
                "phase_source": "tag",
                "session_id": "trans-A",
                "timestamp": 1.0,
            },
        )
        before = (hook_dir / "5.json").read_text()
        files_before = sorted(p.name for p in hook_dir.iterdir())

        _resolve(
            _agent("%5"),
            overrides={
                "%5": {
                    "action": "override",
                    "phase": "review",
                    "auto_phase": "qa",
                    "set_at": "2026-05-10T00:00:00Z",
                    "note": None,
                    "session_id": "bridge-A",
                }
            },
            overrides_rev=3,
        )

        assert (hook_dir / "5.json").read_text() == before
        assert sorted(p.name for p in hook_dir.iterdir()) == files_before


CODEX_FIXTURES = Path(__file__).parent / "fixtures" / "codex_rollouts"
CODEX_PLAIN_ID = "00000000-0000-7000-8000-000000000001"


def _codex_agent(
    *,
    thread_id: str | None = CODEX_PLAIN_ID,
    rollout_path: str | None = None,
    repo_root: str | None = "/repo",
    branch: str | None = "feature/x",
) -> Agent:
    return Agent(
        pane_id="%9",
        session_name="s",
        window_index=0,
        pane_index=0,
        agent_type=AgentType.CODEX,
        cwd="/repo/wt",
        repo_root=repo_root,
        repo_name="repo",
        branch=branch,
        raw_command="codex",
        state=AgentState.IDLE,
        codex_thread_id=thread_id,
        codex_rollout_path=rollout_path,
    )


class TestCodexPhaseResolution:
    """#206: codex phase from the rollout via the adapter, inside the
    same pure-read boundary and fast-path rules as claude."""

    def test_phase_from_fixture_rollout(self):
        agent = _codex_agent(rollout_path=str(CODEX_FIXTURES / "plain.jsonl"))
        snap = _resolve(agent)
        assert snap.detected_phase is DevPhase.IMPLEMENT
        assert snap.phase_source == "tag"
        assert snap.phase_session_id == CODEX_PLAIN_ID
        assert snap.inputs_observed.transcript_mtime_ns > 0
        assert snap.inputs_observed.transcript_size > 0

    def test_meta_mismatch_falls_back_to_rule(self):
        agent = _codex_agent(
            thread_id="99999999-9999-7999-8999-999999999999",
            rollout_path=str(CODEX_FIXTURES / "plain.jsonl"),
        )
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.DESIGN):
            snap = _resolve(agent)
        assert snap.detected_phase is DevPhase.DESIGN
        assert snap.phase_source == "rule"

    def test_no_rollout_path_falls_back_to_rule(self):
        agent = _codex_agent(rollout_path=None)
        with patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.DESIGN):
            snap = _resolve(agent)
        assert snap.phase_source == "rule"
        assert snap.inputs_observed.transcript_mtime_ns == 0

    def test_fast_path_reuses_previous_snapshot(self):
        agent = _codex_agent(rollout_path=str(CODEX_FIXTURES / "plain.jsonl"))
        first = _resolve(agent)
        calls = []
        real_adapter = __import__("agent_hq.adapters", fromlist=["get_adapter"]).get_adapter(
            AgentType.CODEX
        )

        class CountingAdapter:
            def transcript_phase(self, path, sid):
                calls.append(path)
                return real_adapter.transcript_phase(path, sid)

        with patch("agent_hq.resolver.get_adapter", return_value=CountingAdapter()):
            second = resolve_phase_snapshot(
                agent=agent,
                overrides_table={},
                overrides_revision=0,
                producer="daemon",
                producer_epoch=100.0,
                sequence=8,
                previous_snapshot=first,
            )
        assert second.detected_phase is DevPhase.IMPLEMENT
        assert second.phase_source == "tag"
        assert calls == []  # byte-identical transcript → no re-scan

    def test_adapter_exception_degrades_to_rule(self):
        agent = _codex_agent(rollout_path=str(CODEX_FIXTURES / "plain.jsonl"))

        class BoomAdapter:
            def transcript_phase(self, path, sid):
                raise RuntimeError("parser bug")

        with (
            patch("agent_hq.resolver.get_adapter", return_value=BoomAdapter()),
            patch("agent_hq.resolver.detect_branch_phase", return_value=DevPhase.DESIGN),
        ):
            snap = _resolve(agent)
        assert snap.detected_phase is DevPhase.DESIGN
        assert snap.phase_source == "rule"

    def test_subagent_child_rollout_never_binds(self):
        agent = _codex_agent(
            thread_id="00000000-0000-7000-8000-000000000005",
            rollout_path=str(CODEX_FIXTURES / "subagent-child.jsonl"),
        )
        with patch("agent_hq.resolver.detect_branch_phase", return_value=None):
            snap = _resolve(agent)
        assert snap.detected_phase is None
        assert snap.phase_source == "none"
