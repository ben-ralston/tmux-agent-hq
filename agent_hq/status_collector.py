"""Fast native-status collection and the shared fail-closed live overlay."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

from .codex_runtime import _alive, read_registrations
from .models import Agent, AgentState, AgentType
from .runtime_dir import check_trusted, ensure_dir
from .runtime_status import QUERY_TIMEOUT, read_claude_statuses, read_codex_status
from .status_types import LiveStatus, unknown_status
from .waiting_clock import WaitingClock

STATUS_FILE = Path("/tmp/agent-hq/runtime-status.json")
MAX_STATUS_AGE = 1.5
SCHEMA_VERSION = 1
log = logging.getLogger(__name__)


def _fresh(wall, mono, now, now_ns) -> bool:
    try:
        return (
            type(wall) in (int, float)
            and math.isfinite(wall)
            and type(mono) is int
            and 0 <= now - wall <= MAX_STATUS_AGE
            and 0 <= now_ns - mono <= int(MAX_STATUS_AGE * 1_000_000_000)
        )
    except (OverflowError, TypeError):
        return False


class RuntimeStatusCollector:
    """Own a background event loop; each source publishes as soon as it completes."""

    def __init__(
        self,
        path=None,
        *,
        interval=0.5,
        claude_reader=None,
        codex_reader=None,
        registrations_reader=None,
        clock=None,
        monotonic_ns=None,
        pid_alive=None,
    ):
        self.path = Path(path) if path is not None else STATUS_FILE
        self._waiting = WaitingClock(self.path.with_name("waiting-since.json"), alive=pid_alive)
        self.interval = interval
        self.claude_reader = claude_reader or read_claude_statuses
        self.codex_reader = codex_reader or read_codex_status
        self.registrations_reader = registrations_reader or read_registrations
        self.clock = clock or time.time
        self.monotonic_ns = monotonic_ns or time.monotonic_ns
        self._rows = {}
        self._errors = {}
        self._stop = threading.Event()
        self._thread = None
        # Poll until discovery supplies presence, including standalone collectors.
        self._claude_present = threading.Event()
        self._claude_present.set()

    def set_claude_present(self, present: bool) -> None:
        """Gate native queries using the daemon's latest process discovery."""
        if present:
            self._claude_present.set()
        else:
            self._claude_present.clear()

    def _publish(self):
        self._waiting.save()
        data = {
            "version": SCHEMA_VERSION,
            "heartbeat_at": self.clock(),
            "heartbeat_ns": self.monotonic_ns(),
            "observations": [row for rows in self._rows.values() for row in rows],
            "source_errors": dict(self._errors),
        }
        ensure_dir(self.path.parent)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.path.parent, prefix=".runtime-status-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, allow_nan=False)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def _claude(self):
        if not self._claude_present.is_set():
            self._rows.pop("claude", None)
            self._errors.pop("claude", None)
            self._publish()
            return
        try:
            statuses = await asyncio.wait_for(asyncio.to_thread(self.claude_reader), QUERY_TIMEOUT)
            if not isinstance(statuses, list) or any(
                not isinstance(s, LiveStatus) for s in statuses
            ):
                raise ValueError("Malformed Claude observations")
            self._rows["claude"] = [
                {"status": self._waiting.stamp(s).to_dict()} for s in statuses
            ]
            self._errors.pop("claude", None)
        except Exception as exc:
            self._rows.pop("claude", None)
            self._errors["claude"] = f"Claude runtime unavailable: {exc}"
        self._publish()

    async def _codex_one(self, receipt, key):
        try:
            status = await asyncio.wait_for(
                self.codex_reader(
                    receipt["socket_path"], receipt["thread_id"], receipt["frontend_pid"]
                ),
                QUERY_TIMEOUT,
            )
            if not isinstance(status, LiveStatus):
                raise ValueError("Malformed Codex observation")
        except Exception as exc:
            status = unknown_status(
                f"Codex runtime unavailable: {exc}",
                source="codex",
                session_id=receipt["thread_id"],
                pid=receipt["frontend_pid"],
                observed_at=self.clock(),
                observed_ns=self.monotonic_ns(),
            )
        self._rows[key] = [
            {
                "status": self._waiting.stamp(status).to_dict(),
                "pane_id": receipt["pane_id"],
                "generation": receipt["generation"],
                "socket_path": receipt["socket_path"],
            }
        ]
        self._publish()

    async def _codex(self):
        try:
            receipts = await asyncio.wait_for(
                asyncio.to_thread(self.registrations_reader), QUERY_TIMEOUT
            )
            if not isinstance(receipts, list):
                raise ValueError("Malformed Codex registrations")
            valid = [r for r in receipts if _receipt_valid(r)]
            keys = {self._key(r) for r in valid}
            for key in list(self._rows):
                if key != "claude" and key not in keys:
                    del self._rows[key]
            self._errors.pop("codex", None)
        except Exception as exc:
            for key in list(self._rows):
                if key != "claude":
                    del self._rows[key]
            self._errors["codex"] = f"Codex registration unavailable: {exc}"
            self._publish()
            return
        self._publish()
        counts = Counter((r["pane_id"], r["frontend_pid"]) for r in valid)
        sessions = Counter(r["thread_id"] for r in valid)
        jobs = []
        for receipt in valid:
            key = self._key(receipt)
            if (
                counts[(receipt["pane_id"], receipt["frontend_pid"])] > 1
                or sessions[receipt["thread_id"]] > 1
            ):
                self._rows.pop(key, None)
                self._errors["codex"] = "Ambiguous Codex registration identity"
                continue
            jobs.append(self._codex_one(receipt, key))
        self._publish()
        await asyncio.gather(*jobs)

    @staticmethod
    def _key(receipt):
        return (
            receipt["pane_id"],
            receipt["frontend_pid"],
            receipt["generation"],
            receipt["thread_id"],
            receipt["socket_path"],
        )

    async def collect_once(self):
        await asyncio.gather(self._claude(), self._codex())

    async def _repeat(self, callback):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await callback()
            except Exception:
                log.exception("Runtime status snapshot write failed")
            deadline = started + self.interval
            while not self._stop.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))

    async def _run(self):
        await asyncio.gather(self._repeat(self._claude), self._repeat(self._codex))
        self._rows.clear()
        self._errors = {"claude": "Runtime collector stopped", "codex": "Runtime collector stopped"}
        self._publish()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._rows.clear()
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._run()), name="runtime-status", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.5)


