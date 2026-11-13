"""Phase-override store and feedback log.

Owns two on-disk artifacts shared between the daemon and the on-demand
``agent-hq web`` server:

* ``/tmp/agent-hq/overrides.json`` — current per-pane overrides. v2
  schema is ``{"revision": int, "entries": {pane_id: entry}}``; legacy
  v1 files (no envelope) read as ``(legacy_dict, 0)`` on first load
  and migrate to v2 on the first write.
* ``~/.config/agent-hq/phase-feedback.jsonl`` — append-only audit log
  of every confirm / override / undo / note the user issues.

Cross-process safety for ``overrides.json`` lives in the
``file_lock(OVERRIDES_LOCK)`` wrapper around every read-modify-write
cycle. ``phase-feedback.jsonl`` uses POSIX O_APPEND which is atomic for
small lines on local filesystems; the in-process ``threading.Lock``
serializes intra-process callers and is held while the line is written
so concurrent appends never interleave.

Phase application is NOT done here. The resolver
(``agent_hq.resolver.resolve_phase_snapshot``) is the single place that
turns a stored override entry into an ``effective_phase``. This module
just persists entries and prunes the ones whose stored session_id no
longer matches a live agent on the same pane.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from ._locking import file_lock
from .models import Agent, AgentType, session_match_state
from .runtime_dir import check_trusted, ensure_dir

log = logging.getLogger(__name__)

OVERRIDES_FILE = Path("/tmp/agent-hq/overrides.json")
OVERRIDES_LOCK = Path("/tmp/agent-hq/overrides.json.lock")
FEEDBACK_FILE = Path.home() / ".config" / "agent-hq" / "phase-feedback.jsonl"

# Serializes feedback.jsonl appends within a single process. Overrides
# now have their own cross-process flock (OVERRIDES_LOCK).
_FEEDBACK_LOCK = threading.Lock()


def _lock_path_for(file_path: Path) -> Path:
    """Compute the sidecar lock path for an arbitrary overrides file path.

    Callers that monkey-patch ``OVERRIDES_FILE`` (tests) need the lock
    file to follow. Production code uses ``OVERRIDES_LOCK`` directly.
    """
    return file_path.with_name(file_path.name + ".lock")


class OverridesUnavailable(OSError):
    """Raised by ``_read_overrides_or_raise`` when overrides.json exists but
    can't be read/parsed. Subclasses ``OSError`` so the existing
    ``except OSError`` handlers in ``web/routes/phase.py`` (and the
    daemon's blanket per-tick handler) catch it without any change there.
    Used only by the write-path mutators below — a missing file is still
    legitimately empty and does NOT raise this."""


def _parse_overrides_payload(data: object) -> tuple[dict[str, dict], int]:
    """Turn parsed JSON into ``(entries, revision)``. Shared by the
    tolerant and strict readers so the v1/v2 envelope logic lives once."""
    if not isinstance(data, dict):
        return {}, 0
    # v2 envelope?
    if "entries" in data and isinstance(data.get("entries"), dict):
        entries = {
            k: v for k, v in data["entries"].items() if isinstance(v, dict) and isinstance(k, str)
        }
        try:
            revision = int(data.get("revision", 0))
        except (TypeError, ValueError):
            revision = 0
        return entries, revision
    # v1 (legacy): root is the entries dict.
    entries = {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(k, str)}
    return entries, 0


def read_overrides() -> tuple[dict[str, dict], int]:
    """Read overrides.json. Returns ``(entries, revision)``.

    Both values come from the same atomic ``read_text`` — there is no
    separate ``os.stat`` so callers see a consistent view. On missing
    or corrupt input returns ``({}, 0)`` so the resolver still works.
    Tolerant: used by read-only callers (resolver, cache, watcher) where a
    stale/empty view for one tick is harmless. Mutators use
    ``_read_overrides_or_raise`` instead so a read failure can't clobber
    the still-good on-disk file (AHQ-014).

    Legacy v1 files (no ``{"revision","entries"}`` envelope) read as
    ``(legacy_dict, 0)``; the next write produces a v2 shape.
    """
    try:
        check_trusted(OVERRIDES_FILE)
        raw = OVERRIDES_FILE.read_text()
    except FileNotFoundError:
        return {}, 0
    except OSError as exc:
        log.error("cannot read overrides.json (%s): %s", OVERRIDES_FILE, exc)
        return {}, 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("overrides.json corrupt, treating as empty")
        return {}, 0
    return _parse_overrides_payload(data)


def _read_overrides_or_raise() -> tuple[dict[str, dict], int]:
    """Like ``read_overrides`` but raises ``OverridesUnavailable`` instead
    of degrading to ``({}, 0)`` when the file exists yet can't be
    read/parsed. Only a genuinely missing file is legitimately empty. Used
    by ``write_override``/``delete_override``/``prune_stale_overrides`` so
    a transient EIO/permissions blip or corrupt file can't clobber the
    still-good on-disk contents (AHQ-014)."""
    try:
        check_trusted(OVERRIDES_FILE)
        raw = OVERRIDES_FILE.read_text()
    except FileNotFoundError:
        return {}, 0
    except OSError as exc:
        raise OverridesUnavailable(f"cannot read {OVERRIDES_FILE}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OverridesUnavailable(f"{OVERRIDES_FILE} corrupt: {exc}") from exc
    return _parse_overrides_payload(data)


def _persist(entries: dict[str, dict], revision: int) -> None:
    """Atomic .tmp-rename write of the v2 envelope. Caller must hold the flock.

    On any write/rename failure the temp file is removed before the
    error propagates — mirrors ``hooks._atomic_write`` so a disk-full /
    read-only blip can't leave an ``overrides.json.tmp`` behind.
    """
    ensure_dir(OVERRIDES_FILE.parent)
    tmp = OVERRIDES_FILE.with_suffix(".json.tmp")
    payload = {"revision": revision, "entries": entries}
    try:
        tmp.write_text(json.dumps(payload))
        tmp.rename(OVERRIDES_FILE)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def write_override(pane_id: str, entry: dict, *, session_id: str | None) -> None:
    """Insert/replace ``entry`` keyed by ``pane_id``. Atomic, cross-process safe.

    ``session_id`` is the writing agent's ``override_session_id`` (claude:
    bridge id; codex: thread UUID; ``None`` when absent); persisted as the
    ``session_id`` key on the entry so the resolver can apply the 5-state
    matrix on session match.
    """
    with file_lock(_lock_path_for(OVERRIDES_FILE)):
        entries, rev = _read_overrides_or_raise()
        entries[pane_id] = {**entry, "session_id": session_id}
        _persist(entries, rev + 1)


def delete_override(pane_id: str) -> bool:
    """Remove ``pane_id`` from overrides. Returns True if present."""
    with file_lock(_lock_path_for(OVERRIDES_FILE)):
        entries, rev = _read_overrides_or_raise()
        if pane_id not in entries:
            return False
        del entries[pane_id]
        _persist(entries, rev + 1)
        return True


def append_feedback(event: dict) -> None:
    """Append one JSONL line. Thread-safe; lines never interleave."""
    line = json.dumps(event) + "\n"
    with _FEEDBACK_LOCK:
        FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with FEEDBACK_FILE.open("a", encoding="utf-8") as f:
            f.write(line)


def make_feedback_event(
    *,
    pane_id: str,
    repo: str | None,
    branch: str | None,
    auto_phase: str | None,
    user_phase: str | None,
    action: str,
    note: str | None,
) -> dict:
    """Construct a feedback event with ISO-8601 UTC timestamp."""
    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pane_id": pane_id,
        "repo": repo,
        "branch": branch,
        "auto_phase": auto_phase,
        "user_phase": user_phase,
        "action": action,
        "note": note,
    }


def prune_stale_overrides(agents: list[Agent]) -> None:
    """Daemon-only: remove legacy entries and confirmed session mismatches.

    Uses the shared ``session_match_state`` matrix (``models.py`` — the
    same function the resolver's ``_feedback_from_entry`` uses for its
    apply decision, so the two never fork into separately-maintained
    matrices):

    | ``session_match_state`` result   | action                               |
    |-----------------------------------|---------------------------------------|
    | ``"match"``                       | keep                                  |
    | ``"ambiguous"`` (one-sided None)  | keep (transient bridge-id read       |
    |                                    | failure; for codex, kept regardless  |
    |                                    | since prune only acts on a definite  |
    |                                    | mismatch)                             |
    | ``"mismatch"``                    | PRUNE (the leak case from issue #65) |
    | no ``session_id`` key             | PRUNE (legacy migration)              |
    | orphan (no live agent)            | keep (out of scope for this prune)    |

    ``lenient_none`` is threaded per-agent-type for parity with the
    resolver, though it has no effect on the prune outcome here: prune
    only ever deletes on ``"mismatch"``, and lenience only changes
    whether a one-sided-None reads as ``"match"`` vs ``"ambiguous"`` —
    both are kept either way.

    Write-back (#205 Codex review): when a lenient match pairs a stored
    ``session_id`` of ``None`` (an override written before the codex
    thread UUID was captured) with an agent that HAS a captured id, the
    entry is stamped with that id. An unstamped ``None`` entry would
    otherwise match every future codex session reusing the pane forever;
    once bound, a later session's different UUID is a genuine
    ``"mismatch"`` and prunes normally.

    Wrapped in ``file_lock(OVERRIDES_LOCK)`` so concurrent
    ``write_override`` from a phase-route POST in another process is
    serialized — never clobbered by our prune pass.

    Phase application happens in the resolver. This function ONLY
    deletes entries that fail the matrix.
    """
    by_pane = {a.pane_id: a for a in agents}
    with file_lock(_lock_path_for(OVERRIDES_FILE)):
        entries, rev = _read_overrides_or_raise()
        stale: list[str] = []
        dirty = False
        for pane_id, entry in entries.items():
            agent = by_pane.get(pane_id)
            if agent is None:
                # Orphan: no live agent for this pane. Out of scope.
                continue
            if "session_id" not in entry:
                stale.append(pane_id)
                continue
            stored = entry.get("session_id")
            current = agent.override_session_id
            state = session_match_state(
                stored, current, lenient_none=agent.agent_type is AgentType.CODEX
            )
            if state == "mismatch":
                stale.append(pane_id)
                continue
            # "match" and "ambiguous" both: keep.
            if state == "match" and stored is None and current is not None:
                # Lenient (codex) match against a pre-capture entry: bind it
                # to this session's UUID so it can't outlive the session and
                # silently apply to a future pane occupant.
                entry["session_id"] = current
                dirty = True
        if not stale and not dirty:
            return
        for pane_id in stale:
            del entries[pane_id]
        _persist(entries, rev + 1)
