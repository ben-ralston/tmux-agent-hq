"""Tests for Agent serialization, PhaseSnapshot, PhaseFeedback, and ``replaces``."""

import pytest

from agent_hq.models import (
    Agent,
    AgentState,
    AgentType,
    DevPhase,
    FileStats,
    InputsObserved,
    PhaseFeedback,
    PhaseSnapshot,
    _phase_from_value,
    normalize_phase_value,
    replaces,
)


def _snap(
    *,
    detected: DevPhase | None = None,
    effective: DevPhase | None = None,
    source: str = "rule",
    session_id: str | None = None,
    feedback: PhaseFeedback | None = None,
    overrides_rev: int = 0,
    hook_rev: int = 0,
    transcript_mtime_ns: int = 0,
    transcript_size: int = 0,
    producer: str = "daemon",
    monotonic_ns: int = 0,
) -> PhaseSnapshot:
    """Build a PhaseSnapshot for tests. Only the fields a test cares about
    need to be passed; everything else gets a zero/default."""
    return PhaseSnapshot(
        detected_phase=detected,
        effective_phase=effective if effective is not None else detected,
        phase_source=source,
        phase_session_id=session_id,
        feedback=feedback,
        inputs_observed=InputsObserved(
            overrides_revision=overrides_rev,
            hook_state_revision=hook_rev,
            transcript_mtime_ns=transcript_mtime_ns,
            transcript_size=transcript_size,
        ),
        producer=producer,
        producer_epoch=0.0,
        sequence=0,
        monotonic_ns=monotonic_ns,
        timestamp=0.0,
    )


def _minimal_dict(**kw):
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


