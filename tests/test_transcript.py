"""Tests for ``agent_hq.transcript`` — the pure JSONL primitives the
resolver uses to derive phase (moved off the hook in #73).

``extract_phase_tag`` text-parsing is covered in ``test_phase_tag.py``;
here we cover the transcript-file reverse reader and path construction.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from agent_hq.transcript import (
    extract_last_assistant_text,
    extract_last_phase_tag,
    transcript_path_for,
)


def _jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records))


def _assistant(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


class TestExtractLastPhaseTag:
    def test_last_assistant_tag_wins(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _jsonl(t, [_assistant("<phase>qa</phase>"), _assistant("<phase>design</phase>")])
        assert extract_last_phase_tag(str(t)) == "design"

    def test_skips_trailing_system_and_user_records(self, tmp_path):
        """#73 Test A shape: qa then design (both assistant), then a
        trailing ``system`` ``done`` notice. The last *assistant* tag
        (design) wins — system/user records are ignored."""
        t = tmp_path / "s.jsonl"
        _jsonl(
            t,
            [
                _assistant("Testing.\n\n<phase>qa</phase>"),
                {"type": "user", "message": {"content": "emit design"}},
                _assistant("Okay.\n\n<phase>design</phase>"),
                {"type": "system", "content": "<phase>done</phase>"},
            ],
        )
        assert extract_last_phase_tag(str(t)) == "design"

    def test_no_tag_anywhere_returns_none(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _jsonl(t, [_assistant("just talking"), _assistant("still no tag")])
        assert extract_last_phase_tag(str(t)) is None

    def test_missing_file_returns_none(self):
        assert extract_last_phase_tag("/no/such/file.jsonl") is None

    def test_legacy_merged_normalizes_to_done(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _jsonl(t, [_assistant("Wrapped up.\n\n<phase>merged</phase>")])
        assert extract_last_phase_tag(str(t)) == "done"


class TestExtractLastAssistantText:
    def test_returns_last_assistant_text(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _jsonl(t, [_assistant("first"), _assistant("last one")])
        assert extract_last_assistant_text(str(t)) == "last one"

    def test_none_when_no_assistant_records(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _jsonl(t, [{"type": "user", "message": {"content": "hi"}}])
        assert extract_last_assistant_text(str(t)) is None


class TestTranscriptPathFor:
    def test_encodes_cwd_slashes_to_dashes(self):
        with patch("agent_hq.transcript.CLAUDE_PROJECTS_DIR", Path("/PROJ")):
            p = transcript_path_for("/Users/x/repo wt", "sid-123")
        assert p == ["/PROJ/-Users-x-repo wt/sid-123.jsonl"]

    def test_encodes_dots_to_dashes(self):
        """Claude Code replaces ``/``, ``.``, and ``_`` with ``-``. Missing
        the dot substitution silently breaks every ``repo.feature-N``
        worktree and anything under ``~/.claude-worktrees`` (the ``/.``
        becomes a ``--`` double-dash)."""
        with patch("agent_hq.transcript.CLAUDE_PROJECTS_DIR", Path("/PROJ")):
            p = transcript_path_for("/Users/x/.claude-worktrees/repo.feature-9-thing", "sid")
        assert p[0] == "/PROJ/-Users-x--claude-worktrees-repo-feature-9-thing/sid.jsonl"

    def test_encodes_underscores_to_dashes(self):
        """AHQ-003 (#180): current Claude Code (~mid-2026) also replaces
        ``_`` when building the project dir name. The primary candidate
        must reflect that or every ``<phase>`` tag for an underscore cwd
        is silently ignored forever (resolver's stat 404s, no error)."""
        with patch("agent_hq.transcript.CLAUDE_PROJECTS_DIR", Path("/PROJ")):
            p = transcript_path_for("/Users/x/agent_hq", "sid")
        assert p[0] == "/PROJ/-Users-x-agent-hq/sid.jsonl"

    def test_underscore_cwd_yields_legacy_fallback_candidate(self):
        """Directories created by a pre-change Claude Code version keep
        their original (underscore-preserving) name on disk forever, so
        an underscore cwd must also yield the old-scheme path as a
        second candidate, after the current-scheme one."""
        with patch("agent_hq.transcript.CLAUDE_PROJECTS_DIR", Path("/PROJ")):
            p = transcript_path_for("/Users/x/agent_hq", "sid")
        assert p == [
            "/PROJ/-Users-x-agent-hq/sid.jsonl",
            "/PROJ/-Users-x-agent_hq/sid.jsonl",
        ]

    def test_no_underscore_yields_single_candidate(self):
        """No ambiguity without an underscore in cwd — one candidate, not
        a spurious duplicate."""
        with patch("agent_hq.transcript.CLAUDE_PROJECTS_DIR", Path("/PROJ")):
            p = transcript_path_for("/Users/x/repo", "sid")
        assert p == ["/PROJ/-Users-x-repo/sid.jsonl"]

    def test_empty_list_when_session_id_missing(self):
        assert transcript_path_for("/Users/x/repo", None) == []
        assert transcript_path_for("/Users/x/repo", "") == []

    def test_pure_no_disk_touch(self, tmp_path):
        """Returns candidate paths even when nothing exists on disk — the
        resolver's ``os.stat`` loop is the one existence check."""
        with patch("agent_hq.transcript.CLAUDE_PROJECTS_DIR", tmp_path):
            p = transcript_path_for("/a/b", "sid")
        assert p == [str(tmp_path / "-a-b" / "sid.jsonl")]
