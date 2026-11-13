"""Transcript-local scrollback: page a Claude Code transcript into turns (#171).

Claude Code's fullscreen ``/tui`` renderer keeps scrollback off-tmux, so
``capture-pane`` only ever sees one viewport. Instead of remote-controlling
the pane (PR #152, rejected hands-on), history is rendered locally from the
agent's transcript JSONL: ``read_history_page`` walks the file backwards in
bounded chunks and maps records onto renderable conversation turns.

Pagination cursors are byte offsets of line starts. Transcripts are
append-only, so a cursor handed to a caller stays valid while the file
grows — the next page is simply "everything strictly before this offset".

Same conventions as ``transcript.py``: pure functions of (path), no clock,
no writes, tolerant reads (malformed lines skipped, ``OSError`` → empty
page). Skipped on purpose: ``thinking`` blocks, ``tool_result`` bodies
(these are what balloon transcripts to tens of MB), ``isSidechain``
records (subagent traffic), and non-message record types.

Injected ``type: user`` records are NOT the user (live-transcript audit
2026-07-17: 33-68% of raw user records). Claude Code's own UI renders them
as dimmed transient notices or not at all, so history mirrors that:
``isMeta`` records (skill/command expansions, hook feedback) are skipped,
compact-continuation summaries become a divider, and task notifications /
teammate messages / local-command output / interrupt markers become
condensed ``system`` turns. Local slash commands render as the one-line
``/name args`` the user actually typed.
"""

from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass, field
from typing import Literal

from .transcript import _iter_lines_reverse_with_offsets, transcript_path_for

TurnKind = Literal["user", "assistant", "tool", "system"]

_MAX_TOOL_ARG_LEN = 80

# Representative argument per tool for the one-line summary, most specific
# first; any tool not matching falls back to its first string input value.
_TOOL_ARG_KEYS = ("command", "file_path", "pattern", "description", "prompt", "query", "skill")


@dataclass(frozen=True)
class TranscriptTurn:
    """One renderable unit: a user prompt, assistant text, or tool one-liner."""

    kind: TurnKind
    text: str
    timestamp: str | None = None


@dataclass(frozen=True)
class HistoryPage:
    """A page of turns (oldest-first) plus the cursor for the previous page."""

    turns: list[TranscriptTurn] = field(default_factory=list)
    next_cursor: int | None = None
    has_more: bool = False


def locate_transcript(cwd: str, transcript_session_id: str | None) -> str | None:
    """First existing transcript JSONL candidate for an agent, else None.

    The stat-side companion to the pure ``transcript_path_for``; the
    gate for history availability shared by the web routes and the TUI.
    Non-Claude agents and session-id-less Claude agents yield no
    candidates and stay Live-only.
    """
    for candidate in transcript_path_for(cwd, transcript_session_id):
        if os.path.exists(candidate):
            return candidate
    return None


def _tool_summary(block: dict) -> str:
    """One-line ``Name(arg)`` summary of a ``tool_use`` block."""
    name = block.get("name") or "tool"
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    arg = ""
    for key in _TOOL_ARG_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            arg = value.strip()
            break
    else:
        for value in tool_input.values():
            if isinstance(value, str) and value.strip():
                arg = value.strip()
                break
    arg = " ".join(arg.split())
    if len(arg) > _MAX_TOOL_ARG_LEN:
        arg = arg[:_MAX_TOOL_ARG_LEN] + "…"
    return f"{name}({arg})"


_COMPACT_DIVIDER = "— conversation compacted —"

_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_LOCAL_STDOUT_RE = re.compile(r"<local-command-stdout>(.*?)</local-command-stdout>", re.S)
_TASK_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)
_TEAMMATE_TAG_RE = re.compile(r"<teammate-message\b([^>]*)>(.*?)</teammate-message>", re.S)
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
# Anchored: only a LEADING wrapper (after the delivery prefix) is the
# envelope — a tag quoted inside a message body is just body text.
_CROSS_SESSION_RE = re.compile(
    r"<cross-session-message\b([^>]*)>(.*?)(?:</cross-session-message>|$)", re.S
)
_MAX_PEER_NAME_LEN = 40
_CROSS_SESSION_PREFIX = "Another Claude session sent a message:"


