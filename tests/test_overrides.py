"""Tests for the phase-override store and feedback log.

Schema v2 of ``overrides.json`` is ``{"revision": int, "entries": {...}}``.
v1 files (no envelope, root is the entries dict) read as
``(legacy_entries, 0)`` and migrate to v2 on first write. The resolver
applies phase-overrides; this module only stores them and prunes the
ones whose session_id no longer matches the live agent.
"""

import json
import re
import threading
from unittest.mock import patch

import pytest

from agent_hq.models import Agent, AgentState, AgentType
from agent_hq.overrides import (
    OVERRIDES_LOCK,
    append_feedback,
    delete_override,
    make_feedback_event,
    prune_stale_overrides,
    read_overrides,
    write_override,
)


def _agent(
    pane_id: str = "%5",
    claude_session_id: str | None = "sess-A",
) -> Agent:
    return Agent(
        pane_id=pane_id,
        session_name="main",
        window_index=1,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd="/home/user/project",
        repo_root="/home/user/project",
        repo_name="my-project",
        branch="main",
        raw_command="claude",
        state=AgentState.RUNNING,
        claude_session_id=claude_session_id,
    )


@pytest.fixture
def overrides_path(tmp_path):
    """Patch the overrides file location and return the path.

    The flock sidecar follows the patched path automatically — the
    overrides module computes its lock path from ``OVERRIDES_FILE``
    so tests get isolation for free.
    """
    path = tmp_path / "overrides.json"
    with patch("agent_hq.overrides.OVERRIDES_FILE", path):
        yield path


@pytest.fixture
def feedback_path(tmp_path):
    """Patch the feedback log location and return the path."""
    path = tmp_path / "phase-feedback.jsonl"
    with patch("agent_hq.overrides.FEEDBACK_FILE", path):
        yield path


class TestReadOverrides:
    def test_returns_empty_when_missing(self, overrides_path):
        entries, rev = read_overrides()
        assert entries == {}
        assert rev == 0

    def test_returns_empty_on_corrupt_json(self, overrides_path):
        overrides_path.write_text("not json{{{")
        entries, rev = read_overrides()
        assert entries == {}
        assert rev == 0

    def test_returns_empty_on_non_object_root(self, overrides_path):
        overrides_path.write_text("[]")
        entries, rev = read_overrides()
        assert entries == {}
        assert rev == 0

    def test_returns_parsed_v2_entries_with_revision(self, overrides_path):
        overrides_path.parent.mkdir(parents=True, exist_ok=True)
        overrides_path.write_text(
            json.dumps(
                {
                    "revision": 17,
                    "entries": {"%5": {"action": "override", "phase": "plan"}},
                }
            )
        )
        entries, rev = read_overrides()
        assert entries == {"%5": {"action": "override", "phase": "plan"}}
        assert rev == 17

    def test_reads_legacy_v1_as_revision_zero(self, overrides_path):
        """Pre-v5 files have no envelope — root is the entries dict.

        First write after this read produces the v2 envelope; until
        then ``revision`` reads as 0 so the resolver's revision
        comparison treats all v1 inputs as the oldest possible."""
        overrides_path.parent.mkdir(parents=True, exist_ok=True)
        overrides_path.write_text(json.dumps({"%5": {"action": "override", "phase": "plan"}}))
        entries, rev = read_overrides()
        assert entries == {"%5": {"action": "override", "phase": "plan"}}
        assert rev == 0

    def test_logs_error_on_unreadable_file(self, overrides_path, caplog):
        """A read error other than FileNotFoundError (permissions, EIO)
        is logged at ERROR rather than silently swallowed — it means the
        file is there but unreadable, which loses the user's overrides
        and lets the next write clobber the on-disk file."""
        overrides_path.write_text(json.dumps({"revision": 3, "entries": {}}))
        with (
            patch("pathlib.Path.read_text", side_effect=PermissionError("denied")),
            caplog.at_level("ERROR", logger="agent_hq.overrides"),
        ):
            entries, rev = read_overrides()
        assert entries == {}
        assert rev == 0
        assert any(
            r.levelname == "ERROR" and "overrides.json" in r.getMessage() for r in caplog.records
        )