class TestAgentSerialization:
    """Tests for Agent.to_dict() and Agent.from_dict()."""

    def test_to_dict_basic(self):
        agent = Agent(
            pane_id="%5",
            session_name="main",
            window_index=1,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/home/user/project",
            repo_root="/home/user/project",
            repo_name="my-project",
            branch="main",
            worktree_name=None,
            raw_command="claude",
            state=AgentState.RUNNING,
        )
        d = agent.to_dict()
        assert d["pane_id"] == "%5"
        assert d["session_name"] == "main"
        assert d["window_index"] == 1
        assert d["pane_index"] == 0
        assert d["agent_type"] == "claude"
        assert d["cwd"] == "/home/user/project"
        assert d["repo_root"] == "/home/user/project"
        assert d["repo_name"] == "my-project"
        assert d["branch"] == "main"
        assert d["worktree_name"] is None
        assert d["raw_command"] == "claude"
        assert d["state"] == "running"
        assert d["phase_snapshot"] is None
        assert d["claude_transcript_session_id"] is None

    def test_from_dict_basic(self):
        d = {
            "pane_id": "%5",
            "session_name": "main",
            "window_index": 1,
            "pane_index": 0,
            "agent_type": "claude",
            "cwd": "/home/user/project",
            "repo_root": "/home/user/project",
            "repo_name": "my-project",
            "branch": "main",
            "worktree_name": None,
            "raw_command": "claude",
            "state": "running",
        }
        agent = Agent.from_dict(d)
        assert agent.pane_id == "%5"
        assert agent.agent_type == AgentType.CLAUDE
        assert agent.state == AgentState.RUNNING
        assert agent.repo_root == "/home/user/project"
        assert agent.phase is None

    def test_roundtrip(self):
        original = Agent(
            pane_id="%8",
            session_name="work",
            window_index=2,
            pane_index=1,
            agent_type=AgentType.CODEX,
            cwd="/tmp/test",
            repo_root=None,
            repo_name=None,
            branch=None,
            worktree_name=None,
            raw_command="codex",
            state=AgentState.IDLE,
        )
        restored = Agent.from_dict(original.to_dict())
        assert restored == original

    def test_roundtrip_native_name_fields(self):
        """#287: the registry name + its source survive the daemon cache."""
        original = Agent(
            pane_id="%9",
            session_name="work",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/tmp/test",
            raw_command="claude",
            native_name="agent-hq-c3",
            native_name_source="derived",
        )
        restored = Agent.from_dict(original.to_dict())
        assert restored.native_name == "agent-hq-c3"
        assert restored.native_name_source == "derived"

    def test_from_dict_without_native_name_fields(self):
        """An older cache file has no native-name keys; they load as None."""
        d = {
            "pane_id": "%5",
            "session_name": "main",
            "window_index": 0,
            "pane_index": 0,
            "agent_type": "claude",
            "cwd": "/tmp",
            "raw_command": "claude",
            "state": "idle",
        }
        agent = Agent.from_dict(d)
        assert agent.native_name is None
        assert agent.native_name_source is None

    def test_from_dict_rejects_non_string_native_name_fields(self):
        """#287 review: a corrupt cache value never reaches `@{name}` rendering."""
        d = {
            "pane_id": "%5",
            "session_name": "main",
            "window_index": 0,
            "pane_index": 0,
            "agent_type": "claude",
            "cwd": "/tmp",
            "raw_command": "claude",
            "state": "idle",
            "native_name": {"x": 1},
            "native_name_source": 7,
        }
        agent = Agent.from_dict(d)
        assert agent.native_name is None
        assert agent.native_name_source is None

    def test_from_dict_with_optional_none_fields(self):
        d = {
            "pane_id": "%5",
            "session_name": "main",
            "window_index": 0,
            "pane_index": 0,
            "agent_type": "aider",
            "cwd": "/tmp",
            "raw_command": "aider",
            "state": "unknown",
        }
        agent = Agent.from_dict(d)
        assert agent.repo_root is None
        assert agent.repo_name is None
        assert agent.branch is None
        assert agent.worktree_name is None

    def test_agent_roundtrip_with_phase_snapshot(self):
        """phase_snapshot/file_stats/latest_summary round-trip cleanly."""
        snap = _snap(detected=DevPhase.IMPLEMENT, source="tag", overrides_rev=3, hook_rev=7)
        agent = Agent(
            pane_id="%9",
            session_name="main",
            window_index=1,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/repo",
            repo_root="/repo",
            repo_name="repo",
            branch="feature/42-foo",
            worktree_name=None,
            raw_command="claude",
            state=AgentState.RUNNING,
            session_slug="slug",
            phase_snapshot=snap,
            file_stats=FileStats(2, 15, 3),
            latest_summary="Refactored foo.py",
        )
        d = agent.to_dict()
        assert d["phase_snapshot"]["effective_phase"] == "implement"
        assert d["phase_snapshot"]["phase_source"] == "tag"
        assert d["phase_snapshot"]["inputs_observed"] == {
            "overrides_revision": 3,
            "hook_state_revision": 7,
            "transcript_mtime_ns": 0,
            "transcript_size": 0,
        }
        assert d["file_stats"] == {"files": 2, "insertions": 15, "deletions": 3}
        assert d["latest_summary"] == "Refactored foo.py"

        back = Agent.from_dict(d)
        assert back.phase == DevPhase.IMPLEMENT
        assert back.phase_snapshot.phase_source == "tag"
        assert back.phase_snapshot.inputs_observed == InputsObserved(3, 7)
        assert back.file_stats == FileStats(2, 15, 3)
        assert back.latest_summary == "Refactored foo.py"

    def test_phase_snapshot_legacy_merged_normalizes_to_done(self):
        """A snapshot dict whose ``effective_phase`` is the pre-#55 ``"merged"``
        value normalizes via the alias map, just like the prior flat field did."""
        snap_dict = {
            "detected_phase": "merged",
            "effective_phase": "merged",
            "phase_source": "rule",
            "phase_session_id": None,
            "feedback": None,
            "inputs_observed": {"overrides_revision": 0, "hook_state_revision": 0},
            "producer": "daemon",
            "producer_epoch": 0.0,
            "sequence": 0,
            "monotonic_ns": 0,
            "timestamp": 0.0,
        }
        snap = PhaseSnapshot.from_dict(snap_dict)
        assert snap.effective_phase == DevPhase.DONE
        assert snap.detected_phase == DevPhase.DONE

    def test_agent_roundtrip_with_no_snapshot(self):
        """phase_snapshot/file_stats/latest_summary default to None and survive round-trip."""
        agent = Agent(
            pane_id="%1",
            session_name="main",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/repo",
            raw_command="claude",
            state=AgentState.IDLE,
        )
        assert agent.phase is None
        assert agent.phase_snapshot is None
        assert agent.file_stats is None
        assert agent.latest_summary is None

        d = agent.to_dict()
        assert d["phase_snapshot"] is None
        assert d["file_stats"] is None
        assert d["latest_summary"] is None

        back = Agent.from_dict(d)
        assert back.phase is None
        assert back.phase_snapshot is None
        assert back.file_stats is None
        assert back.latest_summary is None

    def test_agent_roundtrip_with_claude_session_id(self):
        agent = Agent(
            pane_id="%5",
            session_name="main",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/repo",
            raw_command="claude",
            state=AgentState.IDLE,
            claude_session_id="session_01Tk4ztYgtWAhHHvE7uh5ghh",
            claude_transcript_session_id="abc-123-transcript",
        )
        d = agent.to_dict()
        assert d["claude_session_id"] == "session_01Tk4ztYgtWAhHHvE7uh5ghh"
        assert d["claude_transcript_session_id"] == "abc-123-transcript"

        back = Agent.from_dict(d)
        assert back.claude_session_id == "session_01Tk4ztYgtWAhHHvE7uh5ghh"
        assert back.claude_transcript_session_id == "abc-123-transcript"

    def test_session_id_fields_default_to_none(self):
        agent = Agent(
            pane_id="%1",
            session_name="main",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/repo",
            raw_command="claude",
            state=AgentState.IDLE,
        )
        assert agent.claude_session_id is None
        assert agent.claude_transcript_session_id is None

        d = agent.to_dict()
        assert d["claude_session_id"] is None
        assert d["claude_transcript_session_id"] is None

        back = Agent.from_dict(d)
        assert back.claude_session_id is None
        assert back.claude_transcript_session_id is None

    def test_stable_key_prefers_transcript_session_id(self):
        """A Claude agent keys on its transcript session id, which survives
        a tmux pane renumber / resume (#132)."""
        agent = Agent(
            pane_id="%38",
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/repo",
            state=AgentState.RUNNING,
            claude_transcript_session_id="abc-123",
        )
        assert agent.stable_key == "abc-123"

    def test_stable_key_falls_back_to_tool_cwd_pane(self):
        """Without a transcript session id, the key falls back to
        ``tool:cwd:pane_id`` — the pane suffix keeps two session-less
        agents in the same cwd from colliding to one card."""
        a = Agent(
            pane_id="%1",
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CODEX,
            cwd="/home/me/scratch",
            state=AgentState.IDLE,
        )
        b = Agent(
            pane_id="%2",
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CODEX,
            cwd="/home/me/scratch",
            state=AgentState.IDLE,
        )
        assert a.stable_key == "codex:/home/me/scratch:%1"
        assert a.stable_key != b.stable_key

    def test_phase_property_is_read_only(self):
        """Agent.phase has no setter — assigning must raise AttributeError.

        The whole point of the snapshot refactor is that ``phase`` reflects
        the resolver's output. In-place mutation would defeat the
        comparison rule in ``replaces``."""
        agent = Agent(
            pane_id="%1",
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/tmp",
            raw_command="claude",
            state=AgentState.IDLE,
        )
        with pytest.raises(AttributeError):
            agent.phase = DevPhase.IMPLEMENT  # type: ignore[misc]