def _teammate_summary(text: str) -> str:
    """``teammate <id>: <summary attr or first body line>`` one-liner."""
    teammate = "teammate"
    body_first = ""
    match = _TEAMMATE_TAG_RE.search(text)
    if match:
        attrs, body = match.group(1), match.group(2)
        id_match = re.search(r'teammate_id="([^"]*)"', attrs)
        if id_match and id_match.group(1):
            teammate = f"teammate {id_match.group(1)}"
        summary_match = re.search(r'summary="([^"]*)"', attrs)
        if summary_match and summary_match.group(1):
            return f"{teammate}: {summary_match.group(1)}"
        for line in body.splitlines():
            if line.strip():
                body_first = line.strip()
                break
    if len(body_first) > _MAX_TOOL_ARG_LEN:
        body_first = body_first[:_MAX_TOOL_ARG_LEN] + "…"
    return f"{teammate}: {body_first}" if body_first else f"{teammate}: message"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _peer_message_turn(origin: object, text: str, timestamp: str | None) -> TranscriptTurn:
    """``message from @<name>: <first body line>`` for a cross-session message (#287).

    Prefers the structured ``origin`` (``name``/``body``) Claude Code records
    alongside the text, falling back to the ``<cross-session-message>``
    wrapper's ``from-name`` attribute and body. Both name sources are
    optional — an unnamed sender reads as "another session".
    """
    origin = origin if isinstance(origin, dict) else {}
    stripped = (text or "").lstrip()
    if stripped.startswith(_CROSS_SESSION_PREFIX):
        stripped = stripped[len(_CROSS_SESSION_PREFIX) :].lstrip()
    match = _CROSS_SESSION_RE.match(stripped)
    attrs, tag_body = (match.group(1), match.group(2)) if match else ("", "")
    name = origin.get("name")
    if not isinstance(name, str) or not name.strip():
        name_match = re.search(r'from-name="([^"]*)"', attrs)
        name = html.unescape(name_match.group(1)) if name_match else ""
    name = name.strip()
    if len(name) > _MAX_PEER_NAME_LEN:
        name = name[:_MAX_PEER_NAME_LEN] + "…"
    sender = f"@{name}" if name else "another session"
    body = origin.get("body")
    if not isinstance(body, str):
        # No structured body: the wrapper's body, else (a direct socket
        # post carries no wrapper) the text after the delivery prefix.
        body = tag_body if match else stripped
    first = _first_line(body)
    if len(first) > _MAX_TOOL_ARG_LEN:
        first = first[:_MAX_TOOL_ARG_LEN] + "…"
    return TranscriptTurn("system", f"message from {sender}: {first or 'message'}", timestamp)


def _is_peer(origin: object) -> bool:
    return isinstance(origin, dict) and origin.get("kind") == "peer"


def _classify_user_text(text: str, timestamp: str | None) -> TranscriptTurn | None:
    """Map extracted user-record text onto its turn (or None to skip).

    Everything here is harness-injected except plain prompts and local
    slash commands — the latter stay ``user`` (the user typed them) but
    condense to the ``/name args`` line the live UI shows.
    """
    stripped = text.lstrip()
    if stripped.startswith("<command-"):  # tag order varies: name/message first
        name_match = _COMMAND_NAME_RE.search(text)
        args_match = _COMMAND_ARGS_RE.search(text)
        name = (name_match.group(1) if name_match else "").strip()
        args = " ".join((args_match.group(1) if args_match else "").split())
        command = f"{name} {args}".strip()
        return TranscriptTurn("user", command, timestamp) if command else None
    if stripped.startswith("<local-command-stdout>"):
        match = _LOCAL_STDOUT_RE.search(text)
        body = _ANSI_SGR_RE.sub("", match.group(1)).strip() if match else ""
        return TranscriptTurn("system", body, timestamp) if body else None
    if stripped.startswith("<task-notification>"):
        match = _TASK_SUMMARY_RE.search(text)
        body = match.group(1).strip() if match else "background task update"
        return TranscriptTurn("system", body, timestamp)
    if stripped.startswith("<cross-session-message") or (
        stripped.startswith(_CROSS_SESSION_PREFIX) and not _TEAMMATE_TAG_RE.search(text)
    ):
        return _peer_message_turn(None, text, timestamp)
    if stripped.startswith((_CROSS_SESSION_PREFIX, "<teammate-message")):
        return TranscriptTurn("system", _teammate_summary(text), timestamp)
    if stripped.startswith("[Request interrupted"):
        return TranscriptTurn("system", "Interrupted by user", timestamp)
    return TranscriptTurn("user", text, timestamp)


# Runs of this many consecutive tool turns collapse into one summary line
# (Claude Code's condensed idiom); shorter runs stay individual.
_TOOL_RUN_COLLAPSE_MIN = 3