class TestWriteOverride:
    def test_writes_new_entry_in_v2_envelope(self, overrides_path):
        write_override(
            "%5",
            {"action": "override", "phase": "plan", "set_at": 1, "note": None},
            session_id="sess-A",
        )
        on_disk = json.loads(overrides_path.read_text())
        assert on_disk["revision"] == 1
        assert on_disk["entries"]["%5"]["action"] == "override"
        assert on_disk["entries"]["%5"]["phase"] == "plan"

    def test_persists_session_id(self, overrides_path):
        write_override(
            "%5",
            {"action": "override", "phase": "plan", "set_at": 1, "note": None},
            session_id="sess-A",
        )
        on_disk = json.loads(overrides_path.read_text())
        assert on_disk["entries"]["%5"]["session_id"] == "sess-A"

    def test_persists_none_session_id_for_non_claude_agents(self, overrides_path):
        write_override(
            "%5",
            {"action": "override", "phase": "plan", "set_at": 1, "note": None},
            session_id=None,
        )
        on_disk = json.loads(overrides_path.read_text())
        assert on_disk["entries"]["%5"]["session_id"] is None

    def test_atomic_via_tmp_rename(self, overrides_path):
        write_override(
            "%5",
            {"action": "confirm", "phase": None, "set_at": 1, "note": None},
            session_id="sess-A",
        )
        assert not overrides_path.with_suffix(".json.tmp").exists()
        assert overrides_path.exists()

    def test_persist_cleans_up_tmp_on_rename_failure(self, overrides_path):
        """If the rename step fails (disk full / read-only fs), the temp
        file must not be left behind and the error must propagate —
        mirrors ``hooks._atomic_write``."""
        with patch("pathlib.Path.rename", side_effect=OSError("no space")):
            with pytest.raises(OSError):
                write_override(
                    "%5",
                    {"action": "override", "phase": "plan", "set_at": 1, "note": None},
                    session_id="sess-A",
                )
        assert not overrides_path.with_suffix(".json.tmp").exists()
        assert not overrides_path.exists()

    def test_increments_revision_on_each_write(self, overrides_path):
        for i in range(5):
            write_override(
                f"%{i}",
                {"action": "confirm", "phase": None, "set_at": i, "note": None},
                session_id="sess-A",
            )
        on_disk = json.loads(overrides_path.read_text())
        assert on_disk["revision"] == 5

    def test_legacy_v1_file_first_write_bumps_to_v2_envelope(self, overrides_path):
        """A pre-v5 file with no envelope. First write migrates to v2.

        Pre-v5 revision reads as 0, so the next write produces revision=1
        in the new envelope — the migration is invisible to the resolver."""
        overrides_path.parent.mkdir(parents=True, exist_ok=True)
        overrides_path.write_text(json.dumps({"%5": {"action": "override", "phase": "plan"}}))
        write_override(
            "%8",
            {"action": "confirm", "phase": None, "set_at": 1, "note": None},
            session_id="sess-A",
        )
        on_disk = json.loads(overrides_path.read_text())
        assert on_disk["revision"] == 1
        assert "%5" in on_disk["entries"]
        assert "%8" in on_disk["entries"]

    def test_merges_with_existing_entries(self, overrides_path):
        write_override(
            "%5",
            {"action": "confirm", "phase": None, "set_at": 1, "note": None},
            session_id="sess-A",
        )
        write_override(
            "%8",
            {"action": "override", "phase": "plan", "set_at": 2, "note": None},
            session_id="sess-B",
        )
        entries, _ = read_overrides()
        assert set(entries.keys()) == {"%5", "%8"}

    def test_overwrites_same_pane_id(self, overrides_path):
        write_override(
            "%5",
            {"action": "confirm", "phase": None, "set_at": 1, "note": None},
            session_id="sess-A",
        )
        write_override(
            "%5",
            {"action": "override", "phase": "qa", "set_at": 2, "note": "later"},
            session_id="sess-A",
        )
        entries, _ = read_overrides()
        assert entries["%5"]["action"] == "override"
        assert entries["%5"]["phase"] == "qa"
        assert entries["%5"]["note"] == "later"

    def test_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "missing-dir" / "overrides.json"
        with patch("agent_hq.overrides.OVERRIDES_FILE", nested):
            write_override(
                "%5",
                {"action": "confirm", "phase": None, "set_at": 1, "note": None},
                session_id="sess-A",
            )
        assert nested.exists()


