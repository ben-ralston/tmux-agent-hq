"""Read-only Claude and Codex runtime sources with bounded, injectable I/O."""

from __future__ import annotations

import asyncio
import calendar
import json
import os
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from websockets.asyncio.client import unix_connect

from agent_hq import eventlog
from agent_hq.status_types import LiveStatus, unknown_status

QUERY_TIMEOUT = 0.75
CLAUDE_QUERY = ["claude", "agents", "--settings", '{"disableAgentView":false}', "--json"]
# Per-session registry `claude agents --json` itself reads (#289). Undocumented,
# so every record must declare the peer protocol this reader was written against.
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
REGISTRY_PROTOCOL = 1


def _timeout(value: float) -> float:
    if not 0 < value <= QUERY_TIMEOUT:
        raise ValueError(f"Runtime query timeout must be between zero and {QUERY_TIMEOUT}s")
    return value


# Reason used when Claude reports "waiting" without a usable waitingFor.
# Not native data — consumers that surface waitingFor verbatim (the MCP
# list_agents ``waiting_for`` field, #287) must not echo it as such.
CLAUDE_WAITING_FALLBACK = "Claude is waiting for input"


def _claude_waiting_start(entry: dict, observed_at: float) -> float | None:
    """The registry's own record of the status change, epoch ms (#415).

    Undocumented and absent from the CLI fallback, so anything unusable is None
    and WaitingClock starts the wait at its first observation instead.
    """
    value = entry.get("statusUpdatedAt")
    if type(value) not in (int, float):
        return None
    try:
        seconds = value / 1000
    except OverflowError:
        return None
    return seconds if 0 < seconds <= observed_at else None


def parse_claude_statuses(payload: object) -> list[LiveStatus]:
    """Ignore background records; ambiguous live PIDs or sessions fail closed."""
    if not isinstance(payload, list):
        raise ValueError("Claude agents response is not a list")
    identified = [entry for entry in payload if isinstance(entry, dict)
                  and type(entry.get("pid")) is int and entry["pid"] > 0
                  and isinstance(entry.get("sessionId"), str) and entry["sessionId"].strip()]
    pids = Counter(entry["pid"] for entry in identified)
    sessions = Counter(entry["sessionId"] for entry in identified)
    observed_at, observed_ns = time.time(), time.monotonic_ns()
    statuses = []
    for entry in identified:
        native = entry.get("status")
        if not isinstance(native, str) or not native.strip():
            continue
        pid, session_id = entry["pid"], entry["sessionId"]
        identity = dict(source="claude", session_id=session_id, pid=pid, native_value=native,
                        observed_at=observed_at, observed_ns=observed_ns)
        if pids[pid] > 1 or sessions[session_id] > 1:
            statuses.append(unknown_status("Ambiguous Claude process/session identity", **identity))
            continue
        state = {"busy": "running", "waiting": "waiting", "idle": "idle"}.get(native)
        if state is None:
            statuses.append(unknown_status(f"Unsupported Claude status: {native}", **identity))
            continue
        reason = ""
        waiting_since = None
        if state == "waiting":
            waiting_since = _claude_waiting_start(entry, observed_at)
            waiting_for = entry.get("waitingFor")
            reason = (waiting_for if isinstance(waiting_for, str) and waiting_for.strip()
                      else CLAUDE_WAITING_FALLBACK)
        statuses.append(LiveStatus(state, native, state.title(), reason, "claude", session_id,
                                   pid, observed_at, observed_ns, waiting_since))
    return statuses