def _collapse_tool_runs(turns: list[TranscriptTurn]) -> list[TranscriptTurn]:
    """Collapse long consecutive tool runs into a ``N tool calls · …`` line.

    Consecutive-ness is about rendered order, not record boundaries; any
    non-tool turn breaks a run. Tool names are counted and listed most-
    frequent first (ties keep first-seen order). A run split across page
    boundaries yields one summary line per page — acceptable, the pages
    are already independent renders.
    """
    out: list[TranscriptTurn] = []
    run: list[TranscriptTurn] = []

    def flush() -> None:
        if len(run) >= _TOOL_RUN_COLLAPSE_MIN:
            counts: dict[str, int] = {}
            for turn in run:
                name = turn.text.split("(", 1)[0]
                counts[name] = counts.get(name, 0) + 1
            parts = [
                f"{name} ×{n}" if n > 1 else name
                for name, n in sorted(counts.items(), key=lambda kv: -kv[1])
            ]
            summary = f"{len(run)} tool calls · " + ", ".join(parts)
            out.append(TranscriptTurn("tool", summary, run[0].timestamp))
        else:
            out.extend(run)
        run.clear()

    for turn in turns:
        if turn.kind == "tool":
            run.append(turn)
        else:
            flush()
            out.append(turn)
    flush()
    return out


def _text_from_blocks(content: list) -> str:
    texts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n\n".join(t.strip() for t in texts if t and t.strip())


def _turns_from_record(record: dict) -> list[TranscriptTurn]:
    """Map one transcript record onto zero or more turns (record order)."""
    record_type = record.get("type")
    if record_type not in ("user", "assistant", "attachment") or record.get("isSidechain"):
        return []
    if record_type == "attachment":
        # Only a cross-session message queued while the session was busy
        # (#287); every other attachment kind stays out of history.
        attachment = record.get("attachment")
        if (
            isinstance(attachment, dict)
            and attachment.get("type") == "queued_command"
            and _is_peer(attachment.get("origin"))
        ):
            prompt = attachment.get("prompt")
            return [
                _peer_message_turn(
                    attachment.get("origin"),
                    prompt if isinstance(prompt, str) else "",
                    record.get("timestamp"),
                )
            ]
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    timestamp = record.get("timestamp")

    if record_type == "user":
        if _is_peer(record.get("origin")):
            # Received cross-session message (#287): flagged isMeta, but it
            # is a real conversational event, so it renders as a notice.
            text = content if isinstance(content, str) else _text_from_blocks(content or [])
            return [_peer_message_turn(record.get("origin"), text, timestamp)]
        if record.get("isMeta"):
            return []  # skill/command expansions, hook feedback, caveats
        if record.get("isCompactSummary") or record.get("isVisibleInTranscriptOnly"):
            return [TranscriptTurn("system", _COMPACT_DIVIDER, timestamp)]
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = _text_from_blocks(content)  # tool_result blocks drop out here
        else:
            return []
        if not text:
            return []
        turn = _classify_user_text(text, timestamp)
        return [turn] if turn else []

    turns: list[TranscriptTurn] = []
    if isinstance(content, str):
        if content.strip():
            turns.append(TranscriptTurn("assistant", content.strip(), timestamp))
        return turns
    if not isinstance(content, list):
        return []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = (block.get("text") or "").strip()
            if text:
                turns.append(TranscriptTurn("assistant", text, timestamp))
        elif block_type == "tool_use":
            turns.append(TranscriptTurn("tool", _tool_summary(block), timestamp))
    return turns


def read_history_page(
    path: str,
    before_offset: int | None = None,
    max_turns: int = 40,
) -> HistoryPage:
    """Read one page of history ending just before ``before_offset``.

    Walks the transcript backwards from ``before_offset`` (or EOF),
    accumulating whole records until at least ``max_turns`` turns are
    collected — a record's turns are never split across pages, so a page
    can slightly overshoot ``max_turns``. Returns turns oldest-first.

    ``next_cursor`` is the byte offset of the earliest line this page
    consumed; pass it back as ``before_offset`` to fetch the previous
    page. ``None`` (with ``has_more=False``) means the beginning was
    reached. ``has_more=True`` can be conservative: the remaining bytes
    may hold only skipped records, in which case the next page comes
    back empty with ``has_more=False`` — callers should treat an empty
    page as end-of-history, not an error.
    """
    record_turns: list[list[TranscriptTurn]] = []
    turn_count = 0
    earliest_offset: int | None = None
    stopped_early = False
    try:
        for offset, line in _iter_lines_reverse_with_offsets(path, end_pos=before_offset):
            earliest_offset = offset
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            turns = _turns_from_record(record)
            if not turns:
                continue
            record_turns.append(turns)
            turn_count += len(turns)
            if turn_count >= max_turns:
                stopped_early = True
                break
    except OSError:
        return HistoryPage()

    ordered = _collapse_tool_runs([turn for turns in reversed(record_turns) for turn in turns])
    has_more = stopped_early and earliest_offset is not None and earliest_offset > 0
    return HistoryPage(
        turns=ordered,
        next_cursor=earliest_offset if has_more else None,
        has_more=has_more,
    )
