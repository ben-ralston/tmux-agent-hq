"""Codex rollout reader tests pinned against the Stage 0 fixtures (#206)."""

import json
from pathlib import Path

from agent_hq.codex_transcript import (
    extract_last_codex_assistant_text,
    extract_last_codex_phase_tag,
    read_rollout_session_meta,
)

FIXTURES = Path(__file__).parent / "fixtures" / "codex_rollouts"

PLAIN_ID = "00000000-0000-7000-8000-000000000001"
RESUME_ID = "00000000-0000-7000-8000-000000000002"
COMPACT_ID = "00000000-0000-7000-8000-000000000003"
PARENT_ID = "00000000-0000-7000-8000-000000000004"
CHILD_ID = "00000000-0000-7000-8000-000000000005"


class TestSessionMeta:
    def test_plain_meta(self):
        meta = read_rollout_session_meta(str(FIXTURES / "plain.jsonl"))
        assert meta.thread_id == PLAIN_ID
        assert meta.cwd == "/fixture/worktree"
        assert meta.originator == "codex-tui"
        assert meta.is_subagent is False

    def test_child_first_line_is_child_identity(self):
        # The child rollout carries TWO session_meta records (child identity
        # first, inherited parent second) — the reader must bind to the first.
        meta = read_rollout_session_meta(str(FIXTURES / "subagent-child.jsonl"))
        assert meta.thread_id == CHILD_ID
        assert meta.is_subagent is True

    def test_parent_meta_not_subagent(self):
        meta = read_rollout_session_meta(str(FIXTURES / "subagent-parent.jsonl"))
        assert meta.thread_id == PARENT_ID
        assert meta.is_subagent is False

    def test_missing_file(self):
        assert read_rollout_session_meta(str(FIXTURES / "nope.jsonl")) is None

    def test_non_meta_first_line(self, tmp_path):
        p = tmp_path / "r.jsonl"
        p.write_text('{"type":"turn_context","payload":{}}\n')
        assert read_rollout_session_meta(str(p)) is None

    def test_malformed_first_line(self, tmp_path):
        p = tmp_path / "r.jsonl"
        p.write_text("{not json\n")
        assert read_rollout_session_meta(str(p)) is None


class TestPhaseTag:
    def test_plain(self):
        assert extract_last_codex_phase_tag(str(FIXTURES / "plain.jsonl"), PLAIN_ID) == "implement"

    def test_resume_last_tag_wins(self):
        assert extract_last_codex_phase_tag(str(FIXTURES / "resume.jsonl"), RESUME_ID) == "qa"

    def test_compaction_tolerated(self):
        assert extract_last_codex_phase_tag(str(FIXTURES / "compaction.jsonl"), COMPACT_ID) == "qa"

    def test_parent_reads_own_rollout(self):
        assert (
            extract_last_codex_phase_tag(str(FIXTURES / "subagent-parent.jsonl"), PARENT_ID) == "qa"
        )

    def test_child_rollout_rejected_for_parent_id(self):
        # Child rollout's FIRST session_meta is the child identity; the
        # inherited-parent meta on line 2 must never bind the parent id.
        assert (
            extract_last_codex_phase_tag(str(FIXTURES / "subagent-child.jsonl"), PARENT_ID) is None
        )

    def test_child_rollout_rejected_even_for_child_id(self):
        # is_subagent rollouts never yield phase — a subagent's phase must
        # never attach to any pane.
        assert (
            extract_last_codex_phase_tag(str(FIXTURES / "subagent-child.jsonl"), CHILD_ID) is None
        )

    def test_wrong_expected_id(self):
        assert extract_last_codex_phase_tag(str(FIXTURES / "plain.jsonl"), RESUME_ID) is None

    def test_none_expected_id(self):
        assert extract_last_codex_phase_tag(str(FIXTURES / "plain.jsonl"), None) is None

    def test_invalid_tag_value_yields_none(self, tmp_path):
        content = (FIXTURES / "plain.jsonl").read_text()
        p = tmp_path / "r.jsonl"
        p.write_text(content.replace("<phase>implement</phase>", "<phase>implementing</phase>"))
        assert extract_last_codex_phase_tag(str(p), PLAIN_ID) is None

    def test_malformed_and_unknown_lines_skipped(self, tmp_path):
        lines = (FIXTURES / "plain.jsonl").read_text().splitlines()
        lines.append("{not json")
        lines.append(json.dumps({"type": "mystery_future_record", "payload": 7}))
        p = tmp_path / "r.jsonl"
        p.write_text("\n".join(lines) + "\n")
        assert extract_last_codex_phase_tag(str(p), PLAIN_ID) == "implement"

    def test_event_msg_duplicate_not_used(self, tmp_path):
        # Patch the event_msg duplicate to carry a DIFFERENT tag than the
        # response_item; the response_item must win (event records are
        # duplicated views, never sources).
        lines = (FIXTURES / "plain.jsonl").read_text().splitlines()
        patched = [
            ln.replace("<phase>implement</phase>", "<phase>done</phase>")
            if '"type":"event_msg"' in ln
            else ln
            for ln in lines
        ]
        p = tmp_path / "r.jsonl"
        p.write_text("\n".join(patched) + "\n")
        assert extract_last_codex_phase_tag(str(p), PLAIN_ID) == "implement"

    def test_missing_file(self):
        assert extract_last_codex_phase_tag(str(FIXTURES / "nope.jsonl"), PLAIN_ID) is None