class TestDeleteOverride:
    def test_removes_entry_and_bumps_revision(self, overrides_path):
        write_override(
            "%5",
            {"action": "confirm", "phase": None, "set_at": 1, "note": None},
            session_id="sess-A",
        )
        write_override(
            "%8",
            {"action": "confirm", "phase": None, "set_at": 2, "note": None},
            session_id="sess-A",
        )
        _, rev_before = read_overrides()
        assert delete_override("%5") is True
        entries, rev_after = read_overrides()
        assert set(entries.keys()) == {"%8"}
        assert rev_after == rev_before + 1

    def test_returns_false_when_missing(self, overrides_path):
        assert delete_override("%5") is False

    def test_no_op_when_file_missing(self, overrides_path):
        assert delete_override("%5") is False
        assert not overrides_path.exists()


class TestAppendFeedback:
    def test_writes_one_jsonl_line_with_iso_ts(self, feedback_path):
        event = make_feedback_event(
            pane_id="%5",
            repo="agent-hq",
            branch="main",
            auto_phase="adhoc",
            user_phase="plan",
            action="override",
            note="testing",
        )
        append_feedback(event)
        lines = feedback_path.read_text().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["pane_id"] == "%5"
        assert parsed["action"] == "override"
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", parsed["ts"])

    def test_appends_to_existing_file(self, feedback_path):
        event = make_feedback_event(
            pane_id="%5",
            repo="r",
            branch="b",
            auto_phase=None,
            user_phase=None,
            action="confirm",
            note=None,
        )
        append_feedback(event)
        append_feedback(event)
        append_feedback(event)
        assert len(feedback_path.read_text().splitlines()) == 3

    def test_serializes_concurrent_writers(self, feedback_path):
        N = 30

        def writer(i: int) -> None:
            event = make_feedback_event(
                pane_id=f"%{i}",
                repo="r",
                branch="b",
                auto_phase="adhoc",
                user_phase=None,
                action="confirm",
                note=None,
            )
            append_feedback(event)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = feedback_path.read_text().splitlines()
        assert len(lines) == N
        pane_ids = {json.loads(line)["pane_id"] for line in lines}
        assert pane_ids == {f"%{i}" for i in range(N)}

    def test_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "no-such-dir" / "phase-feedback.jsonl"
        with patch("agent_hq.overrides.FEEDBACK_FILE", nested):
            append_feedback(
                make_feedback_event(
                    pane_id="%5",
                    repo="r",
                    branch="b",
                    auto_phase="adhoc",
                    user_phase=None,
                    action="confirm",
                    note=None,
                )
            )
        assert nested.exists()


