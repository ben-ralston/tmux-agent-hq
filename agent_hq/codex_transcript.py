"""Pure Codex rollout JSONL primitives (#206 Stage 2).

The Codex analog of ``transcript.py``: side-effect-free readers the
resolver and enrichment use behind ``resolve_phase_snapshot()``'s
pure-read boundary. The rollout format is documented unstable, so every
function tolerates unknown record types, malformed lines, and missing
files — returning ``None`` rather than raising (degradation rule: a
parser failure may cost Codex phase/summary enrichment, never
discovery).

Association safety: a rollout is only read when its FIRST line is a
``session_meta`` whose ``payload.id`` equals the expected thread id and
which is not a subagent rollout. Subagent-child rollouts deliberately
carry TWO ``session_meta`` records (child identity first, inherited
parent second) — checking only the first line is what rejects them for
the parent id; refusing ``is_subagent`` rollouts entirely keeps a
child's phase from ever attaching to a pane.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .transcript import extract_phase_tag

# How far back from EOF a reverse scan may read before giving up.
#
# Claude transcripts reliably carry a ``<phase>`` tag, so their reverse
# read early-exits in the file tail. A codex rollout only carries one if
# codex honors the SessionStart ``additionalContext`` instruction — and
# when it doesn't, an unbounded scan would json.loads every record from
# EOF to byte 0 on every mtime change, for every codex pane, inside the
# 2s daemon tick. Bounding the scan trades a tag older than the budget
# (which the branch rule then covers) for a predictable per-poll cost.
CODEX_REVERSE_SCAN_BUDGET_BYTES = 512 * 1024


@dataclass(frozen=True)
class RolloutMeta:
    thread_id: str | None
    cwd: str | None
    originator: str | None
    is_subagent: bool


def read_rollout_session_meta(path: str) -> RolloutMeta | None:
    """Parse the rollout's first line. ``None`` on any failure. Pure read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first = f.readline()
        obj = json.loads(first)
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    tid = payload.get("id")
    cwd = payload.get("cwd")
    originator = payload.get("originator")
    # Child rollouts mark themselves two ways: thread_source == "subagent"
    # and a structured (dict) ``source`` describing the spawn; accept either.
    is_subagent = payload.get("thread_source") == "subagent" or isinstance(
        payload.get("source"), dict
    )
    return RolloutMeta(
        thread_id=tid if isinstance(tid, str) and tid else None,
        cwd=cwd if isinstance(cwd, str) and cwd else None,
        originator=originator if isinstance(originator, str) else None,
        is_subagent=is_subagent,
    )


def _matches_thread(path: str, expected_thread_id: str | None) -> bool:
    if not expected_thread_id:
        return False
    meta = read_rollout_session_meta(path)
    return meta is not None and not meta.is_subagent and meta.thread_id == expected_thread_id


def _rollout_assistant_text(record: dict) -> str:
    """Assistant text from a ``response_item`` message record, else ``""``.

    Only ``response_item`` records count — ``event_msg`` records repeat the
    same assistant text and must never be a source (plan §4: duplicates
    don't duplicate).
    """
    if record.get("type") != "response_item":
        return ""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return ""
    if payload.get("type") != "message" or payload.get("role") != "assistant":
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    texts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "output_text"
    ]
    return "\n".join(t for t in texts if t).strip()


def _iter_bounded_reverse(path: str, budget: int = CODEX_REVERSE_SCAN_BUDGET_BYTES):
    """Reverse-read lines, reading at most ``budget`` raw bytes from EOF.

    The budget binds at the byte layer, BEFORE line assembly (final codex
    review P2): bounding only assembled lines would let a single enormous
    JSONL record — multi-MB tool output on one line is realistic — be read
    end-to-end just to discover it blows the budget, keeping exactly the
    unbounded I/O this constant exists to prevent. A line truncated at the
    budget floor is dropped, not yielded: parsing partial JSON can only
    mislead.
    """
    chunk_size = 64 * 1024
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        floor = max(0, pos - budget)
        leftover = b""
        while pos > floor:
            read_size = min(chunk_size, pos - floor)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size) + leftover
            lines = chunk.split(b"\n")
            if pos > 0:
                # Bytes remain before this chunk (the next chunk, or the
                # region below the budget floor) — lines[0] may be partial.
                leftover = lines[0]
                rest = lines[1:]
            else:
                leftover = b""
                rest = lines
            for raw in reversed(rest):
                if raw.strip():
                    yield raw.decode("utf-8", errors="replace")


def extract_last_codex_phase_tag(path: str, expected_thread_id: str | None) -> str | None:
    """Latest line-leading ``<phase>`` tag from the rollout's assistant text.

    Same semantics as the Claude reader: reverse-read, first assistant
    record with a *valid* tag wins (invalid tag values are skipped and the
    walk continues backward) — but bounded by
    ``CODEX_REVERSE_SCAN_BUDGET_BYTES``.
    """
    if not _matches_thread(path, expected_thread_id):
        return None
    try:
        for line in _iter_bounded_reverse(path):
            try:
                record = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            text = _rollout_assistant_text(record)
            if not text:
                continue
            tag = extract_phase_tag(text)
            if tag is not None:
                return tag
        return None
    except OSError:
        return None


def extract_last_codex_assistant_text(path: str, expected_thread_id: str | None) -> str | None:
    """Latest assistant text (rollout-tail ``latest_summary`` fallback)."""
    if not _matches_thread(path, expected_thread_id):
        return None
    try:
        for line in _iter_bounded_reverse(path):
            try:
                record = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            text = _rollout_assistant_text(record)
            if text:
                return text
        return None
    except OSError:
        return None