class RegistryUnavailable(Exception):
    """The session registry cannot be trusted this cycle; use the CLI instead."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, OverflowError):
        return False


# `procStart` is ps `lstart` rendered in UTC (1 s resolution; measured: local-time
# parsing is off by the UTC offset); births come from `etime`.
PROC_START_TOLERANCE = 2.0


def _proc_start(record: dict) -> float | None:
    try:
        return calendar.timegm(time.strptime(record["procStart"], "%a %b %d %H:%M:%S %Y"))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _drop_reused_pids(records: list[dict], births: Callable[[], dict]) -> list[dict]:
    """A crashed session's file survives; once its pid is reused the record looks
    live, and if that session was resumed elsewhere the shared sessionId marks the
    real one ambiguous. Only then is the process table worth a `ps` read: keep the
    records whose recorded start matches the pid's current birth. An unverifiable
    record is kept, so the ambiguity fails closed downstream."""
    sessions = Counter(r.get("sessionId") for r in records)
    if all(count == 1 for count in sessions.values()):
        return records
    born = births()
    kept = []
    for record in records:
        started, birth = _proc_start(record), born.get(record["pid"])
        if (sessions[record.get("sessionId")] > 1 and started is not None
                and birth is not None and abs(birth - started) > PROC_START_TOLERANCE):
            continue
        kept.append(record)
    return kept


def read_claude_registry(sessions_dir: Path | None = None, *,
                         alive: Callable[[int], bool] | None = None,
                         births: Callable[[], dict] | None = None) -> list[dict]:
    """Return live sessions' registry records, or raise rather than guess.

    Liveness is judged from the `<pid>.json` filename before the file is parsed,
    so a crashed session's leftover (possibly truncated) file is skipped and only
    a live session's record can force the fallback.
    """
    directory = CLAUDE_SESSIONS_DIR if sessions_dir is None else sessions_dir
    is_alive = alive or _pid_alive
    try:
        # scandir, not glob: glob swallows a listing error and yields nothing.
        with os.scandir(directory) as entries:
            names = sorted(e.name for e in entries if e.name.endswith(".json"))
        records = []
        for name in names:
            stem = name[:-len(".json")]
            if not stem.isdigit() or int(stem) <= 0 or not is_alive(int(stem)):
                continue
            try:
                record = json.loads((directory / name).read_text())
            except FileNotFoundError:
                continue  # session exited between listing and reading
            if not isinstance(record, dict) or record.get("pid") != int(stem):
                raise RegistryUnavailable(f"Malformed registry record {name}")
            protocol = record.get("peerProtocol")
            if type(protocol) is not int or protocol != REGISTRY_PROTOCOL:
                raise RegistryUnavailable(f"Unsupported registry protocol {protocol!r}")
            if not isinstance(record.get("status"), str):
                raise RegistryUnavailable(f"Registry record {name} has no status")
            session = record.get("sessionId")
            if not isinstance(session, str) or not session.strip():
                raise RegistryUnavailable(f"Registry record {name} has no sessionId")
            records.append(record)
    except (OSError, ValueError) as exc:
        raise RegistryUnavailable(f"Unreadable session registry: {exc}") from exc
    if births is None:
        from agent_hq.process_identity import read_process_births as births
    return _drop_reused_pids(records, births)


def read_claude_statuses(*, runner: Callable | None = None,
                        timeout: float = QUERY_TIMEOUT,
                        sessions_dir: Path | None = None,
                        alive: Callable[[int], bool] | None = None) -> list[LiveStatus]:
    """Read the session registry; fall back to the CLI when it can't be trusted.

    Command, timeout, and envelope errors from the fallback reach the collector.
    """
    try:
        return parse_claude_statuses(read_claude_registry(sessions_dir, alive=alive))
    except RegistryUnavailable as exc:
        eventlog.emit("warn", "claude status registry unavailable, using cli",
                      {"reason": str(exc)})
    run = subprocess.run if runner is None else runner
    result = run(CLAUDE_QUERY.copy(), capture_output=True, text=True, check=True,
                 timeout=_timeout(timeout))
    return parse_claude_statuses(json.loads(result.stdout))


def map_codex_status(native: object, thread_id: str, pid: int) -> LiveStatus:
    """Map the app-server ThreadStatus union, rejecting unknown active flags."""
    observed_at, observed_ns = time.time(), time.monotonic_ns()
    kind = native.get("type") if isinstance(native, dict) else None
    identity = dict(source="codex", session_id=thread_id, pid=pid,
                    native_value=kind if isinstance(kind, str) else "",
                    observed_at=observed_at, observed_ns=observed_ns)
    if kind == "idle":
        state, reason = "idle", ""
    elif kind == "active":
        flags = native.get("activeFlags")
        if not isinstance(flags, list) or any(
            flag not in ("waitingOnApproval", "waitingOnUserInput") for flag in flags
        ):
            return unknown_status("Unsupported or malformed Codex active flags", **identity)
        state = "waiting" if flags else "running"
        reasons = []
        if "waitingOnApproval" in flags:
            reasons.append("Waiting for approval")
        if "waitingOnUserInput" in flags:
            reasons.append("Waiting for user input")
        reason = "; ".join(reasons)
    else:
        reasons = {"notLoaded": "Codex thread is not loaded",
                   "systemError": "Codex runtime reported a system error"}
        reason = (reasons.get(kind, f"Unsupported Codex status: {kind}")
                  if isinstance(kind, str) else "Malformed Codex status")
        return unknown_status(reason, **identity)
    return LiveStatus(state, kind, state.title(), reason, "codex", thread_id, pid,
                      observed_at, observed_ns)


async def _response(socket, request_id: int) -> dict:
    """Ignore unrelated traffic, never answer server requests (including approvals)."""
    while True:
        message = json.loads(await socket.recv())
        if not isinstance(message, dict):
            raise ValueError("Malformed Codex protocol message")
        if "method" in message:
            continue
        if type(message.get("id")) is not int or message["id"] != request_id:
            continue
        if "error" in message or not isinstance(message.get("result"), dict):
            raise ValueError("Codex runtime request failed or returned a malformed result")
        return message["result"]


async def read_codex_status(
    socket_path: str,
    thread_id: str,
    pid: int,
    *,
    connect: Callable | None = None,
    timeout: float = QUERY_TIMEOUT,
) -> LiveStatus:
    """Initialize a read-only connection and query only the selected root thread.

    Transport and protocol exceptions propagate so the collector invalidates this
    source. A mismatched identity produces an explicitly unusable observation.
    """
    timeout = _timeout(timeout)
    connector = unix_connect if connect is None else connect

    async def query():
        # Codex 0.154 rejects the default permessage-deflate extension offer.
        async with connector(socket_path, uri="ws://localhost/rpc", compression=None,
                             open_timeout=timeout, close_timeout=0.05) as socket:
            await socket.send(json.dumps({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "agent-hq-status", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            }}))
            await _response(socket, 1)
            await socket.send(json.dumps({"method": "initialized"}))
            await socket.send(json.dumps({"id": 2, "method": "thread/read", "params": {
                "threadId": thread_id, "includeTurns": False,
            }}))
            result = await _response(socket, 2)
            thread = result.get("thread")
            if not isinstance(thread, dict):
                raise ValueError("Malformed Codex thread/read result")
            if thread.get("id") != thread_id:
                return unknown_status("Codex thread identity mismatch", source="codex",
                                      session_id=thread_id, pid=pid)
            return map_codex_status(thread.get("status"), thread_id, pid)

    return await asyncio.wait_for(query(), timeout=timeout)
