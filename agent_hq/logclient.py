"""Vendored write-side client for the unified logger (#231).

Adapted from the logger project's reference Python client (keep diffs
against it portable). ``log()``/``enqueue()``/
``flush()`` NEVER raise: a logger outage must not break any caller.

Three deliberate divergences from upstream, approved in the #231 spec:

1. Own queue file (``~/.cache/agent-hq/logger-queue.jsonl``) — an
   agent-hq bug can never wedge another client's queue, and the
   path joins the test-isolation contract (#133).
2. Missing write token ⇒ DISABLED: events are dropped, never queued.
   Upstream queues while awaiting a token; for a 2 s-tick daemon on an
   unconfigured machine that is unbounded growth.
3. Queue capped at ``MAX_QUEUE_EVENTS`` (500, drop-oldest — upstream caps
   at 2000 and quarantines the overflow to ``.rejected``). logger#9's "cap
   only when a call ends on a transient failure" applies to ``log()`` and
   ``flush()``: overflow is never dropped ahead of a delivery attempt.
   ``enqueue()`` makes no delivery attempt and nothing guarantees a later
   flush, so it still caps on every append.

logger#9 hardening (ported from upstream): a 400/422 naming ``item N:``
quarantines only that event; an unattributed reject or a 413 on a
multi-event chunk halves the chunk (own budget of ``MAX_SPLITS_PER_CALL``);
guaranteed rejects are repaired at build time (empty message, oversized
message/source/json_extra, scalar json_extra, NaN); and after a transient
failure ``log()`` calls within ``LOGGER_COOLDOWN_MS`` POST only their own
event. ``flush()`` — the daemon's deliberate drain — ignores the cooldown.

Env seams (tests/portability only): ``LOGGER_URL``,
``LOGGER_TOKEN_FILE``, ``LOGGER_QUEUE_FILE``, ``LOGGER_COOLDOWN_MS``
(in-process failure cooldown, default 60000).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = "https://logger.example.invalid"
MAX_MESSAGE_LEN = 8192
# Server limits are JS .length — UTF-16 code units, not Python code points.
MAX_JSON_EXTRA_LEN = 16384  # compact-serialized
MAX_SOURCE_LEN = 128
# The server rejects an empty message; a placeholder keeps the event.
EMPTY_MESSAGE = "(empty message)"
# An oversized json_extra is replaced by a marker carrying this much of its
# serialized form. Escaping at most doubles it, so the marker always fits.
JSON_EXTRA_PREVIEW_LEN = 4096
# Cloudflare's Browser Integrity Check bans the default Python-urllib UA at
# the edge (error 1010 -> 403) before the Worker runs; any custom UA passes.
USER_AGENT = "claude-logger/1.0"
TIMEOUT_S = 3
BATCH_MAX = 100  # server rejects arrays >100; replay chunks at this size
# Replay work per call is capped so the flock hold (and any caller stalled
# behind it) stays bounded — at worst (MAX_CHUNKS_PER_CALL +
# MAX_SPLITS_PER_CALL) * TIMEOUT_S.
MAX_CHUNKS_PER_CALL = 5
# Chunk halvings (413 / unattributed reject) allowed per call: enough to
# isolate one offender in a full chunk, ceil(log2(BATCH_MAX)).
MAX_SPLITS_PER_CALL = 7
MAX_QUEUE_EVENTS = 500

# In-process failure cooldown (logger#9): after a transient failure, log()
# calls within the window send only their own event, skipping the backlog
# read and replay. Mutated only under the queue flock.
_last_transient_at = float("-inf")
# Cooldown appends since the last full-path call; at MAX_QUEUE_EVENTS the
# next call takes the full path so the queue cap still applies.
_cooldown_appends = 0

_DEFAULT_TOKEN_FILE = Path(os.path.expanduser("~/.claude/secrets/logger-write.token"))
_DEFAULT_QUEUE_FILE = Path(os.path.expanduser("~/.cache/agent-hq/logger-queue.jsonl"))


def _url() -> str:
    return os.environ.get("LOGGER_URL", DEFAULT_URL).rstrip("/")


def _write_token_file() -> Path:
    env = os.environ.get("LOGGER_TOKEN_FILE")
    return Path(env) if env else _DEFAULT_TOKEN_FILE


def _queue_file() -> Path:
    env = os.environ.get("LOGGER_QUEUE_FILE")
    return Path(env) if env else _DEFAULT_QUEUE_FILE


def _cooldown_s() -> float:
    try:
        return float(os.environ.get("LOGGER_COOLDOWN_MS", "60000")) / 1000
    except ValueError:
        return 60.0


def _in_cooldown() -> bool:
    return time.monotonic() - _last_transient_at < _cooldown_s()


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _clip(text: str, max_units: int) -> str:
    """Clip to max UTF-16 code units (the server's measure) without ending on
    the high half of a surrogate pair, which would store as garbage."""
    data = text.encode("utf-16-le", "surrogatepass")
    cut = max_units * 2
    if len(data) <= cut:
        return text
    if 0xD800 <= int.from_bytes(data[cut - 2 : cut], "little") <= 0xDBFF:
        cut -= 2
    return data[:cut].decode("utf-16-le", "surrogatepass")


def _compact_json(value) -> str:
    # The shape the Worker re-serializes and measures (JSON.stringify).
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def enabled() -> bool:
    """Whether remote logging is configured on this machine: the token file
    exists and holds a non-blank token (#237 — a blank one would queue every
    event to the cap and pay a doomed replay on each ``log()``). A revoked
    token still reports enabled: 401/403 stay transient so rotation heals."""
    try:
        return bool(_read_token(_write_token_file()))
    except OSError:
        return False


def _read_token(path: Path) -> str:
    try:
        return path.read_text().strip()
    except UnicodeDecodeError as exc:
        # An OSError like any other unreadable token, so every caller's
        # transient handling (queue, don't drop) applies (#237).
        raise OSError(f"token file is not valid text: {exc}") from exc


def _quarantine(queue_file: Path, lines: list[str], reason: str) -> None:
    """Park permanently-undeliverable material in a .rejected sidecar.

    Nothing is silently dropped: corrupt queue lines and server-rejected
    chunks land here for manual recovery instead of wedging or vanishing.
    """
    sidecar = queue_file.with_name(queue_file.name + ".rejected")
    with sidecar.open("a") as f:
        for line in lines:
            f.write(line.rstrip("\n") + "\n")
    print(
        f"logger: {len(lines)} event(s) quarantined to {sidecar} ({reason})",
        file=sys.stderr,
    )


def _read_queue(queue_file: Path) -> list[dict]:
    if not queue_file.exists():
        return []
    events = []
    corrupt = []
    for line in queue_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            corrupt.append(line)
    if corrupt:
        _quarantine(queue_file, corrupt, "corrupt queue line")
        # Rewrite immediately (caller holds the lock) so a corrupt line is
        # quarantined exactly once.
        _write_queue(queue_file, events)
    return events


def _write_queue(queue_file: Path, events: list[dict]) -> None:
    """Atomic rewrite (temp file + rename): a crash mid-write must never
    destroy the queued events the old file held."""
    if not events:
        queue_file.unlink(missing_ok=True)
        return
    tmp = queue_file.with_name(queue_file.name + ".tmp")
    tmp.write_text("".join(json.dumps(e) + "\n" for e in events))
    os.replace(tmp, queue_file)


def _append_queue(queue_file: Path, event: dict, *, cap: bool = True) -> None:
    with queue_file.open("a") as f:
        f.write(json.dumps(event) + "\n")
    if cap:
        _enforce_cap(queue_file)


def _enforce_cap(queue_file: Path) -> None:
    """Drop-oldest cap. Text-level (not parsed) so a corrupt line can't
    block capping; caller holds the queue lock."""
    try:
        lines = queue_file.read_text().splitlines()
    except OSError:
        return
    if len(lines) <= MAX_QUEUE_EVENTS:
        return
    tmp = queue_file.with_name(queue_file.name + ".tmp")
    tmp.write_text("".join(line + "\n" for line in lines[-MAX_QUEUE_EVENTS:]))
    os.replace(tmp, queue_file)


def _post_events(events: list[dict], token: str) -> tuple[str, int | None]:
    """POST a batch to /log. Returns (status, item): status is "ok",
    "transient", "too_large" (413), or "rejected" (permanent — 400/422 or an
    unserializable payload); item is the batch index a rejection names."""
    try:
        data = json.dumps(events).encode()
    except (TypeError, ValueError):
        return "rejected", None
    request = urllib.request.Request(
        _url() + "/log",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return ("ok" if response.status == 200 else "transient"), None
    except urllib.error.HTTPError as error:
        # Validation-class codes mean the PAYLOAD can never deliver. Auth
        # (401/403) and everything else may heal (token rotation, server
        # hiccup) — those must keep the events queued, not quarantine them.
        # 413 is a request-size condition: the caller halves a multi-event
        # chunk and quarantines a single event.
        if error.code in (400, 422):
            return "rejected", _rejected_item(error)
        if error.code == 413:
            return "too_large", None
        return "transient", None
    except Exception:
        # Any other network-layer failure (e.g. a truncated response raising
        # http.client.HTTPException) must still queue the event rather than
        # let it propagate out and be silently lost by the caller's
        # catch-all — "nothing silently dropped" is the module's contract.
        return "transient", None


def _rejected_item(error: urllib.error.HTTPError) -> int | None:
    """The failing index from a batch error body ({"error": "item N: ..."})."""
    try:
        match = re.match(r"item (\d+): ", str(json.loads(error.read())["error"]))
    except Exception:
        return None
    return int(match.group(1)) if match else None


def _build_event(
    app: str,
    level: str,
    message: str,
    json_extra: dict | None = None,
    source: str | None = None,
) -> dict:
    text = _clip(str(message), MAX_MESSAGE_LEN)
    event = {
        "app": app,
        "level": level,
        "message": text or EMPTY_MESSAGE,
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": _clip(str(source or socket.gethostname()), MAX_SOURCE_LEN),
    }
    if json_extra is not None:
        event["json_extra"] = _repair_json_extra(json_extra)
    return event


def _repair_json_extra(json_extra):
    """Repair a json_extra the server would reject (it must be an object/array
    of at most MAX_JSON_EXTRA_LEN serialized chars) instead of letting it
    quarantine the whole event. Returns the parsed snapshot, so what's
    validated here is exactly what's sent."""
    try:
        serialized = _compact_json(json_extra)
    except (TypeError, ValueError):
        # A caller's unserializable extra (or NaN, which would make the whole
        # batch invalid JSON) must not lose the event.
        return {"unserializable": str(json_extra)[:1000]}
    if not serialized.startswith(("{", "[")):
        serialized = _compact_json({"value": json_extra})
    if _utf16_len(serialized) > MAX_JSON_EXTRA_LEN:
        return {
            "truncated": True,
            "original_length": _utf16_len(serialized),
            "preview": _clip(serialized, JSON_EXTRA_PREVIEW_LEN),
        }
    return json.loads(serialized)


def log(
    app: str,
    level: str,
    message: str,
    json_extra: dict | None = None,
    source: str | None = None,
) -> bool:
    """Best-effort push. True=delivered, False=queued/dropped/disabled.

    Previously queued events replay FIRST (oldest first, original ts
    preserved) so an outage's events still reach D1 after recovery —
    except during the failure cooldown, when this call's event is sent
    alone and the backlog waits for the next full-path call or flush().
    """
    try:
        if not enabled():
            return False
        event = _build_event(app, level, message, json_extra, source)
        queue_file = _queue_file()
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        with open(_lock_path(queue_file), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _deliver_or_queue(queue_file, event)
    except Exception:
        return False


def enqueue(
    app: str,
    level: str,
    message: str,
    json_extra: dict | None = None,
    source: str | None = None,
) -> bool:
    """Local queue append, no network. For hot paths (hook binary).

    Not O(1): the cap check re-reads the queue file on every append, bounded
    by ``MAX_QUEUE_EVENTS`` (#237).

    True=queued (drained by a later ``log()``/``flush()``), False=dropped.

    Takes the queue lock NON-BLOCKING: ``log()``/``flush()`` can hold it for
    up to ``(MAX_CHUNKS_PER_CALL + MAX_SPLITS_PER_CALL) * TIMEOUT_S`` (~36s)
    while replaying against a down or rejecting endpoint, and ``enqueue()``
    exists precisely for the hook binary's hot path — it must never wait on
    that lock. If the lock is contended the event is DROPPED (hook latency
    beats telemetry durability), not queued. It caps the queue (drop-oldest)
    on every append: it makes no delivery attempt, and nothing guarantees a
    flush.
    """
    try:
        if not enabled():
            return False
        event = _build_event(app, level, message, json_extra, source)
        queue_file = _queue_file()
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        with open(_lock_path(queue_file), "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return False
            _append_queue(queue_file, event)
        return True
    except Exception:
        return False


def flush() -> bool:
    """Replay queued events without adding a new one. Never raises.

    True iff the queue is empty afterwards (including the was-empty case).
    Ignores the failure cooldown: this is the deliberate backlog drain.
    """
    try:
        if not enabled():
            return False
        queue_file = _queue_file()
        if not queue_file.exists():
            return True
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        with open(_lock_path(queue_file), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _deliver_or_queue(queue_file, None)
        return not queue_file.exists()
    except Exception:
        return False


def _lock_path(queue_file: Path) -> Path:
    return queue_file.with_name(queue_file.name + ".lock")


def _reject_reason(status: str) -> str:
    if status == "too_large":
        return "server rejected event as too large"
    return "server rejected event as malformed"


def _deliver_alone(queue_file: Path, event: dict) -> bool:
    """Cooldown path: a recent transient failure is likely still in force, so
    skip the backlog read and replay (the per-call cost a persistent failure
    would otherwise grow) but still try this call's event."""
    global _last_transient_at, _cooldown_appends
    try:
        status, _ = _post_events([event], _read_token(_write_token_file()))
    except OSError:
        status = "transient"
    if status == "transient":
        _last_transient_at = time.monotonic()
        _cooldown_appends += 1
        # O(1): no read-back to cap; the counter bounds the growth.
        _append_queue(queue_file, event, cap=False)
        return False
    # Any non-transient answer (ok or a permanent reject) proves the server
    # is reachable: the next call replays the backlog.
    _last_transient_at = float("-inf")
    if status == "ok":
        return True
    _quarantine(queue_file, [json.dumps(event)], _reject_reason(status))
    return False


def _deliver_or_queue(queue_file: Path, event: dict | None) -> bool:
    """Called under the queue lock: replay queued events (+ ``event`` if
    given), chunked. Returns True iff the server accepted ``event``.

    Progress persists after EVERY delivered chunk, the no-progress failure
    path is an O(1) append (not a full rewrite), and a permanently-rejected
    event is quarantined — alone, not with its chunk — so it can't wedge the
    queue head. Work per call is capped at MAX_CHUNKS_PER_CALL (+
    MAX_SPLITS_PER_CALL halvings). ``event=None`` (``flush()``) never takes
    the cooldown path.
    """
    global _last_transient_at, _cooldown_appends
    if event is not None and _in_cooldown() and _cooldown_appends < MAX_QUEUE_EVENTS:
        return _deliver_alone(queue_file, event)
    _cooldown_appends = 0
    queued = _read_queue(queue_file)
    pending = queued + ([event] if event is not None else [])
    if not pending:
        return event is None
    # Until the first rewrite the on-disk queue holds exactly ``queued``, so a
    # failure only needs to append the new event.
    persisted = False

    def fail_transient(rest: list[dict]) -> bool:
        """Persist ``rest`` (the unsent tail) after a transient failure. The
        queue cap (drop-oldest, divergence 3) applies only here: overflow a
        healthy server could still take is never dropped ahead of a
        delivery attempt."""
        global _last_transient_at
        _last_transient_at = time.monotonic()
        if len(rest) > MAX_QUEUE_EVENTS:
            _write_queue(queue_file, rest[-MAX_QUEUE_EVENTS:])
        elif not persisted and event is not None:
            _append_queue(queue_file, event, cap=False)
        # else: the last rewrite (or nothing, for flush) already persisted it
        return False

    try:
        token = _read_token(_write_token_file())
    except OSError:
        return fail_transient(pending)

    event_accepted = False
    index = 0
    chunks_sent = 0
    # A 413 or an unattributed reject halves the chunk; isolating an offender
    # resets it to full size. Halvings have their own per-call budget instead
    # of counting against MAX_CHUNKS_PER_CALL.
    size = BATCH_MAX
    splits = 0
    while index < len(pending):
        if chunks_sent >= MAX_CHUNKS_PER_CALL:
            _write_queue(queue_file, pending[index:])
            return event_accepted
        chunk = pending[index : index + size]
        status, item = _post_events(chunk, token)
        if status == "transient":
            return fail_transient(pending[index:])
        # Any non-transient answer proves the server reachable: it clears the
        # cooldown (matters for flush(), which runs inside the window).
        _last_transient_at = float("-inf")
        if status == "ok":
            if event is not None and any(e is event for e in chunk):
                event_accepted = True
            index += len(chunk)
        else:
            if len(chunk) == 1:
                offender = 0
            elif status == "rejected" and item is not None and item < len(chunk):
                offender = item
            else:
                if splits >= MAX_SPLITS_PER_CALL:
                    # Budget spent: the next call isolates the next offender.
                    _write_queue(queue_file, pending[index:])
                    return event_accepted
                splits += 1
                size = (len(chunk) + 1) // 2
                continue
            # Only the offender leaves the queue: the server writes nothing
            # from a rejected batch, so the rest of the chunk is retried.
            _quarantine(queue_file, [json.dumps(chunk[offender])], _reject_reason(status))
            pending.pop(index + offender)
            size = BATCH_MAX
        chunks_sent += 1
        _write_queue(queue_file, pending[index:])
        persisted = True
    return event_accepted
