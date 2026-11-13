"""Hooks-based state detection for Claude Code agents.

Claude Code can push state changes via its hook system. When a hook fires,
the handle_hook_event() entry point writes the agent's idle/running/waiting
state to a per-pane JSON file under ``/tmp/agent-hq/states/``. Agent HQ
reads these files via ``read_hook_state`` / ``read_hook_revision`` instead
of scraping pane content.

The hook no longer derives phase. It used to extract the ``<phase>`` tag
at hook-fire time, which raced the transcript flush it depended on
(#73 Test A: a tag emitted ~160 ms before the Stop hook was missed and a
stale phase froze permanently). Phase now lives entirely in the resolver,
which reverse-reads the transcript on every poll long after the flush
settles (see ``transcript.py`` / ``resolver.py``). The hook keeps the
display-only ``latest_summary`` (best-effort last assistant text on Stop).

Schema v2 (#73): every state file carries a monotonic ``revision`` integer.
Each write — hook event, daemon prune tombstone — bumps revision by 1
under a per-pane ``flock``. The revision lives inside the file so callers
read content + version atomically with a single ``read_text``.

SessionEnd writes a tombstone (``tombstone: true``, revision+1) instead
of unlinking. Preserves revision monotonicity for the brief window before
the next session writes its first hook event — without this, a watcher
holding revision=18 would reject the next session's revision=1 snapshot
until the session-id reset rule fires.

Daemon orphan cleanup (``prune_stale_hook_state_files``) unlinks files
whose pane no longer exists (safe — any future session reusing the pane
starts at revision=1) and tombstones live-pane files whose stored
``session_id`` no longer matches the discovered agent.

Reads are pure and session-scoped: ``read_hook_state`` and
``read_hook_revision`` take ``current_session_id`` and return a
``validity`` string that callers (enrichment, resolver) use to decide
between hook signal vs fallback. They never mutate disk.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import eventlog
from ._locking import file_lock
from .models import Agent, AgentState
from .runtime_dir import check_trusted, ensure_dir
from .transcript import extract_last_assistant_text

HOOK_STATE_DIR = Path("/tmp/agent-hq/states")

# tmux-assistant-resurrect's ``get_codex_session()`` resolves a codex pane's
# thread by grepping this file for the codex process pid (#208). Codex itself
# never writes it; the codex hook flavor appends a tag per session so a fresh
# ``codex`` pane (not started via ``codex resume``) is resurrectable.
CODEX_SESSION_TAGS_FILE = Path.home() / ".codex" / "session-tags.jsonl"

# Validity codes returned by the read helpers. The resolver and
# enrichment treat each distinctly.
Validity = Literal[
    "valid",  # session match (positive or both-None); use the stored value
    "missing",  # no file on disk
    "stale",  # state-only: timestamp older than 5 min (phase has no stale)
    "transient_unknown",  # exactly one of stored/current is None — skip apply
    "mismatch",  # both non-None and differ — fall back; prune will tombstone
    "legacy",  # file has no ``session_id`` key — pre-#65 migration debris
    "tombstone",  # file marked ``tombstone: true`` — session ended
]

PHASE_INSTRUCTION = (
    "Agent HQ is monitoring this session for phase tracking. At the end of "
    "EVERY assistant message, append a single line containing <phase>X</phase> "
    "where X is exactly one of: adhoc, design, implement, qa, review, ready, "
    "done.\n\n"
    "The tag describes the OVERALL state of work in this session/worktree, "
    "NOT what this single turn was about. Once you've reached a phase, "
    "KEEP emitting it until real work moves to a different phase. "
    "Tangential questions, side-conversations, or unrelated chat do NOT "
    "change the phase — keep the tag matching the actual state of the "
    "underlying work. Examples: mid-QA + user asks an unrelated question "
    "→ still qa. PR open with review complete + user chats about something "
    "else → still ready. Phase moves backward only if you genuinely return "
    "to earlier work (e.g., review finds a bug → back to implement).\n\n"
    "Phase meanings:\n"
    "- adhoc: exploring, no clear task underway\n"
    "- design: working out what to build — requirements, design tradeoffs, "
    "AND/OR drafting the concrete implementation plan (steps enumerated, "
    "files identified). Covers everything before code is being written: "
    "ideation, design discussion, and plan-writing are all design. "
    "Discussing the shape of a fix is design.\n"
    "- implement: writing code\n"
    "- qa: verifying the work — running tests / lint / manual checks, OR "
    "resting after they pass while the review-or-PR decision is still "
    "pending. This is the holding state once code is written: tests may be "
    "actively running, or they've passed and nothing has yet moved to "
    "review or met all of ready's conditions. Stay in qa here — do NOT "
    "jump to ready just because tests went green.\n"
    "- review: code review actively in progress (a review tool/agent is "
    "running, or a human reviewer was requested). While you are fixing "
    "issues a reviewer raised, you are back in implement/qa, not ready.\n"
    "- ready: implementation AND verification are complete and the ONLY "
    "thing left is opening / merging the PR. Emit ready ONLY when ALL of "
    "these hold — if any is unmet you are NOT ready (stay in qa or review): "
    "(a) committed: the work is committed for the PR. Uncommitted changes — "
    "even if tested and lint-clean — are NOT ready; that's still qa. "
    "(b) reviewed: code review has actually run, or was explicitly skipped "
    "at the user's request — AND every issue the review raised has been "
    "fixed or explicitly waved off by the user. \"Review hasn't happened / "
    'not yet decided" does NOT count as skipped, and unresolved review '
    "findings mean you are NOT ready. "
    "(c) verified: tests and lint pass, and any applicable manual/functional "
    "verification has been run when the changes need functional validation "
    "(e.g. a project skill describing how to smoke-test the app). A "
    "verification step the user explicitly declined counts as satisfied; a "
    "planned step that was neither run nor declined does not. "
    "This includes the case where the PR hasn't been opened yet (work "
    "committed, review/verification done, just no PR).\n"
    "- done: work complete. Use this when the PR for this branch is merged, "
    "OR for a one-shot task with no branch/PR — e.g. dispatching a worktree "
    "from main, answering a question, running a single command the user "
    "asked for. If you're on a feature branch whose changes aren't merged, "
    "you are NOT done — at most ready, and only if ready's full bar above "
    "holds; otherwise stay in implement/qa/review — even if you've stopped "
    "working. If the user starts a new request after done, move to the "
    "appropriate phase for that new task.\n\n"
    "The tag MUST be the very last line of your response, on its own line, "
    "with no extra text after it. This applies to EVERY assistant message — "
    "including messages that end in a tool call. The first text you produce "
    "in a new turn must end with a <phase> tag before you call any tool, "
    "so phase tracking can pick up your intent without waiting for the "
    "whole turn to finish."
)

# Maps Claude Code hook event names to AgentState values.
# None means "this event ends the session" (write a tombstone).
EVENT_STATE_MAP: dict[str, AgentState | None] = {
    "UserPromptSubmit": AgentState.RUNNING,
    "PreToolUse": AgentState.RUNNING,
    "PostToolUse": AgentState.RUNNING,
    "SubagentStart": AgentState.RUNNING,
    "PermissionRequest": AgentState.WAITING,
    # Notification intentionally excluded — fires for informational messages
    # (e.g., "agent was stopped") that don't require user input, causing
    # false WAITING detection. PermissionRequest covers actual permission prompts.
    "Stop": AgentState.IDLE,
    "SessionEnd": None,
}

# Map state strings back to AgentState enum values
_STATE_STRING_MAP: dict[str, AgentState] = {
    "running": AgentState.RUNNING,
    "waiting": AgentState.WAITING,
    "idle": AgentState.IDLE,
}


def _pane_num(pane_id: str) -> str:
    """Strip % prefix from a tmux pane ID to get the numeric part."""
    return pane_id.lstrip("%")


def state_lock_path(pane_num: str) -> Path:
    """Per-pane sidecar lock path used by hook writers + daemon prune."""
    return HOOK_STATE_DIR / f"{pane_num}.json.lock"


STALE_THRESHOLD_SECONDS = 300  # 5 minutes


def _normalize_session(value: object) -> str | None:
    """Empty-string session_id stored by legacy hooks normalizes to ``None``.

    The Claude Code hook protocol passes ``session_id`` as a string; the
    SessionStart hook may emit ``""`` before a session id is assigned.
    Treating ``""`` the same as missing keeps the 5-state matrix from
    spuriously firing a positive mismatch against a real session.
    """
    if value == "" or value is None:
        return None
    if isinstance(value, str):
        return value
    return None


def _read_state_file(state_file: Path) -> dict:
    """Read the existing state file as a dict, or {} if missing/corrupt."""
    try:
        check_trusted(state_file)
        return json.loads(state_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _classify_session(
    *,
    has_session_key: bool,
    stored: str | None,
    current: str | None,
) -> Validity:
    """Apply the 5-state session-id matrix to a freshly-read state file.

    | stored / current     | validity            |
    |----------------------|---------------------|
    | no session_id key    | legacy              |
    | match (both non-None)| valid               |
    | both None            | valid (non-Claude)  |
    | exactly one is None  | transient_unknown   |
    | both non-None, differ| mismatch            |
    """
    if not has_session_key:
        return "legacy"
    if stored is not None and current is not None:
        return "valid" if stored == current else "mismatch"
    if stored is None and current is None:
        return "valid"
    return "transient_unknown"


def _is_tombstone(data: dict) -> bool:
    return bool(data.get("tombstone")) is True


def _read_revision(data: dict) -> int:
    """Pre-v5 files have no ``revision`` key — read as 0.

    First write bumps to 1. Implementations must NOT treat missing
    ``revision`` as ``"legacy"`` (that's reserved for missing
    ``session_id`` key). The 5-state matrix still applies based on
    the session_id read.
    """
    try:
        return int(data.get("revision", 0))
    except (TypeError, ValueError):
        return 0


def read_hook_state(
    pane_id: str,
    *,
    current_session_id: str | None = None,
) -> tuple[AgentState | None, Validity]:
    """Read hook-based state for a tmux pane. Pure read.

    Returns ``(state, validity)``. ``state`` is non-None only when
    ``validity == "valid"`` AND the file is fresh (within
    ``STALE_THRESHOLD_SECONDS``). Stale files fall back to regex
    detection — the 5-min cap is intentional for state only; phase is
    sticky and has no staleness rule.
    """
    state_file = HOOK_STATE_DIR / f"{_pane_num(pane_id)}.json"
    data = _read_state_file(state_file)
    if not data:
        return None, "missing"
    if _is_tombstone(data):
        return None, "tombstone"
    timestamp = data.get("timestamp")
    if (
        not isinstance(timestamp, (int, float))
        or (time.time() - timestamp) > STALE_THRESHOLD_SECONDS
    ):
        return None, "stale"
    has_key = "session_id" in data
    stored = _normalize_session(data.get("session_id")) if has_key else None
    validity = _classify_session(has_session_key=has_key, stored=stored, current=current_session_id)
    if validity != "valid":
        return None, validity
    state_str = data.get("state")
    if state_str is None:
        return None, "valid"
    return _STATE_STRING_MAP.get(state_str), "valid"


def read_hook_revision(
    pane_id: str,
    *,
    current_session_id: str | None = None,
) -> tuple[Validity, int]:
    """Read the hook state file's session validity + revision. Pure read.

    Returns ``(validity, revision)``. The hook no longer writes phase
    (it moved to the transcript-reading resolver), but the resolver
    still observes the state file's monotonic ``revision`` so
    ``replaces()`` can order state-driven snapshot updates.

    * ``validity`` follows the 5-state matrix plus ``missing`` /
      ``tombstone``. There is NO ``"stale"`` here — revision ordering
      is about monotonicity, not freshness (that cap is state-only,
      in ``read_hook_state``).
    * ``revision`` is the on-disk integer revision. Missing files
      return 0; tombstones return their own revision (carrying the
      monotonic progression across SessionEnd).
    """
    state_file = HOOK_STATE_DIR / f"{_pane_num(pane_id)}.json"
    data = _read_state_file(state_file)
    if not data:
        return "missing", 0
    revision = _read_revision(data)
    if _is_tombstone(data):
        return "tombstone", revision
    has_key = "session_id" in data
    stored = _normalize_session(data.get("session_id")) if has_key else None
    validity = _classify_session(has_session_key=has_key, stored=stored, current=current_session_id)
    return validity, revision


@dataclass(frozen=True)
class HookIdentity:
    """Identity fields a codex hook writes into the pane state file (#206)."""

    harness: str
    session_id: str | None
    transcript_path: str | None
    cwd: str | None
    model: str | None


def _harness_from_argv(argv: list[str]) -> str:
    """``agent-hq-hook --harness codex`` → ``"codex"``; default ``"claude"``."""
    try:
        return argv[argv.index("--harness") + 1]
    except (ValueError, IndexError):
        return "claude"


def _payload_str(hook_input: dict, key: str) -> str | None:
    value = hook_input.get(key)
    return value if isinstance(value, str) and value else None


def _codex_event_owns_pane(hook_input: dict) -> bool:
    """Whether this event comes from an interactive codex TUI session that
    is coherent with the rollout it names.

    Fail-closed on three counts: an unreadable/missing rollout, a
    non-``codex-tui`` originator (``codex exec`` and friends) or a
    subagent rollout, and a payload whose ``session_id`` disagrees with
    the rollout's own ``session_meta`` id.

    That last check replaces an earlier "stored session_id differs →
    drop" guard, which was wrong in a common case: the TUI creates its
    rollout lazily, so a codex restarted in the same pane skips the
    SessionStart identity write and announces itself on its first state
    event — which the old guard rejected, stranding the pane on the dead
    session. Coherence is the property actually worth enforcing: a
    legitimate restart names its own new rollout, while leaked or
    mis-addressed events do not.
    """
    transcript_path = _payload_str(hook_input, "transcript_path")
    if not transcript_path:
        return False
    from .codex_transcript import read_rollout_session_meta

    meta = read_rollout_session_meta(transcript_path)
    if meta is None or meta.is_subagent or meta.originator != "codex-tui":
        return False
    # session_id is REQUIRED: every real 0.144.x payload carries it (Stage
    # 0 V4), so a payload without one is malformed — accepting it would
    # persist a partial record (``session_id: None``) that drops
    # enrichment to the cwd/mtime heuristic until the next good event.
    incoming = _normalize_session(hook_input.get("session_id"))
    return incoming is not None and meta.thread_id == incoming


def _codex_rollout_is_known_noninteractive(hook_input: dict) -> bool:
    """True only when the rollout is READABLE and says non-interactive.

    The mirror image of ``_codex_event_owns_pane``, and deliberately so:
    identity must fail closed (write only on positive proof), while phase
    injection must fail OPEN. The TUI creates its rollout lazily, so at
    SessionStart there is usually nothing to read — demanding proof of
    interactivity there would stop injecting the phase instruction into
    every real interactive session, which is the entire feature. A
    ``codex exec`` run, by contrast, already has its rollout on disk when
    SessionStart fires (Stage 0 probe), so the sessions we want to leave
    alone are exactly the ones we can positively identify.
    """
    transcript_path = _payload_str(hook_input, "transcript_path")
    if not transcript_path:
        return False
    from .codex_transcript import read_rollout_session_meta

    meta = read_rollout_session_meta(transcript_path)
    if meta is None:
        return False
    return meta.is_subagent or meta.originator != "codex-tui"


_ANCESTOR_WALK_MAX_DEPTH = 8


def _find_codex_ancestor_pid(start_pid: int) -> int | None:
    """Nearest ancestor (including ``start_pid``) whose comm is codex.

    The hook runs as a descendant of the codex TUI process (usually via an
    intermediate shell), while tmux-assistant-resurrect resolves a pane's
    session by the codex pid it finds walking the pane's tree downward —
    so the tag we write must carry that same pid. Bounded walk, ``None``
    on any failure: resurrection tagging is best-effort.
    """
    pid = start_pid
    for _ in range(_ANCESTOR_WALK_MAX_DEPTH):
        if pid <= 1:
            return None
        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        raw = result.stdout.strip()
        if result.returncode != 0 or not raw:
            return None
        ppid_str, _, comm = raw.partition(" ")
        try:
            ppid = int(ppid_str)
        except ValueError:
            return None
        if os.path.basename(comm.strip()) == "codex":
            return pid
        pid = ppid
    return None


def _codex_identity_fields(hook_input: dict) -> dict:
    """Identity keys carried on every codex write. Every 0.144.5 payload
    includes them (Stage 0 V4), so no cross-write merge logic is needed."""
    return {
        "harness": "codex",
        "transcript_path": _payload_str(hook_input, "transcript_path"),
        "cwd": _payload_str(hook_input, "cwd"),
        "model": _payload_str(hook_input, "model"),
    }


_SESSION_TAGS_MAX_LINES = 500


def _record_codex_session_tag(hook_input: dict, *, frontend_pid: int | None = None) -> None:
    """Ensure ``CODEX_SESSION_TAGS_FILE`` has a line for this pid+session.

    tmux-assistant-resurrect's ``get_codex_session()`` greps the file for
    ``"pid": <codex-pid>`` and reads ``.session`` from the last match, so
    the record must keep exactly those key names/types — and dedup must
    compare against the pid's LAST record, not membership anywhere: after
    pid reuse (session A, reused for B, reused for A again) an
    anywhere-match would skip the third write and leave the last match
    pointing at B. Codex never fires SessionEnd and pids recycle slowly;
    stale lines are harmless and growth is capped instead of pruned.

    Called only after ``_codex_event_owns_pane`` — a ``codex exec`` run or
    a subagent sharing the pane must not become a resurrection target —
    and only AFTER the authoritative state/identity write, so a slow walk
    or lock wait can never starve that write inside the hook timeout.
    Assumes agent-hq is the file's only writer (nothing else writes it
    today — that gap is why this exists): the flock coordinates our own
    processes, and the rare cap rewrite would lose a concurrent append
    from an uncoordinated foreign writer. Best-effort: any failure is
    confined to stderr; hook output and state writes are never affected.
    """
    try:
        session = _normalize_session(hook_input.get("session_id"))
        if session is None:
            return
        pid = frontend_pid
        if pid is None and os.environ.get("AGENT_HQ_CODEX_WRAPPED"):
            from .codex_runtime import _alive, read_registrations

            receipts = [r for r in read_registrations(alive=_alive)
                        if r["pane_id"] == os.environ.get("TMUX_PANE")
                        and r.get("thread_id") == session]
            if len(receipts) != 1:
                return
            pid = receipts[0]["frontend_pid"]
        elif pid is None:
            pid = _find_codex_ancestor_pid(os.getppid())
        if pid is None:
            return
        tags_file = CODEX_SESSION_TAGS_FILE
        tags_file.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(tags_file.parent / (tags_file.name + ".lock")):
            try:
                lines = tags_file.read_text().splitlines()
            except FileNotFoundError:
                lines = []
            lines = [line for line in lines if line.strip()]
            last_session_for_pid: str | None = None
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("pid") == pid:
                    last_session_for_pid = record.get("session")
            needs_append = last_session_for_pid != session
            if needs_append:
                lines.append(
                    json.dumps(
                        {
                            "pid": pid,
                            "session": session,
                            "cwd": _payload_str(hook_input, "cwd"),
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        }
                    )
                )
            if len(lines) > _SESSION_TAGS_MAX_LINES:
                # Rare rewrite path (applies even on a dedup pass, so an
                # oversized pre-existing file shrinks): atomic replace
                # keeping the newest N so a concurrent plugin read never
                # sees a truncated file.
                lines = lines[-_SESSION_TAGS_MAX_LINES:]
                fd, tmp_name = tempfile.mkstemp(dir=tags_file.parent, suffix=".jsonl.tmp")
                try:
                    with os.fdopen(fd, "w") as f:
                        f.write("\n".join(lines) + "\n")
                    os.replace(tmp_name, tags_file)
                except Exception:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
            elif needs_append:
                with tags_file.open("a") as f:
                    f.write(lines[-1] + "\n")
    except Exception as exc:
        print(f"agent-hq-hook: failed to write session tag: {exc}", file=sys.stderr)


# pid -> (start_time, cached_at). A process's start time never changes, but
# pids are recycled, so entries carry a short TTL. Without the cache each
# enrichment pass would shell out to `ps` several times per codex agent
# (association read, state read, dedupe tiebreak) on every 2s tick. The TTL
# only needs to amortize the reads within a couple of 2s ticks — keeping it
# short also bounds the fail-open window where a pid recycled into the same
# pane could inherit the dead process's cached start time.
_process_start_cache: dict[int, tuple[float | None, float]] = {}
_PROCESS_START_TTL = 10.0
# ``ps etime`` resolution: elapsed times are truncated to whole seconds.
_PROCESS_START_RESOLUTION = 1.0


def _clear_process_start_cache() -> None:
    """Test hook: drop cached process start times."""
    _process_start_cache.clear()


def process_start_time(pid: int) -> float | None:
    """Unix start time of ``pid`` (cached), or ``None`` if unknown."""
    cached = _process_start_cache.get(pid)
    now = time.monotonic()
    if cached is not None and (now - cached[1]) < _PROCESS_START_TTL:
        return cached[0]
    started = _process_start_time_uncached(pid)
    _process_start_cache[pid] = (started, now)
    return started


def _parse_etime(raw: str) -> float | None:
    """``ps etime`` (``[[dd-]hh:]mm:ss``) → elapsed seconds, or ``None``."""
    days = 0
    if "-" in raw:
        day_part, _, raw = raw.partition("-")
        try:
            days = int(day_part)
        except ValueError:
            return None
    parts = raw.split(":")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.insert(0, 0)
    hours, minutes, seconds = nums
    return float(((days * 24 + hours) * 3600) + minutes * 60 + seconds)


def _process_start_time_uncached(pid: int) -> float | None:
    """Unix start time of ``pid`` via ``ps``, or ``None`` if unknown.

    Used to decide whether a pane's *current* harness process is the one
    that wrote the pane's identity record (see ``read_hook_identity``).

    Derived from ``etime`` (elapsed since start), not ``lstart``: elapsed
    time is numeric — immune to strptime's locale-dependent month/day
    names AND to the DST fall-back hour, where a wall-clock start time is
    ambiguous. ``etime`` truncates to whole seconds, so the computed start
    can only read LATER than the true start — which errs toward rejecting
    identity (fail closed), never falsely accepting it.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw:
        return None
    elapsed = _parse_etime(raw)
    if elapsed is None:
        return None
    return time.time() - elapsed


def read_hook_identity(pane_id: str, *, agent_pid: int | None = None) -> HookIdentity | None:
    """Pure read of the identity fields; ``None`` when absent/tombstone.

    Only codex hooks write a ``harness`` key, so a claude-written (or
    pre-#206) state file returns ``None`` — and a claude session reusing
    the pane organically erases stale codex identity because claude
    writes carry none of these keys.

    Expiry (multi-review P1, both reviewers): Codex has no SessionEnd, so
    when hooks stop firing (hooks.json edited, trust revoked) the last
    identity is the last write, and a later session in the same pane must
    not inherit it. But elapsed time is the wrong discriminator: a live
    session that simply sits idle would lose its identity, both same-cwd
    panes would fall back to the newest-rollout heuristic, collide, and be
    cleared by ``_clear_duplicate_codex_thread_ids`` — losing phase, deep
    link, and resume target for two healthy agents.

    So ownership is decided by the pane's current harness process, at ANY
    age: the identity holds only when that process started BEFORE the
    record was written (it is the process that wrote it, however idle). A
    process that started after — pane reuse, including reuse INSIDE the
    staleness window with a missed SessionStart — falls back to rollout
    discovery. Elapsed time then only decides whether a *pid-less* caller
    may trust the record: without a pid there is nothing to validate
    against, so the freshness cap remains the fallback rule.
    """
    state_file = HOOK_STATE_DIR / f"{_pane_num(pane_id)}.json"
    data = _read_state_file(state_file)
    if not data or _is_tombstone(data):
        return None
    harness = data.get("harness")
    if not isinstance(harness, str) or not harness:
        return None
    timestamp = data.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return None
    if agent_pid is None:
        if (time.time() - timestamp) > STALE_THRESHOLD_SECONDS:
            return None
    else:
        started = process_start_time(agent_pid)
        if started is None or started > (timestamp - _PROCESS_START_RESOLUTION):
            # ``ps`` reports whole seconds while ``timestamp`` keeps
            # fractions, so the comparison is ambiguous inside a ~1s
            # window. Require a clear margin and treat the ambiguous
            # window as "not ours" — identity fails closed, and the
            # heuristic fallback covers it.
            return None

    def _s(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) and value else None

    return HookIdentity(
        harness=harness,
        session_id=_normalize_session(data.get("session_id")),
        transcript_path=_s("transcript_path"),
        cwd=_s("cwd"),
        model=_s("model"),
    )


def read_hook_summary(pane_id: str, *, max_age: float | None = None) -> str | None:
    """Return the latest_summary from the pane's state file, or None.

    Not session-scoped — summary is a UI nicety; mismatches simply
    show the prior session's summary briefly until the next event
    overwrites the file. Returns None for tombstones.

    ``max_age`` bounds how old the record may be. Codex passes it so a
    summary left behind when hooks stopped firing can't suppress the
    rollout-tail fallback forever (the card would freeze on an old reply
    while the rollout kept growing). Claude passes nothing — its
    SessionEnd tombstone already bounds the record's life.
    """
    state_file = HOOK_STATE_DIR / f"{_pane_num(pane_id)}.json"
    data = _read_state_file(state_file)
    if not data or _is_tombstone(data):
        return None
    if max_age is not None:
        timestamp = data.get("timestamp")
        if not isinstance(timestamp, (int, float)) or (time.time() - timestamp) > max_age:
            return None
    summary = data.get("latest_summary")
    if isinstance(summary, str) and summary:
        return summary
    return None


def _atomic_write(state_file: Path, payload: dict) -> None:
    """Atomic tempfile-rename write; the result is always mode 0600.

    Caller MUST hold ``file_lock(state_lock_path(pane_num))``. Without the
    lock, two writers can race on the same destination and tombstone state
    can be clobbered by an event that read before the tombstone landed.
    """
    ensure_dir(state_file.parent)
    fd, tmp_path = tempfile.mkstemp(
        dir=state_file.parent,
        prefix=f"{state_file.stem}_",
        suffix=".json.tmp",
    )
    try:
        # Always private: the payload carries the last assistant message,
        # which can repeat secrets the agent read (#263 F6).
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_path, state_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_tombstone(state_file: Path, *, revision: int) -> None:
    """Mark a state file as session-ended without losing the revision count.

    Caller must hold the per-pane flock. A future session writing to the
    same file reads this revision and bumps to revision+1, so a watcher
    holding the old session's revision can never reject the new session's
    first snapshot for being "older".
    """
    payload = {
        "revision": revision,
        "tombstone": True,
        "session_id": None,
        "timestamp": time.time(),
    }
    _atomic_write(state_file, payload)


def handle_hook_event() -> None:
    """Entry point for the agent-hq-hook command.

    Reads hook event JSON from stdin, maps it to an agent state, and
    writes the state file under per-pane flock. Always outputs ``{}``
    to stdout — the protocol forbids raising.

    ``--harness codex`` (#206) selects the codex flavor: identity fields
    (harness/transcript_path/cwd/model) ride on every write, SessionStart
    also writes an identity record, Stop takes ``latest_summary`` from the
    payload's ``last_assistant_message``, and there is deliberately NO
    SessionEnd branch — Codex never fires one (Stage 0 V3), so expiry
    stays with prune/staleness/pane-reuse.
    """
    eventlog.set_process_role("hook")
    harness = _harness_from_argv(sys.argv)
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        print("{}")
        return

    pane_id = os.environ.get("TMUX_PANE")
    if not pane_id:
        print("{}")
        return

    event_name = hook_input.get("hook_event_name", "")

    # SessionStart: inject the phase-emit instruction.
    if event_name == "SessionStart":
        if harness == "codex" and _codex_rollout_is_known_noninteractive(hook_input):
            # A `codex exec` run or subagent we deliberately ignore: it must
            # not be told to emit <phase> tags either, or scripted output and
            # subagent replies get Agent HQ's monitoring text appended.
            print("{}")
            return
        if harness == "codex" and _codex_event_owns_pane(hook_input):
            # Authoritative pane→thread association: write identity now so
            # enrichment can prefer it over the cwd/mtime rollout heuristic.
            # No ``state`` key — SessionStart implies no state, matching
            # the claude semantics of a state-less file read.
            #
            # Gated on the rollout being an interactive TUI session (not a
            # `codex exec` run or a subagent), mirroring the filter
            # ``enrichment._read_rollout_meta`` applies to the heuristic
            # path: a scripted run inheriting TMUX_PANE must not steal the
            # association from the interactive session in that pane.
            try:
                pane_num = _pane_num(pane_id)
                state_file = HOOK_STATE_DIR / f"{pane_num}.json"
                ensure_dir(HOOK_STATE_DIR)
                with file_lock(state_lock_path(pane_num)):
                    existing = _read_state_file(state_file)
                    payload = {
                        "revision": _read_revision(existing) + 1,
                        "session_id": _normalize_session(hook_input.get("session_id")),
                        "timestamp": time.time(),
                        **_codex_identity_fields(hook_input),
                    }
                    _atomic_write(state_file, payload)
            except OSError as exc:
                print(f"agent-hq-hook: failed to write identity: {exc}", file=sys.stderr)
                eventlog.emit(
                    "error",
                    "hook state write failed",
                    {"kind": "identity", "pane": pane_id, "harness": harness, "error": str(exc)},
                )
            _record_codex_session_tag(hook_input)
        # Unconditional (#285): SessionStart is the only delivery point, so
        # gating on the daemon being up meant a session that started
        # during a daemon gap (install-global, crash, manual stop) was
        # never told to emit <phase> tags — for its whole lifetime. An
        # unmonitored session carrying a tag line is harmless; a
        # monitored one that never tags is not.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": PHASE_INSTRUCTION,
                    }
                }
            )
        )
        return

    if event_name not in EVENT_STATE_MAP:
        print("{}")
        return

    if harness == "codex":
        if not _codex_event_owns_pane(hook_input):
            # Same interactive filter as the SessionStart identity write. It
            # is needed on this path too because the TUI creates its rollout
            # lazily: SessionStart usually fires before the file exists, so
            # identity most often first lands on a state event (verified
            # live on codex-cli 0.144.6). Without the filter here, a
            # `codex exec` run or subagent sharing the pane would still
            # overwrite the interactive session's association — and its
            # state would flip the pane's card.
            print("{}")
            return

    pane_num = _pane_num(pane_id)
    state_file = HOOK_STATE_DIR / f"{pane_num}.json"
    target_state = EVENT_STATE_MAP[event_name]

    # The state dir is created (root secured) by file_lock inside the try:
    # an unsafe runtime root must degrade like any OSError, never raise.
    try:
        with file_lock(state_lock_path(pane_num)):
            existing = _read_state_file(state_file)
            next_revision = _read_revision(existing) + 1

            if target_state is None:
                # SessionEnd: tombstone instead of unlink. Preserves the
                # monotonic revision across the session boundary so the
                # next session's first write (revision = tombstone + 1)
                # always wins comparison.
                _write_tombstone(state_file, revision=next_revision)
                print("{}")
                return

            incoming_session = _normalize_session(hook_input.get("session_id"))

            # No session-mismatch guard here: ``_codex_event_owns_pane``
            # already rejected exec/subagent events and any payload whose
            # session_id disagrees with the rollout it names, so whatever
            # reaches this point is a coherent interactive session — and
            # the newest one legitimately owns the pane (a restart claims
            # it on its first state event).

            # Display-only: best-effort last assistant text on Stop. No
            # stability/retry loop — phase (the thing that needed the
            # transcript to be fully flushed) is no longer derived here;
            # a slightly stale summary is purely cosmetic. The resolver
            # reads the transcript itself for the authoritative phase.
            # Codex Stop payloads carry the message directly (Stage 0 V3);
            # the claude-format transcript reader can't parse rollouts.
            latest_summary: str | None = None
            if event_name == "Stop":
                if harness == "codex":
                    last_message = hook_input.get("last_assistant_message")
                    if isinstance(last_message, str) and last_message.strip():
                        latest_summary = last_message.strip()
                else:
                    transcript_path = hook_input.get("transcript_path")
                    if transcript_path:
                        latest_summary = extract_last_assistant_text(transcript_path)

            state_data = {
                "revision": next_revision,
                "state": target_state.value,
                "session_id": incoming_session,
                "hook_event": event_name,
                "timestamp": time.time(),
                "latest_summary": latest_summary,
            }
            if harness == "codex":
                state_data.update(_codex_identity_fields(hook_input))
            _atomic_write(state_file, state_data)
    except OSError as exc:
        # Best-effort: hook protocol must not raise. But surfacing the
        # error to stderr (visible via Claude Code /hooks debug) helps
        # diagnose disk-full / read-only / permission issues.
        print(f"agent-hq-hook: failed to write {state_file}: {exc}", file=sys.stderr)
        eventlog.emit(
            "error",
            "hook state write failed",
            {"kind": "state", "pane": pane_id, "harness": harness, "error": str(exc)},
        )

    if harness == "codex":
        # Resurrection tag (#208) rides any coherent event, not just
        # SessionStart — with lazy rollout creation, a restarted session's
        # first coherent event is usually a state event. Deliberately AFTER
        # the state write: tagging is best-effort and must never starve the
        # authoritative write inside the hook timeout.
        _record_codex_session_tag(hook_input)

    print("{}")


def prune_stale_hook_state_files(
    active_pane_ids: set[str],
    agents_by_pane: dict[str, Agent],
) -> None:
    """Daemon-only garbage collection over ``HOOK_STATE_DIR``.

    Two paths:

    * **Orphan** (pane no longer exists in tmux): ``os.unlink`` under the
      per-pane flock. Safe because no future session can compare against
      the deleted file — any later session reusing the pane number
      starts at revision=1.
    * **Mismatch / legacy** (still-live pane, stored session_id differs
      from the discovered agent's transcript id, or no ``session_id``
      key): write a tombstone with ``revision+1``. Preserves monotonic
      revision so a watcher holding the prior session's snapshot
      doesn't reject the tombstone for being "older".

    An empty ``active_pane_ids`` is treated as "no observation, do
    nothing" — a transient tmux failure mustn't wipe legitimate state.

    Caller passes ``active_pane_ids`` derived from ``get_all_panes()``
    (all tmux panes, not just discovered agents). Using only-agents
    would unlink a state file for a still-live pane that discovery
    transiently miss-classified.
    """
    if not active_pane_ids:
        return
    try:
        entries = list(HOOK_STATE_DIR.iterdir())
    except (FileNotFoundError, OSError):
        return
    active_nums = {_pane_num(pid) for pid in active_pane_ids}

    for path in entries:
        if not path.is_file() or path.suffix != ".json":
            continue
        pane_num = path.stem
        pane_id = f"%{pane_num}"

        if pane_num not in active_nums:
            with file_lock(state_lock_path(pane_num)):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            continue

        # Harness-generic (#206): claude's stable_id IS the transcript
        # session id; codex's is the thread UUID, so stale codex state
        # from a prior session can now tombstone on mismatch too.
        agent = agents_by_pane.get(pane_id)
        current_session_id = agent.session_metadata.stable_id if agent else None
        with file_lock(state_lock_path(pane_num)):
            # Re-read under the lock so the file we classify is the file
            # we tombstone. Without the lock, a hook fired between read
            # and write could land a fresh state with the new session id
            # which we'd then overwrite with a stale tombstone.
            data = _read_state_file(path)
            if not data or _is_tombstone(data):
                continue
            has_key = "session_id" in data
            stored = _normalize_session(data.get("session_id")) if has_key else None
            validity = _classify_session(
                has_session_key=has_key, stored=stored, current=current_session_id
            )
            if validity in ("mismatch", "legacy"):
                _write_tombstone(path, revision=_read_revision(data) + 1)
