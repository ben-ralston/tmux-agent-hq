"""Pure Claude Code transcript primitives.

These helpers used to live in ``hooks.py``, where the hook extracted the
phase tag at hook-fire time — racing the very transcript flush it depended
on (#73 Test A). Phase derivation now happens in the resolver, which reads
the transcript on every poll long after the flush settles. This module
holds the shared, side-effect-free pieces both paths need:

* ``hooks.handle_hook_event`` still calls ``extract_last_assistant_text``
  for the display-only ``latest_summary``.
* ``phase.detect_transcript_phase`` (→ ``resolver``) calls
  ``extract_last_phase_tag`` for the authoritative phase signal.

Everything here is a pure function of (text) or (path). No clock, no
disk writes, no hook protocol. ``transcript_path_for`` is pure path
construction — it does not stat the filesystem; the resolver stats
each returned candidate in order so existence + mtime/size are
observed together for whichever one exists.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import DevPhase, normalize_phase_value

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Match ``<phase>X</phase>`` at the start of a line — start-of-text or a
# newline boundary, optional leading whitespace, then the tag. Trailing
# content on the same line (e.g., ``(disable recaps in /config)``) and
# additional message content on later lines do NOT disqualify the tag —
# what matters is that the tag *begins* its line, distinguishing recap-
# style emits from tags Claude mentions mid-prose.
_PHASE_TAG_LINE_RE = re.compile(
    r"(?:^|\n)[ \t]*<phase>\s*([a-zA-Z]+)\s*</phase>",
    re.IGNORECASE,
)
_VALID_PHASE_VALUES = {p.value for p in DevPhase}

_TRANSCRIPT_REVERSE_CHUNK = 64 * 1024


def extract_phase_tag(text: str) -> str | None:
    """Return the most recent line-leading ``<phase>X</phase>`` value in ``text``.

    A tag counts only if it begins its own line. Tags mid-prose are
    ignored. When multiple line-leading tags appear, the LAST one wins
    (lets Claude correct itself within a single message).
    """
    if not text:
        return None
    candidate: str | None = None
    for m in _PHASE_TAG_LINE_RE.finditer(text):
        candidate = m.group(1).strip().lower()
    if candidate is None:
        return None
    candidate = normalize_phase_value(candidate)
    return candidate if candidate in _VALID_PHASE_VALUES else None


def _iter_lines_reverse_with_offsets(
    path: str,
    end_pos: int | None = None,
    chunk_size: int = _TRANSCRIPT_REVERSE_CHUNK,
):
    """Yield ``(byte_offset, decoded_line)`` pairs from ``path``, newest first.

    ``end_pos`` bounds the read to bytes strictly before that offset
    (``None`` = end of file), which is what makes byte offsets usable as
    pagination cursors: transcripts are append-only, so the offset of a
    line never changes once written. Memory stays bounded to ~one chunk
    plus the longest line, exactly like the original reverse reader.
    """
    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        pos = file_size if end_pos is None else max(0, min(end_pos, file_size))
        leftover = b""
        while pos > 0:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size) + leftover
            lines = chunk.split(b"\n")
            if pos > 0:
                leftover = lines[0]
                rest = lines[1:]
                start = pos + len(lines[0]) + 1
            else:
                leftover = b""
                rest = lines
                start = 0
            offsets = []
            offset = start
            for raw in rest:
                offsets.append(offset)
                offset += len(raw) + 1
            for raw, line_offset in zip(reversed(rest), reversed(offsets)):
                if raw.strip():
                    yield line_offset, raw.decode("utf-8", errors="replace")


def _iter_lines_reverse(path: str, chunk_size: int = _TRANSCRIPT_REVERSE_CHUNK):
    """Yield decoded lines from ``path`` starting at the end of file."""
    for _offset, line in _iter_lines_reverse_with_offsets(path, chunk_size=chunk_size):
        yield line


def _assistant_text_from_record(record: dict) -> str:
    """Extract text from a Claude Code transcript ``assistant`` record."""
    msg = record.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        texts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return " ".join(t for t in texts if t).strip()
    if isinstance(content, str):
        return content.strip()
    return ""


def extract_last_assistant_text(transcript_path: str) -> str | None:
    """Return the last assistant text turn from a Claude Code JSONL transcript."""
    try:
        for line in _iter_lines_reverse(transcript_path):
            try:
                record = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if record.get("type") != "assistant":
                continue
            text = _assistant_text_from_record(record)
            if text:
                return text
        return None
    except OSError:
        return None


def extract_last_phase_tag(transcript_path: str) -> str | None:
    """Return the phase value from the most recent assistant message with a tag.

    Reverse-reads the JSONL and considers only ``type == "assistant"``
    records — ``system``/``user`` records (e.g. a trailing ``system``
    ``<phase>done</phase>`` notice) are skipped, so the value reflects
    what Claude itself last emitted. The reverse read early-exits on the
    first tagged assistant record, so the common case touches only the
    tail of the file.
    """
    try:
        for line in _iter_lines_reverse(transcript_path):
            try:
                record = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if record.get("type") != "assistant":
                continue
            text = _assistant_text_from_record(record)
            if not text:
                continue
            tag = extract_phase_tag(text)
            if tag is not None:
                return tag
        return None
    except OSError:
        return None


def transcript_path_for(cwd: str, transcript_session_id: str | None) -> list[str]:
    """Build candidate JSONL transcript paths for a Claude session. Pure.

    Claude Code stores transcripts at
    ``~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`` where the cwd
    is encoded by replacing ``/``, ``.``, and (as of ~mid-2026) ``_``
    with ``-`` (so ``/Users/x/.claude-worktrees/repo.feature-9`` →
    ``-Users-x--claude-worktrees-repo-feature-9``, and
    ``/Users/x/agent_hq`` → ``-Users-x-agent-hq``). Missing the ``.``
    substitution silently breaks every dotted worktree path; missing the
    ``_`` substitution silently breaks every underscore cwd — in both
    cases the transcript path never exists, every ``<phase>`` tag is
    ignored, and phase falls back to the branch rule forever with no
    error (AHQ-003, #180).

    A directory Claude Code created before it started replacing ``_``
    keeps its original (underscore-preserving) name on disk forever, so
    for an underscore cwd this returns TWO candidates in priority order:
    the current-scheme (``_`` replaced) path first, then the legacy
    (``_`` preserved) path. Callers should stat each in order and use
    the first that exists.

    Returns an empty list when the transcript sessionId is unknown
    (older Claude versions, or not yet discovered) — the resolver then
    falls back to branch-rule phase detection, exactly as it did before
    phase derivation moved here. Does NOT touch disk: the caller's
    ``os.stat``/``exists()`` is the observation of existence + mtime +
    size.
    """
    if not transcript_session_id:
        return []
    current_encoded = cwd.replace("/", "-").replace(".", "-").replace("_", "-")
    candidates = [str(CLAUDE_PROJECTS_DIR / current_encoded / f"{transcript_session_id}.jsonl")]
    if "_" in cwd:
        legacy_encoded = cwd.replace("/", "-").replace(".", "-")
        candidates.append(
            str(CLAUDE_PROJECTS_DIR / legacy_encoded / f"{transcript_session_id}.jsonl")
        )
    return candidates
