"""Tests for extract_phase_tag — parses Claude's inline <phase> tags."""

from __future__ import annotations

import pytest

from agent_hq.transcript import extract_phase_tag


class TestExtractPhaseTag:
    @pytest.mark.parametrize(
        "phase",
        ["adhoc", "design", "implement", "qa", "review", "ready", "done"],
    )
    def test_returns_value_for_each_known_phase(self, phase: str):
        text = f"Did the thing.\n\n<phase>{phase}</phase>"
        assert extract_phase_tag(text) == phase

    def test_legacy_brainstorm_and_plan_tags_normalize_to_design(self):
        # #108: sessions that predate the rename still emit brainstorm/plan;
        # the parser maps them to the current `design` value via the alias.
        assert extract_phase_tag("Done.\n\n<phase>brainstorm</phase>") == "design"
        assert extract_phase_tag("Done.\n\n<phase>plan</phase>") == "design"

    def test_returns_none_when_tag_missing(self):
        assert extract_phase_tag("Just normal text, no tag.") is None

    def test_returns_none_for_unknown_phase_value(self):
        # Defends against typos and Claude inventing new phases.
        assert extract_phase_tag("...\n<phase>refactor</phase>") is None

    def test_case_insensitive_phase_value(self):
        assert extract_phase_tag("<phase>QA</phase>") == "qa"
        assert extract_phase_tag("<phase>Design</phase>") == "design"

    def test_returns_last_tag_when_multiple_present(self):
        # If Claude mentions <phase>plan</phase> mid-message but ends with
        # <phase>qa</phase>, the last one is the source of truth.
        text = "Discussed <phase>plan</phase> earlier.\n\n<phase>qa</phase>"
        assert extract_phase_tag(text) == "qa"

    def test_tolerates_internal_whitespace(self):
        assert extract_phase_tag("<phase>  qa  </phase>") == "qa"
        assert extract_phase_tag("<phase>\nqa\n</phase>") == "qa"

    def test_returns_none_on_empty_string(self):
        assert extract_phase_tag("") is None

    def test_returns_none_when_tag_malformed(self):
        # Self-closing or missing closing tag — don't try to be clever.
        assert extract_phase_tag("<phase>qa") is None
        assert extract_phase_tag("<phase/>") is None
        assert extract_phase_tag("</phase>") is None

    def test_ignores_mid_message_tag_when_no_final_tag(self):
        """The instruction says the tag MUST be on the very last line.
        A valid tag mid-body, with non-tag text after, is NOT authoritative."""
        text = "I considered <phase>plan</phase> but went with implementation."
        assert extract_phase_tag(text) is None

    def test_ignores_quoted_tags_followed_by_real_text(self):
        text = (
            "I'd emit `<phase>review</phase>` here, but I'm still implementing.\n\nMore work to do."
        )
        assert extract_phase_tag(text) is None

    def test_accepts_line_leading_tag_with_trailing_parenthetical(self):
        """Recap-skill pattern: tag at start of a line, followed by a hint
        like ``(disable recaps in /config)``. The tag is intended to be
        authoritative even though there's text after the closing tag on
        the same line — what matters is that the tag *starts* the line."""
        text = "Some recap text.\n\n<phase>design</phase> (disable recaps in /config)"
        assert extract_phase_tag(text) == "design"

    def test_accepts_line_leading_tag_with_following_lines(self):
        """Recap-skill pattern with structured layout: tag, optional trailing
        note, blank line, then more sections (task lists, status, etc.).
        Common in agentic skills that emit a status block mid-message."""
        text = (
            "## Recap\n\n"
            "<phase>design</phase> (disable recaps in /config)\n\n"
            "6 tasks (1 done, 5 open)\n"
            "- Item 1\n"
            "- Item 2\n"
        )
        assert extract_phase_tag(text) == "design"

    def test_accepts_indented_line_leading_tag(self):
        """Tag preceded only by whitespace on its line still counts."""
        text = "Some prose.\n\n    <phase>design</phase>"
        assert extract_phase_tag(text) == "design"

    def test_picks_last_line_leading_tag_when_multiple_in_message(self):
        """``Discussed <phase>plan</phase> earlier`` is mid-prose and ignored;
        the line-leading ``<phase>qa</phase>`` further down wins."""
        text = (
            "Discussed <phase>plan</phase> earlier.\n\n"
            "<phase>qa</phase> (note)\n\n"
            "More content here."
        )
        assert extract_phase_tag(text) == "qa"

    def test_accepts_tag_with_trailing_whitespace_only(self):
        # PHASE_INSTRUCTION says the tag is the last line; a trailing newline
        # is fine.
        assert extract_phase_tag("Body text.\n\n<phase>qa</phase>\n") == "qa"
        assert extract_phase_tag("Body text.\n\n<phase>qa</phase>  \n  ") == "qa"

    def test_normalizes_legacy_merged_to_done(self):
        """Pre-rename transcripts can still contain ``<phase>merged</phase>``.
        Reject-as-typo would lose lookback recovery from those transcripts —
        normalize through the same alias map used for state files."""
        assert extract_phase_tag("Wrapped up.\n\n<phase>merged</phase>") == "done"
        # Case-insensitive normalization too — Claude sometimes capitalizes.
        assert extract_phase_tag("<phase>Merged</phase>") == "done"