class TestPhaseFeedbackRoundtrip:
    def test_override_roundtrip(self):
        fb = PhaseFeedback(
            action="override",
            phase=DevPhase.QA,
            auto_phase=DevPhase.IMPLEMENT,
            set_at="2026-05-10T12:00:00Z",
            note="forced",
            session_id="sess-A",
        )
        d = fb.to_dict()
        assert d["action"] == "override"
        assert d["phase"] == "qa"
        assert d["auto_phase"] == "implement"
        assert PhaseFeedback.from_dict(d) == fb

    def test_confirm_roundtrip(self):
        fb = PhaseFeedback(
            action="confirm",
            phase=None,
            auto_phase=DevPhase.REVIEW,
            set_at="2026-05-10T12:00:00Z",
            note=None,
            session_id=None,
        )
        d = fb.to_dict()
        assert d["action"] == "confirm"
        assert d["phase"] is None
        assert d["auto_phase"] == "review"
        assert PhaseFeedback.from_dict(d) == fb

    def test_from_dict_rejects_unknown_action(self):
        with pytest.raises(ValueError):
            PhaseFeedback.from_dict({"action": "weird"})

    def test_from_dict_swallows_unknown_phase_string(self):
        """A corrupt phase value on disk shouldn't blow up the cache read."""
        d = {
            "action": "override",
            "phase": "bogus",
            "auto_phase": "alsobogus",
            "set_at": "2026-01-01T00:00:00Z",
            "note": None,
            "session_id": "x",
        }
        fb = PhaseFeedback.from_dict(d)
        assert fb.action == "override"
        assert fb.phase is None
        assert fb.auto_phase is None


