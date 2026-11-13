"""Tests for SentMessageStore — the durable Idea-Inbox sent-message log. #140"""

from __future__ import annotations

import json
from pathlib import Path

from agent_hq.idea.sent_store import SentMessageStore


def _store(tmp_path: Path, **kwargs) -> SentMessageStore:
    return SentMessageStore(tmp_path / "inbox-sent.jsonl", **kwargs)


class TestRecordPending:
    def test_writes_full_self_contained_jsonl_line(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record_pending("ship the thing", sent_id="abc")

        lines = (tmp_path / "inbox-sent.jsonl").read_text().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["id"] == "abc"
        assert obj["text"] == "ship the thing"
        assert obj["status"] == "pending"
        assert obj["created_at"]  # ISO timestamp present

    def test_listed_across_instances(self, tmp_path: Path) -> None:
        _store(tmp_path).record_pending("a", sent_id="1")
        records = _store(tmp_path).list()
        assert [r.id for r in records] == ["1"]
        assert records[0].status == "pending"
        assert records[0].text == "a"


class TestRecordOutcome:
    def test_filed_outcome_folds_latest_wins(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record_pending("idea text", sent_id="x")
        store.record_outcome(
            "x",
            status="filed",
            destination="github",
            issue_url="https://github.com/o/r/issues/9",
            issue_number=9,
            repo="o/r",
            project="P",
            title="Drafted title",
            warnings=["w1"],
            confidence=0.42,
            needs_triage=True,
        )

        records = store.list()
        assert len(records) == 1
        rec = records[0]
        assert rec.status == "filed"
        assert rec.issue_url == "https://github.com/o/r/issues/9"
        assert rec.warnings == ["w1"]
        assert rec.confidence == 0.42
        assert rec.needs_triage is True

    def test_outcome_line_is_self_contained(self, tmp_path: Path) -> None:
        """Outcome lines re-carry text/created_at so cap-truncation of the
        pending line can never orphan a record's content."""
        store = _store(tmp_path)
        store.record_pending("original text", sent_id="x")
        created = store.list()[0].created_at
        store.record_outcome("x", status="error", error="boom")

        last_line = json.loads((tmp_path / "inbox-sent.jsonl").read_text().splitlines()[-1])
        assert last_line["text"] == "original text"
        assert last_line["created_at"] == created
        assert last_line["error"] == "boom"

    def test_error_outcome(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record_pending("t", sent_id="x")
        store.record_outcome("x", status="error", error="github: rate limited")
        rec = store.list()[0]
        assert rec.status == "error"
        assert rec.error == "github: rate limited"

    def test_unknown_id_is_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record_pending("t", sent_id="x")
        assert store.record_outcome("nope", status="filed") is False
        assert [r.id for r in store.list()] == ["x"]

    def test_retry_with_same_id_supersedes(self, tmp_path: Path) -> None:
        """A client retry reuses the idempotency key as the sent id — its new
        lines supersede the failed attempt instead of duplicating it."""
        store = _store(tmp_path)
        store.record_pending("t", sent_id="x")
        store.record_outcome("x", status="error", error="boom")
        store.record_pending("t", sent_id="x")
        store.record_outcome("x", status="filed", destination="github")

        records = store.list()
        assert len(records) == 1
        assert records[0].status == "filed"


class TestDismiss:
    def test_dismiss_excludes_from_list(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record_pending("t", sent_id="x")
        store.record_outcome("x", status="error", error="boom")
        assert store.dismiss("x") is True
        assert store.list() == []

    def test_dismiss_unknown_id_returns_false(self, tmp_path: Path) -> None:
        assert _store(tmp_path).dismiss("nope") is False

    def test_dismissed_is_terminal(self, tmp_path: Path) -> None:
        """A zombie thread writing `filed` after the user dismissed an
        interrupted row must not resurrect it."""
        store = _store(tmp_path)
        store.record_pending("t", sent_id="x")
        store.dismiss("x")
        store.record_outcome("x", status="filed", destination="github")
        assert store.list() == []


class TestReadTolerance:
    def test_missing_file_lists_empty(self, tmp_path: Path) -> None:
        assert _store(tmp_path).list() == []

    def test_corrupt_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "inbox-sent.jsonl"
        good = json.dumps(
            {"id": "x", "text": "t", "created_at": "2026-06-10T00:00:00+00:00", "status": "pending"}
        )
        path.write_text("not json\n[1,2]\n" + good + "\n")
        records = SentMessageStore(path).list()
        assert [r.id for r in records] == ["x"]


class TestCap:
    def test_caps_to_newest_max_records_lines(self, tmp_path: Path) -> None:
        store = _store(tmp_path, max_records=4)
        for i in range(6):
            store.record_pending(f"t{i}", sent_id=f"id{i}")

        lines = (tmp_path / "inbox-sent.jsonl").read_text().splitlines()
        assert len(lines) == 4
        ids = [json.loads(line)["id"] for line in lines]
        assert ids == ["id2", "id3", "id4", "id5"]

    def test_list_order_is_oldest_first(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record_pending("a", sent_id="1")
        store.record_pending("b", sent_id="2")
        store.record_outcome("1", status="error", error="x")
        assert [r.id for r in store.list()] == ["1", "2"]


class TestNeverRaises:
    def test_mutations_swallow_io_errors(self, tmp_path: Path) -> None:
        """A logging failure must never break filing — mirror SnapshotStore."""
        blocked = tmp_path / "dir-not-file"
        blocked.mkdir()
        store = SentMessageStore(blocked)  # path IS a directory → writes fail
        assert store.record_pending("t", sent_id="x") is None
        assert store.record_outcome("x", status="filed") is False
        assert store.dismiss("x") is False
        assert store.list() == []


class TestDefaultPath:
    def test_default_path_constant(self) -> None:
        from agent_hq.idea import sent_store

        # conftest's autouse isolation fixture redirects this into tmp_path;
        # the unredirected default must live under ~/.config/agent-hq/.
        store = SentMessageStore()
        assert store._path == sent_store._DEFAULT_SENT_FILE


class TestDismissedTerminalHardening:
    def test_record_outcome_noops_on_dismissed_id(self, tmp_path: Path) -> None:
        """A zombie thread's outcome write after dismissal must not append —
        otherwise the line cap could eventually drop the dismiss tombstone
        and resurrect the row. (Codex review #140)"""
        store = _store(tmp_path)
        store.record_pending("t", sent_id="x")
        store.dismiss("x")
        assert store.record_outcome("x", status="error", error="zombie") is False
        lines = (tmp_path / "inbox-sent.jsonl").read_text().splitlines()
        assert json.loads(lines[-1])["status"] == "dismissed"  # nothing appended after


class TestMalformedFieldTolerance:
    def test_bad_field_types_are_skipped_not_raised(self, tmp_path: Path) -> None:
        """Valid JSON with junk field types must honor the tolerant-read
        contract: skip the line, never raise from list()/dismiss()."""
        path = tmp_path / "inbox-sent.jsonl"
        bad_int = json.dumps({"id": "a", "text": "t", "created_at": "x",
                              "status": "pending", "issue_number": "not-an-int"})
        bad_warnings = json.dumps({"id": "b", "text": "t", "created_at": "x",
                                   "status": "pending", "warnings": 7})
        good = json.dumps({"id": "c", "text": "ok", "created_at": "x",
                           "status": "pending"})
        path.write_text(bad_int + "\n" + bad_warnings + "\n" + good + "\n")
        store = SentMessageStore(path)
        assert [r.id for r in store.list()] == ["c"]
        assert store.dismiss("a") is False  # malformed record is invisible