class TestAssistantText:
    def test_plain_summary(self):
        text = extract_last_codex_assistant_text(str(FIXTURES / "plain.jsonl"), PLAIN_ID)
        assert text == "<phase>implement</phase>\nFixture response."

    def test_resume_latest(self):
        text = extract_last_codex_assistant_text(str(FIXTURES / "resume.jsonl"), RESUME_ID)
        assert "After resume." in text

    def test_wrong_id(self):
        assert extract_last_codex_assistant_text(str(FIXTURES / "plain.jsonl"), RESUME_ID) is None


class TestBoundedScan:
    """Workflow review: a rollout with no <phase> tag must not be
    reverse-scanned in full on every poll — the scan is byte-bounded so a
    multi-MB rollout can't blow the 2s daemon tick."""

    def _big_rollout(self, tmp_path, *, tag_at_end: bool):
        p = tmp_path / "big.jsonl"
        meta = {
            "type": "session_meta",
            "payload": {"id": PLAIN_ID, "cwd": "/w", "originator": "codex-tui"},
        }
        lines = [json.dumps(meta)]
        if tag_at_end:
            lines.append(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "<phase>qa</phase>"}],
                        },
                    }
                )
            )
        filler = json.dumps({"type": "response_item", "payload": {"type": "reasoning"}})
        lines.extend([filler] * 20000)
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_untagged_rollout_scan_is_bounded(self, tmp_path):
        from agent_hq.codex_transcript import CODEX_REVERSE_SCAN_BUDGET_BYTES

        p = self._big_rollout(tmp_path, tag_at_end=False)
        assert p.stat().st_size > CODEX_REVERSE_SCAN_BUDGET_BYTES
        assert extract_last_codex_phase_tag(str(p), PLAIN_ID) is None

    def test_tag_near_tail_still_found(self, tmp_path):
        # Tag sits right after the meta line (i.e. far from the tail) —
        # bounded scan gives up rather than reading the whole file.
        p = self._big_rollout(tmp_path, tag_at_end=True)
        assert extract_last_codex_phase_tag(str(p), PLAIN_ID) is None

    def test_recent_tag_found_within_budget(self, tmp_path):
        p = tmp_path / "small.jsonl"
        meta = {
            "type": "session_meta",
            "payload": {"id": PLAIN_ID, "cwd": "/w", "originator": "codex-tui"},
        }
        msg = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "<phase>qa</phase>"}],
            },
        }
        p.write_text(json.dumps(meta) + "\n" + json.dumps(msg) + "\n")
        assert extract_last_codex_phase_tag(str(p), PLAIN_ID) == "qa"


class TestByteLevelScanBudget:
    """Final codex review P2: the budget must bind at the raw-byte layer.
    A single enormous JSONL record (multi-MB tool output on one line) must
    not be read end-to-end just to discover it blows the budget."""

    def test_single_line_larger_than_budget_yields_nothing(self, tmp_path):
        from agent_hq.codex_transcript import _iter_bounded_reverse

        p = tmp_path / "giant.jsonl"
        p.write_text("x" * 10_000 + "\n")
        assert list(_iter_bounded_reverse(str(p), budget=1_000)) == []

    def test_tail_lines_within_budget_are_yielded_newest_first(self, tmp_path):
        from agent_hq.codex_transcript import _iter_bounded_reverse

        p = tmp_path / "r.jsonl"
        p.write_text("old\n" + "mid\n" + "new\n")
        assert list(_iter_bounded_reverse(str(p), budget=1_000)) == ["new", "mid", "old"]

    def test_partial_line_at_budget_floor_is_dropped(self, tmp_path):
        from agent_hq.codex_transcript import _iter_bounded_reverse

        p = tmp_path / "r.jsonl"
        # Layout: AAAA...A\nshort\n  — budget covers "short" plus a slice
        # of the long line; the slice must be dropped, not yielded as if
        # it were a complete line.
        p.write_text("A" * 500 + "\n" + "short\n")
        got = list(_iter_bounded_reverse(str(p), budget=100))
        assert got == ["short"]

    def test_giant_tail_line_does_not_hide_but_bounds_earlier_records(self, tmp_path):
        # End-to-end: a giant untagged line at EOF exhausts the budget, so
        # the reader degrades to no-phase (branch rule covers it) instead
        # of scanning the whole file.
        import json as _json

        from agent_hq.codex_transcript import extract_last_codex_phase_tag

        p = tmp_path / "r.jsonl"
        meta = {
            "type": "session_meta",
            "payload": {"id": PLAIN_ID, "cwd": "/w", "originator": "codex-tui"},
        }
        tagged = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "<phase>qa</phase>"}],
            },
        }
        giant = {"type": "response_item", "payload": {"type": "reasoning", "blob": "z" * 2_000_000}}
        p.write_text(
            _json.dumps(meta) + "\n" + _json.dumps(tagged) + "\n" + _json.dumps(giant) + "\n"
        )
        # 512 KiB budget < 2 MB tail line: the tag before it is out of reach.
        assert extract_last_codex_phase_tag(str(p), PLAIN_ID) is None