class TestPhaseSnapshotRoundtrip:
    def test_minimal_roundtrip(self):
        snap = _snap()
        assert PhaseSnapshot.from_dict(snap.to_dict()) == snap

    def test_full_roundtrip_with_feedback(self):
        snap = _snap(
            detected=DevPhase.IMPLEMENT,
            effective=DevPhase.QA,
            source="tag",
            session_id="trans-1",
            feedback=PhaseFeedback(
                action="override",
                phase=DevPhase.QA,
                auto_phase=DevPhase.IMPLEMENT,
                set_at="2026-05-10T12:00:00Z",
                note=None,
                session_id="sess-A",
            ),
            overrides_rev=4,
            hook_rev=12,
            producer="watcher",
            monotonic_ns=123456789,
        )
        back = PhaseSnapshot.from_dict(snap.to_dict())
        assert back == snap


class TestReplacesRule:
    def test_accepts_when_old_is_none(self):
        new = _snap(overrides_rev=1)
        assert replaces(new, None) is True

    def test_session_id_mismatch_accepts_unconditionally(self):
        """Positive session-id mismatch beats lower revisions: a new
        Claude session reusing the pane number is a real reset, not a
        regression."""
        old = _snap(session_id="A", overrides_rev=100, hook_rev=50)
        new = _snap(session_id="B", overrides_rev=1, hook_rev=1)
        assert replaces(new, old) is True

    def test_one_none_session_does_not_short_circuit(self):
        """Exactly-one-None falls through to revision compare so a transient
        session-file read failure can't accept a stale snapshot."""
        old = _snap(session_id="A", overrides_rev=10, hook_rev=10)
        new = _snap(session_id=None, overrides_rev=5, hook_rev=5)
        assert replaces(new, old) is False

    def test_reject_when_any_input_strictly_older(self):
        old = _snap(overrides_rev=5, hook_rev=10)
        # Newer overrides but older hook → reject (any-input-older).
        new = _snap(overrides_rev=6, hook_rev=9)
        assert replaces(new, old) is False

    def test_accept_when_at_least_one_strictly_newer_and_no_regression(self):
        old = _snap(overrides_rev=5, hook_rev=10)
        new = _snap(overrides_rev=6, hook_rev=10)
        assert replaces(new, old) is True

    def test_tiebreak_via_monotonic_ns_when_inputs_identical(self):
        old = _snap(overrides_rev=5, hook_rev=10, monotonic_ns=100)
        same_or_later = _snap(overrides_rev=5, hook_rev=10, monotonic_ns=100)
        later = _snap(overrides_rev=5, hook_rev=10, monotonic_ns=101)
        earlier = _snap(overrides_rev=5, hook_rev=10, monotonic_ns=99)
        assert replaces(same_or_later, old) is True
        assert replaces(later, old) is True
        assert replaces(earlier, old) is False


class TestReplacesTranscriptAxis:
    """The transcript stat axis orders transcript-only phase changes
    (which touch neither overrides.json nor the hook state file). 0 on
    either side means 'not observed' → non-comparable (an unknown must
    never reject a real update)."""

    def test_accept_when_transcript_strictly_newer(self):
        old = _snap(transcript_mtime_ns=100, transcript_size=10)
        new = _snap(transcript_mtime_ns=200, transcript_size=12)
        assert replaces(new, old) is True

    def test_reject_when_transcript_strictly_older(self):
        old = _snap(transcript_mtime_ns=200, transcript_size=12)
        new = _snap(transcript_mtime_ns=100, transcript_size=10)
        assert replaces(new, old) is False

    def test_size_is_secondary_when_mtime_ties(self):
        old = _snap(transcript_mtime_ns=100, transcript_size=10)
        bigger = _snap(transcript_mtime_ns=100, transcript_size=11)
        smaller = _snap(transcript_mtime_ns=100, transcript_size=9)
        assert replaces(bigger, old) is True
        assert replaces(smaller, old) is False

    def test_zero_is_unknown_never_rejects(self):
        """New snapshot couldn't observe the transcript (sessionId None
        / legacy) → mtime 0. It must not be rejected against an old
        snapshot that did observe one."""
        old = _snap(transcript_mtime_ns=500, transcript_size=50, monotonic_ns=1)
        new = _snap(transcript_mtime_ns=0, transcript_size=0, monotonic_ns=2)
        assert replaces(new, old) is True  # falls to monotonic tiebreak, not reject

    def test_zero_on_old_side_non_comparable(self):
        old = _snap(transcript_mtime_ns=0, transcript_size=0, monotonic_ns=1)
        new = _snap(transcript_mtime_ns=900, transcript_size=80, monotonic_ns=2)
        assert replaces(new, old) is True

    def test_revision_regression_still_rejects_even_if_transcript_newer(self):
        """A newer transcript can't rescue a snapshot that observed an
        older hook revision — revision regression is checked first."""
        old = _snap(hook_rev=10, transcript_mtime_ns=100)
        new = _snap(hook_rev=9, transcript_mtime_ns=200)
        assert replaces(new, old) is False