def _receipt_valid(receipt):
    return (
        isinstance(receipt, dict)
        and type(receipt.get("frontend_pid")) is int
        and receipt["frontend_pid"] > 0
        and all(
            isinstance(receipt.get(key), str) and receipt[key]
            for key in ("pane_id", "thread_id", "generation", "socket_path")
        )
    )


def _read_snapshot(path, now, now_ns):
    try:
        check_trusted(path)
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            raise ValueError("Unsupported runtime snapshot")
        if not _fresh(data.get("heartbeat_at"), data.get("heartbeat_ns"), now, now_ns):
            raise ValueError("Runtime collector heartbeat is stale or invalid")
        if not isinstance(data.get("observations"), list):
            raise ValueError("Malformed runtime observations")
        if not isinstance(data.get("source_errors"), dict):
            raise ValueError("Malformed runtime source errors")
        return data, ""
    except (OSError, ValueError, TypeError) as exc:
        return None, f"Runtime status unavailable: {exc}"


def apply_live_status(
    agents: list[Agent],
    *,
    path=None,
    now=None,
    now_ns=None,
    registrations=None,
    process_started_at=None,
) -> list[Agent]:
    """Always overwrite cached live state, including when the source is unavailable."""
    now = time.time() if now is None else now
    now_ns = time.monotonic_ns() if now_ns is None else now_ns
    data, error = _read_snapshot(Path(path) if path is not None else STATUS_FILE, now, now_ns)
    if process_started_at is None:
        # Generation validation belongs to collection. Live reads only check
        # liveness; the native observation expires within 1.5 seconds.
        def process_started_at(pid):
            return 0 if pid and _alive(pid) else None
    if registrations is None and any(a.agent_type is AgentType.CODEX for a in agents):
        try:
            registrations = read_registrations(alive=_alive)
        except Exception:
            registrations = []
    receipts = [r for r in (registrations or []) if _receipt_valid(r)]
    rows = []
    if data is not None:
        for row in data["observations"]:
            if isinstance(row, dict):
                rows.append((row, LiveStatus.from_dict(row.get("status"))))
    agent_pids = Counter((a.agent_type, a.agent_pid) for a in agents if a.agent_pid)
    for agent in agents:
        source = agent.agent_type.value
        sid = (
            agent.claude_transcript_session_id
            if agent.agent_type is AgentType.CLAUDE
            else agent.codex_thread_id
            if agent.agent_type is AgentType.CODEX
            else None
        )
        reason = error or "No matching native runtime observation"
        selected = None
        if data is not None:
            source_error = data["source_errors"].get(source)
            if isinstance(source_error, str) and source_error:
                reason = source_error
        matching = [(row, s) for row, s in rows if s.source == source and s.pid == agent.agent_pid]
        registration = None
        if agent.agent_type is AgentType.CODEX:
            matches = [
                r
                for r in receipts
                if r["pane_id"] == agent.pane_id or r["frontend_pid"] == agent.agent_pid
            ]
            if (
                len(matches) == 1
                and matches[0]["frontend_pid"] == agent.agent_pid
                and matches[0]["pane_id"] == agent.pane_id
            ):
                registration = matches[0]
                sid = registration["thread_id"]
                agent.codex_thread_id = sid
                agent.codex_rollout_path = registration.get("transcript_path")
            else:
                reason = (
                    "Ambiguous Codex registration identity"
                    if len(matches) > 1
                    else "Codex runtime is not connected"
                )
            matching = [
                (row, s)
                for row, s in matching
                if registration is not None
                and row.get("pane_id") == agent.pane_id
                and row.get("generation") == registration["generation"]
                and row.get("socket_path") == registration["socket_path"]
            ]
        same_session = [s for _, s in rows if s.source == source and s.session_id == sid]
        if (
            len(matching) > 1
            or (sid and len(same_session) > 1)
            or agent_pids[(agent.agent_type, agent.agent_pid)] > 1
        ):
            reason = "Ambiguous native process/session identity"
        elif len(matching) == 1 and matching[0][1].session_id == sid and sid:
            candidate = matching[0][1]
            if not _fresh(candidate.observed_at, candidate.observed_ns, now, now_ns):
                reason = "Native runtime observation is stale or invalid"
            else:
                try:
                    birth = process_started_at(agent.agent_pid)
                    valid_birth = (
                        type(birth) in (int, float)
                        and math.isfinite(birth)
                        and 0 <= birth <= candidate.observed_at
                    )
                except Exception:
                    valid_birth = False
                if not valid_birth:
                    reason = "Native process generation cannot be verified"
                else:
                    selected = candidate
        if selected is None:
            selected = unknown_status(
                reason,
                source=source if source in ("claude", "codex") else "none",
                session_id=sid,
                pid=agent.agent_pid,
                observed_at=now,
                observed_ns=now_ns,
            )
        agent.status_snapshot = selected
        agent.state = AgentState(selected.state)
    return agents