class TestPruneStaleOverrides:
    """``prune_stale_overrides`` is the daemon-side garbage collector.

    Resolver applies phase. Prune only deletes entries that fail the
    5-state matrix: positive session mismatch, and pre-upgrade entries
    with no ``session_id`` key. Everything else stays on disk.
    """

    def test_drops_mismatched_session_id_and_bumps_revision(self, overrides_path):
        write_override(
            "%5",
            {"action": "override", "phase": "done", "set_at": 1, "note": None},
            session_id="old-session",
        )
        agents = [_agent("%5", claude_session_id="new-session")]
        _, rev_before = read_overrides()
        prune_stale_overrides(agents)
        entries, rev_after = read_overrides()
        assert entries == {}
        assert rev_after == rev_before + 1

    def test_keeps_matching_session_id(self, overrides_path):
        write_override(
            "%5",
            {"action": "override", "phase": "qa", "set_at": 1, "note": None},
            session_id="sess-A",
        )
        agents = [_agent("%5", claude_session_id="sess-A")]
        prune_stale_overrides(agents)
        entries, _ = read_overrides()
        assert "%5" in entries

    def test_drops_mismatched_codex_thread_id(self, overrides_path):
        """#205 Task 6: prune must compare against override_session_id, not
        the claude-only bridge id, so a codex thread-id mismatch is
        actually reachable (a stale codex override otherwise never prunes
        since claude_session_id is always None for codex agents)."""
        write_override(
            "%5",
            {"action": "override", "phase": "done", "set_at": 1, "note": None},
            session_id="uuid-7",
        )
        agents = [
            Agent(
                pane_id="%5",
                session_name="main",
                window_index=1,
                pane_index=0,
                agent_type=AgentType.CODEX,
                cwd="/home/user/project",
                repo_root="/home/user/project",
                repo_name="my-project",
                branch="main",
                raw_command="codex",
                state=AgentState.RUNNING,
                codex_thread_id="uuid-8",
            )
        ]
        _, rev_before = read_overrides()
        prune_stale_overrides(agents)
        entries, rev_after = read_overrides()
        assert entries == {}
        assert rev_after == rev_before + 1

    def test_keeps_matching_codex_thread_id(self, overrides_path):
        write_override(
            "%5",
            {"action": "override", "phase": "qa", "set_at": 1, "note": None},
            session_id="uuid-7",
        )
        agents = [
            Agent(
                pane_id="%5",
                session_name="main",
                window_index=1,
                pane_index=0,
                agent_type=AgentType.CODEX,
                cwd="/home/user/project",
                repo_root="/home/user/project",
                repo_name="my-project",
                branch="main",
                raw_command="codex",
                state=AgentState.RUNNING,
                codex_thread_id="uuid-7",
            )
        ]
        prune_stale_overrides(agents)
        entries, _ = read_overrides()
        assert "%5" in entries

    @staticmethod
    def _codex_agent(pane_id: str = "%5", thread_id: str | None = None) -> Agent:
        return Agent(
            pane_id=pane_id,
            session_name="main",
            window_index=1,
            pane_index=0,
            agent_type=AgentType.CODEX,
            cwd="/home/user/project",
            repo_root="/home/user/project",
            repo_name="my-project",
            branch="main",
            raw_command="codex",
            state=AgentState.RUNNING,
            codex_thread_id=thread_id,
        )

    def test_stamps_precapture_codex_entry_with_captured_uuid(self, overrides_path):
        """#205 Codex review write-back: an override written while the codex
        thread UUID was uncaptured stores ``session_id=None``, which the
        lenient matrix matches against EVERY future codex session in the
        pane. Prune must bind it to the first captured UUID it sees so it
        can't outlive the session."""
        write_override(
            "%5",
            {"action": "override", "phase": "qa", "set_at": 1, "note": None},
            session_id=None,
        )
        _, rev_before = read_overrides()
        prune_stale_overrides([self._codex_agent(thread_id="uuid-7")])
        entries, rev_after = read_overrides()
        assert entries["%5"]["session_id"] == "uuid-7"
        assert rev_after == rev_before + 1

    def test_stamped_entry_prunes_on_session_rollover(self, overrides_path):
        """The point of the write-back: once bound, a later session's
        different UUID is a genuine mismatch and the stale override goes."""
        write_override(
            "%5",
            {"action": "override", "phase": "qa", "set_at": 1, "note": None},
            session_id=None,
        )
        prune_stale_overrides([self._codex_agent(thread_id="uuid-7")])
        prune_stale_overrides([self._codex_agent(thread_id="uuid-other")])
        entries, _ = read_overrides()
        assert entries == {}

    def test_no_stamp_when_agent_uuid_also_missing(self, overrides_path):
        """None-vs-None is an ordinary match — nothing to bind, no write."""
        write_override(
            "%5",
            {"action": "override", "phase": "qa", "set_at": 1, "note": None},
            session_id=None,
        )
        _, rev_before = read_overrides()
        prune_stale_overrides([self._codex_agent(thread_id=None)])
        entries, rev_after = read_overrides()
        assert entries["%5"]["session_id"] is None
        assert rev_after == rev_before

    def test_claude_one_sided_none_is_not_stamped(self, overrides_path):
        """Claude is not lenient: a stored ``None`` vs a live bridge id reads
        ``ambiguous`` (transient read failure), which is kept but must NOT
        be bound — the next successful read resolves it."""
        write_override(
            "%5",
            {"action": "override", "phase": "qa", "set_at": 1, "note": None},
            session_id=None,
        )
        _, rev_before = read_overrides()
        prune_stale_overrides([_agent("%5", claude_session_id="sess-B")])
        entries, rev_after = read_overrides()
        assert entries["%5"]["session_id"] is None
        assert rev_after == rev_before

    def test_drops_legacy_entry_missing_session_id_key(self, overrides_path):
        """Pre-#65 entries (no session_id key) are migration debris — drop them."""
        overrides_path.parent.mkdir(parents=True, exist_ok=True)
        # v1 layout (no envelope), entry missing session_id.
        overrides_path.write_text(
            json.dumps(
                {
                    "%5": {
                        "action": "override",
                        "phase": "done",
                        "set_at": 1,
                        "note": None,
                    }
                }
            )
        )
        agents = [_agent("%5", claude_session_id="sess-A")]
        prune_stale_overrides(agents)
        entries, _ = read_overrides()
        assert entries == {}

    def test_keeps_orphan_entries(self, overrides_path):
        """No live agent for the pane → keep (out of scope for prune)."""
        write_override(
            "%99",
            {"action": "override", "phase": "qa", "set_at": 1, "note": None},
            session_id="sess-A",
        )
        agents = [_agent("%5", claude_session_id="sess-A")]
        prune_stale_overrides(agents)
        entries, _ = read_overrides()
        assert "%99" in entries

    def test_keeps_entry_when_agent_session_id_is_transient_none(self, overrides_path):
        """Exactly-one-None (agent's bridge-id read failed) is NOT a
        positive mismatch — leave the entry alone."""
        write_override(
            "%5",
            {"action": "override", "phase": "qa", "set_at": 1, "note": None},
            session_id="sess-A",
        )
        agents = [_agent("%5", claude_session_id=None)]
        prune_stale_overrides(agents)
        entries, _ = read_overrides()
        assert "%5" in entries
        assert entries["%5"]["session_id"] == "sess-A"

    def test_keeps_entry_when_both_session_ids_none(self, overrides_path):
        """Non-Claude agents (None vs None) match — keep."""
        write_override(
            "%5",
            {"action": "override", "phase": "plan", "set_at": 1, "note": None},
            session_id=None,
        )
        agents = [_agent("%5", claude_session_id=None)]
        prune_stale_overrides(agents)
        entries, _ = read_overrides()
        assert "%5" in entries

    def test_no_write_when_nothing_stale(self, overrides_path):
        """Healthy file → no rewrite. Saves a write per daemon tick."""
        import time

        write_override(
            "%5",
            {"action": "override", "phase": "qa", "set_at": 1, "note": None},
            session_id="sess-A",
        )
        agents = [_agent("%5", claude_session_id="sess-A")]
        before = overrides_path.stat().st_mtime_ns
        time.sleep(0.01)
        prune_stale_overrides(agents)
        after = overrides_path.stat().st_mtime_ns
        assert before == after