class TestDevPhase:
    """Tests for DevPhase enum."""

    def test_all_phase_values(self):
        # #108 collapsed brainstorm + plan into a single `design` phase.
        values = {p.value for p in DevPhase}
        assert values == {
            "design",
            "implement",
            "qa",
            "review",
            "ready",
            "done",
            "adhoc",
        }

    def test_brainstorm_and_plan_members_removed(self):
        # Guards the #108 collapse: the old members must not linger.
        assert not hasattr(DevPhase, "BRAINSTORM")
        assert not hasattr(DevPhase, "PLAN")

    def test_phase_emoji_per_phase(self):
        for phase in DevPhase:
            assert isinstance(phase.emoji, str)
            assert len(phase.emoji) >= 1

    def test_phase_label_per_phase(self):
        assert DevPhase.DESIGN.label == "Design"
        assert DevPhase.IMPLEMENT.label == "Implement"
        assert DevPhase.QA.label == "QA"
        assert DevPhase.REVIEW.label == "Review"
        assert DevPhase.READY.label == "Ready"
        assert DevPhase.DONE.label == "Done"
        assert DevPhase.ADHOC.label == "Ad-hoc"

    def test_normalize_phase_value_aliases_merged_to_done(self):
        assert normalize_phase_value("merged") == "done"
        assert normalize_phase_value("implement") == "implement"

    def test_normalize_phase_value_aliases_brainstorm_and_plan_to_design(self):
        # #108: legacy stored snapshots / overrides / transcript tags that
        # wrote "brainstorm" or "plan" must still parse, mapping to design.
        assert normalize_phase_value("brainstorm") == "design"
        assert normalize_phase_value("plan") == "design"
        assert normalize_phase_value("design") == "design"

    def test_phase_from_value_coerces_legacy_aliases_to_design(self):
        assert _phase_from_value("brainstorm") is DevPhase.DESIGN
        assert _phase_from_value("plan") is DevPhase.DESIGN
        assert _phase_from_value("design") is DevPhase.DESIGN


class TestFileStats:
    def test_file_stats_fields(self):
        stats = FileStats(files=3, insertions=42, deletions=8)
        assert stats.files == 3
        assert stats.insertions == 42
        assert stats.deletions == 8

    def test_file_stats_display(self):
        assert FileStats(3, 42, 8).display() == "3 files +42 -8"
        assert FileStats(1, 10, 0).display() == "1 file +10 -0"
        assert FileStats(0, 0, 0).display() == ""

    def test_file_stats_roundtrip(self):
        stats = FileStats(5, 100, 20)
        assert FileStats.from_dict(stats.to_dict()) == stats


