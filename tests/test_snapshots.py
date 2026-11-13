"""Tests for the durable agent-snapshot store + recovery record. #125"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from agent_hq.models import (
    Agent,
    AgentState,
    AgentType,
    DevPhase,
    InputsObserved,
    PhaseSnapshot,
)
from agent_hq.snapshots import (
    MAX_RECORDS,
    SnapshotStore,
    agent_to_record,
    issue_numbers_from_branch,
)


def _snap(effective: DevPhase) -> PhaseSnapshot:
    return PhaseSnapshot(
        detected_phase=effective,
        effective_phase=effective,
        phase_source="tag",
        phase_session_id=None,
        feedback=None,
        inputs_observed=InputsObserved(0, 0),
        producer="daemon",
        producer_epoch=0.0,
        sequence=0,
        monotonic_ns=0,
        timestamp=0.0,
    )


def _agent(
    *,
    pane_id="%5",
    session_name="agent-hq-125",
    agent_type=AgentType.CLAUDE,
    cwd="/Users/me/wt/agent-hq.feature-125-snapshot-recovery",
    repo_name="agent-hq",
    branch="feature/125-snapshot-recovery",
    state=AgentState.RUNNING,
    transcript_id="sess-abc",
    phase: DevPhase | None = DevPhase.IMPLEMENT,
) -> Agent:
    return Agent(
        pane_id=pane_id,
        session_name=session_name,
        window_index=1,
        pane_index=0,
        agent_type=agent_type,
        cwd=cwd,
        repo_root=cwd,
        repo_name=repo_name,
        branch=branch,
        worktree_name=None,
        raw_command="claude",
        state=state,
        phase_snapshot=_snap(phase) if phase is not None else None,
        claude_transcript_session_id=transcript_id,
    )


class TestIssueNumbersFromBranch:
    def test_single_feature_issue(self):
        assert issue_numbers_from_branch("feature/125-snapshot-recovery") == [125]

    def test_multi_issue_branch(self):
        assert issue_numbers_from_branch("feature/12-34-combined-thing") == [12, 34]

    def test_bugfix_prefix(self):
        assert issue_numbers_from_branch("bugfix/7-fix-crash") == [7]

    def test_chore_and_docs_prefixes(self):
        assert issue_numbers_from_branch("chore/9-tidy") == [9]
        assert issue_numbers_from_branch("docs/3-readme") == [3]

    def test_plain_branch_has_no_issue(self):
        assert issue_numbers_from_branch("main") == []

    def test_none_branch(self):
        assert issue_numbers_from_branch(None) == []


class TestAgentToRecord:
    def test_claude_agent_maps_all_fields(self):
        rec = agent_to_record(_agent())
        assert rec == {
            "session_id": "sess-abc",
            "stable_id": "sess-abc",
            "tool": "claude",
            "cwd": "/Users/me/wt/agent-hq.feature-125-snapshot-recovery",
            "repo": "agent-hq",
            "branch": "feature/125-snapshot-recovery",
            "issue": 125,
            "tmux_session": "agent-hq-125",
            "pane": "%5",
            "state": "running",
            "phase": "implement",
        }

    def test_no_phase_serializes_null(self):
        rec = agent_to_record(_agent(phase=None))
        assert rec["phase"] is None

    def test_non_claude_agent_without_session(self):
        rec = agent_to_record(_agent(agent_type=AgentType.CODEX, transcript_id=None, branch="main"))
        assert rec["session_id"] is None
        assert rec["tool"] == "codex"
        assert rec["issue"] is None

    def test_record_codex_thread_id_fills_session_id(self):
        agent = _agent(agent_type=AgentType.CODEX, transcript_id=None)
        agent.codex_thread_id = "uuid-7"
        rec = agent_to_record(agent)
        assert rec["session_id"] == "uuid-7" and rec["stable_id"] == "uuid-7"

    def test_record_claude_unchanged_and_stable_id_added(self):
        rec = agent_to_record(_agent(transcript_id="sess-abc"))
        assert rec["session_id"] == "sess-abc" and rec["stable_id"] == "sess-abc"


class TestSnapshotStoreAppend:
    def test_first_append_writes_record(self, tmp_path: Path):
        store = SnapshotStore(tmp_path / "snaps.jsonl")
        assert store.append_if_changed([_agent()], timestamp=100.0) is True
        last = store.last()
        assert last["timestamp"] == 100.0
        assert last["agents"][0]["session_id"] == "sess-abc"

    def test_identical_agents_is_noop(self, tmp_path: Path):
        path = tmp_path / "snaps.jsonl"
        store = SnapshotStore(path)
        store.append_if_changed([_agent()], timestamp=100.0)
        assert store.append_if_changed([_agent()], timestamp=200.0) is False
        assert len(store.records()) == 1

    def test_changed_state_appends(self, tmp_path: Path):
        store = SnapshotStore(tmp_path / "snaps.jsonl")
        store.append_if_changed([_agent(state=AgentState.RUNNING)], timestamp=100.0)
        assert store.append_if_changed([_agent(state=AgentState.IDLE)], timestamp=200.0) is True
        assert len(store.records()) == 2

    def test_changed_roster_appends(self, tmp_path: Path):
        store = SnapshotStore(tmp_path / "snaps.jsonl")
        store.append_if_changed([_agent(pane_id="%1")], timestamp=100.0)
        appended = store.append_if_changed(
            [_agent(pane_id="%1"), _agent(pane_id="%2", transcript_id="sess-2")],
            timestamp=200.0,
        )
        assert appended is True
        assert len(store.records()[-1]["agents"]) == 2

    def test_dedup_is_order_insensitive(self, tmp_path: Path):
        a = _agent(pane_id="%1", transcript_id="s1")
        b = _agent(pane_id="%2", transcript_id="s2")
        store = SnapshotStore(tmp_path / "snaps.jsonl")
        store.append_if_changed([a, b], timestamp=100.0)
        # Same set, reversed order — must NOT be treated as a change.
        assert store.append_if_changed([b, a], timestamp=200.0) is False
        assert len(store.records()) == 1

    def test_empty_roster_is_not_recorded(self, tmp_path: Path):
        # A roster of 0 agents carries no recovery value and, if recorded as
        # the last line, would make `recover` say "nothing was live" after a
        # reboot cold-start. Snapshots represent live rosters only. #125
        store = SnapshotStore(tmp_path / "snaps.jsonl")
        assert store.append_if_changed([], timestamp=100.0) is False
        assert store.records() == []

    def test_empty_roster_does_not_clobber_last(self, tmp_path: Path):
        store = SnapshotStore(tmp_path / "snaps.jsonl")
        store.append_if_changed([_agent()], timestamp=100.0)
        assert store.append_if_changed([], timestamp=200.0) is False
        assert len(store.records()) == 1
        assert store.records()[0]["agents"][0]["session_id"] == "sess-abc"


class TestSnapshotStoreReads:
    def test_last_missing_file_is_none(self, tmp_path: Path):
        assert SnapshotStore(tmp_path / "nope.jsonl").last() is None

    def test_records_missing_file_is_empty(self, tmp_path: Path):
        assert SnapshotStore(tmp_path / "nope.jsonl").records() == []

    def test_records_tolerates_corrupt_lines(self, tmp_path: Path):
        path = tmp_path / "snaps.jsonl"
        path.write_text(
            json.dumps({"timestamp": 1.0, "agents": []})
            + "\nnot json\n"
            + json.dumps({"timestamp": 2.0, "agents": []})
            + "\n"
        )
        recs = SnapshotStore(path).records()
        assert [r["timestamp"] for r in recs] == [1.0, 2.0]

    def test_last_returns_most_recent(self, tmp_path: Path):
        store = SnapshotStore(tmp_path / "snaps.jsonl")
        store.append_if_changed([_agent(state=AgentState.RUNNING)], timestamp=1.0)
        store.append_if_changed([_agent(state=AgentState.IDLE)], timestamp=2.0)
        assert store.last()["timestamp"] == 2.0


class TestLastNonEmpty:
    def test_skips_trailing_empty_records(self, tmp_path: Path):
        # The daemon no longer writes empties, but a legacy/edited file (or a
        # crash mid-write) may have one — recover must look past it. #125
        path = tmp_path / "snaps.jsonl"
        path.write_text(
            json.dumps({"timestamp": 1.0, "agents": [{"pane": "%5", "session_id": "s1"}]})
            + "\n"
            + json.dumps({"timestamp": 2.0, "agents": []})
            + "\n"
        )
        assert SnapshotStore(path).last_nonempty()["timestamp"] == 1.0

    def test_none_when_all_empty(self, tmp_path: Path):
        path = tmp_path / "snaps.jsonl"
        path.write_text(json.dumps({"timestamp": 1.0, "agents": []}) + "\n")
        assert SnapshotStore(path).last_nonempty() is None

    def test_none_when_missing(self, tmp_path: Path):
        assert SnapshotStore(tmp_path / "nope.jsonl").last_nonempty() is None


class TestRotation:
    def test_keeps_last_n_records(self, tmp_path: Path):
        store = SnapshotStore(tmp_path / "snaps.jsonl", max_records=3)
        for i in range(5):
            # Distinct state each time forces a real append.
            st = AgentState.RUNNING if i % 2 == 0 else AgentState.IDLE
            store.append_if_changed([_agent(state=st, transcript_id=f"s{i}")], timestamp=float(i))
        recs = store.records()
        assert len(recs) == 3
        assert [r["timestamp"] for r in recs] == [2.0, 3.0, 4.0]


class TestReadFailureDoesNotClobberHistory:
    """AHQ-014: a whole-file read failure must skip the append, not
    truncate the crash-recovery history to a single new record."""

    def test_unreadable_file_skips_append_preserves_history(self, tmp_path: Path) -> None:
        path = tmp_path / "snaps.jsonl"
        store = SnapshotStore(path)
        store.append_if_changed([_agent(pane_id="%1")], timestamp=1.0)
        store.append_if_changed([_agent(pane_id="%2", transcript_id="s2")], timestamp=2.0)
        assert len(store.records()) == 2

        with patch("pathlib.Path.read_text", side_effect=OSError("EIO")):
            appended = store.append_if_changed(
                [_agent(pane_id="%3", transcript_id="s3")], timestamp=3.0
            )

        assert appended is False
        assert len(store.records()) == 2


class TestResilience:
    def test_default_path_constant_lives_in_config_dir(self):
        from agent_hq.snapshots import _DEFAULT_SNAPSHOTS_FILE

        assert _DEFAULT_SNAPSHOTS_FILE.name == "agent-snapshots.jsonl"
        assert _DEFAULT_SNAPSHOTS_FILE.parent.name == "agent-hq"

    def test_default_max_records_is_positive(self):
        assert MAX_RECORDS > 0

    def test_unwritable_path_swallows_error(self, tmp_path: Path):
        # Parent is a FILE, so mkdir/open will fail — must not raise.
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        store = SnapshotStore(blocker / "nested" / "snaps.jsonl")
        assert store.append_if_changed([_agent()], timestamp=1.0) is False

    def test_append_swallows_non_oserror(self, tmp_path: Path, monkeypatch):
        # append runs in the daemon hot loop and must NEVER raise — not even on
        # a non-OSError (e.g. a serialization failure). #125
        store = SnapshotStore(tmp_path / "snaps.jsonl")

        def _boom(_records):
            raise ValueError("boom")

        monkeypatch.setattr(store, "_atomic_write", _boom)
        assert store.append_if_changed([_agent()], timestamp=1.0) is False

    def test_append_swallows_record_build_error(self, tmp_path: Path, monkeypatch):
        # Record-building (agent_to_record + sort) runs in the hot loop too, so
        # it must be inside the never-raise guard. #125
        store = SnapshotStore(tmp_path / "snaps.jsonl")

        def _boom(_agents):
            raise RuntimeError("boom")

        monkeypatch.setattr("agent_hq.snapshots._record_agents", _boom)
        assert store.append_if_changed([_agent()], timestamp=1.0) is False