class TestWriteOverrideReadFailure:
    """AHQ-014: a read failure (not 'file missing') must never let the
    write proceed with an empty base — that clobbers existing entries."""

    def test_unreadable_file_raises_and_does_not_clobber(self, overrides_path):
        write_override(
            "%5",
            {"action": "confirm", "phase": None, "set_at": 1, "note": None},
            session_id="sess-A",
        )
        before = overrides_path.read_text()
        with patch("pathlib.Path.read_text", side_effect=PermissionError("denied")):
            with pytest.raises(OSError):
                write_override(
                    "%9",
                    {"action": "confirm", "phase": None, "set_at": 2, "note": None},
                    session_id="sess-C",
                )
        assert overrides_path.read_text() == before

    def test_corrupt_json_raises_and_does_not_clobber_on_delete(self, overrides_path):
        write_override(
            "%5",
            {"action": "confirm", "phase": None, "set_at": 1, "note": None},
            session_id="sess-A",
        )
        overrides_path.write_text("not json{{{")
        with pytest.raises(OSError):
            delete_override("%5")
        assert overrides_path.read_text() == "not json{{{"

    def test_corrupt_json_raises_and_does_not_clobber_on_prune(self, overrides_path):
        overrides_path.parent.mkdir(parents=True, exist_ok=True)
        overrides_path.write_text("not json{{{")
        agents = [_agent("%5", claude_session_id="sess-A")]
        with pytest.raises(OSError):
            prune_stale_overrides(agents)
        assert overrides_path.read_text() == "not json{{{"

    def test_missing_file_still_writes_normally(self, overrides_path):
        # Sanity: a genuinely missing file is NOT a read failure — this
        # must not raise.
        write_override(
            "%5",
            {"action": "confirm", "phase": None, "set_at": 1, "note": None},
            session_id="sess-A",
        )
        entries, _ = read_overrides()
        assert "%5" in entries


class TestOverridesLockSerialization:
    """End-to-end serialization check on real concurrent writers.

    Spawn N threads, each writes an entry. Final revision must equal N
    (every write must have bumped the revision under the lock; no
    clobbering, no skipped writes)."""

    def test_concurrent_writes_serialize_via_flock(self, overrides_path):
        N = 25

        def writer(i: int) -> None:
            write_override(
                f"%{i}",
                {"action": "confirm", "phase": None, "set_at": i, "note": None},
                session_id=f"sess-{i}",
            )

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        entries, rev = read_overrides()
        assert rev == N
        assert set(entries.keys()) == {f"%{i}" for i in range(N)}


def test_overrides_lock_path_constant_matches_default_file():
    """Production constant points at the canonical ``.json.lock`` sidecar."""
    assert str(OVERRIDES_LOCK).endswith("overrides.json.lock")