class TestSessionMetadata:
    def _agent(self, **kw):
        defaults = dict(
            pane_id="%1",
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/tmp/x",
        )
        defaults.update(kw)
        return Agent(**defaults)

    def test_claude_metadata_roles(self):
        a = self._agent(claude_session_id="bridge-1", claude_transcript_session_id="sess-1")
        md = a.session_metadata
        assert md.harness is AgentType.CLAUDE
        assert md.stable_id == "sess-1"
        assert md.native_uri == "https://claude.ai/code/bridge-1"

    def test_codex_thread_uuid_serves_all_roles(self):
        a = self._agent(agent_type=AgentType.CODEX, codex_thread_id="uuid-7")
        md = a.session_metadata
        assert md.stable_id == "uuid-7"
        assert md.native_uri == "codex://threads/uuid-7"

    def test_unknown_harness_has_no_uri(self):
        a = self._agent(agent_type=AgentType.AIDER)
        assert a.session_metadata.stable_id is None
        assert a.session_metadata.native_uri is None

    def test_stable_key_prefers_codex_thread_id(self):
        a = self._agent(agent_type=AgentType.CODEX, codex_thread_id="uuid-7")
        assert a.stable_key == "uuid-7"

    def test_stable_key_fallback_and_legacy_key(self):
        a = self._agent(agent_type=AgentType.CODEX)
        assert a.stable_key == "codex:/tmp/x:%1"
        assert a.legacy_stable_key == "codex:/tmp/x:%1"
        # legacy key is ALWAYS the fallback form, even when a stable id exists
        b = self._agent(claude_transcript_session_id="sess-1")
        assert b.stable_key == "sess-1"
        assert b.legacy_stable_key == "claude:/tmp/x:%1"

    def test_override_session_id_per_harness(self):
        assert self._agent(claude_session_id="bridge-1").override_session_id == "bridge-1"
        assert (
            self._agent(agent_type=AgentType.CODEX, codex_thread_id="uuid-7").override_session_id
            == "uuid-7"
        )

    def test_is_pinned_in_accepts_legacy_key(self):
        # Once codex_thread_id is captured, stable_key upgrades from the
        # pre-upgrade "codex:cwd:pane" form to the thread UUID (#205 Task
        # 1-3). A pin written before the upgrade must still match (Task 7).
        a = self._agent(
            pane_id="%3", cwd="/w", agent_type=AgentType.CODEX, codex_thread_id="uuid-7"
        )
        assert a.is_pinned_in({"uuid-7"})
        assert a.is_pinned_in({"codex:/w:%3"})  # pre-upgrade pin survives
        assert not a.is_pinned_in({"other"})

    def test_is_pinned_in_matches_when_stable_and_legacy_key_are_equal(self):
        # No upgrade captured yet: stable_key == legacy_stable_key.
        a = self._agent(pane_id="%4", cwd="/w", agent_type=AgentType.CODEX)
        assert a.stable_key == a.legacy_stable_key
        assert a.is_pinned_in({a.stable_key})


class TestIdentitySerializationCompat:
    def test_to_dict_emits_old_and_new(self):
        a = Agent(
            pane_id="%1",
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CODEX,
            cwd="/x",
            codex_thread_id="uuid-7",
        )
        d = a.to_dict()
        assert d["agent_type"] == "codex" and d["harness"] == "codex"
        assert d["stable_id"] == "uuid-7" and d["codex_thread_id"] == "uuid-7"
        assert d["claude_session_id"] is None  # old names still present

    def test_from_dict_old_only_record(self):
        d = _minimal_dict(agent_type="claude", claude_transcript_session_id="sess-1")
        assert Agent.from_dict(d).claude_transcript_session_id == "sess-1"

    def test_from_dict_new_wins_on_disagreement(self):
        d = _minimal_dict(
            agent_type="claude",
            claude_transcript_session_id="old-sess",
            stable_id="new-sess",
        )
        assert Agent.from_dict(d).claude_transcript_session_id == "new-sess"

    def test_from_dict_new_only_codex(self):
        d = _minimal_dict(agent_type="codex", stable_id="uuid-7")
        a = Agent.from_dict(d)
        assert a.codex_thread_id == "uuid-7" and a.stable_key == "uuid-7"

    def test_from_dict_unknown_harness_value_falls_back(self):
        d = _minimal_dict(agent_type="claude", harness="not-a-tool")
        assert Agent.from_dict(d).agent_type is AgentType.CLAUDE


class TestCodexRolloutPath:
    """``Agent.codex_rollout_path`` (#206): SessionMetadata.transcript_path
    for codex + serialization round-trip."""

    def _codex(self, **kw):
        defaults = dict(
            pane_id="%1",
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CODEX,
            cwd="/x",
        )
        defaults.update(kw)
        return Agent(**defaults)

    def test_session_metadata_transcript_path_codex(self):
        a = self._codex(codex_thread_id="uuid-7", codex_rollout_path="/r/rollout.jsonl")
        assert a.session_metadata.transcript_path == "/r/rollout.jsonl"

    def test_session_metadata_transcript_path_none_when_uncaptured(self):
        assert self._codex().session_metadata.transcript_path is None

    def test_claude_transcript_path_stays_none(self):
        a = Agent(
            pane_id="%1",
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/x",
            claude_transcript_session_id="sess-1",
        )
        assert a.session_metadata.transcript_path is None

    def test_serialization_roundtrip(self):
        a = self._codex(codex_thread_id="uuid-7", codex_rollout_path="/r/rollout.jsonl")
        d = a.to_dict()
        assert d["codex_rollout_path"] == "/r/rollout.jsonl"
        assert Agent.from_dict(d).codex_rollout_path == "/r/rollout.jsonl"

    def test_from_dict_old_record_defaults_none(self):
        d = _minimal_dict(agent_type="codex", stable_id="uuid-7")
        assert Agent.from_dict(d).codex_rollout_path is None
