"""The remote write-action log (#397): every write Claude Duet makes, locally.

One JSON line per event in ``<remote STATE_DIR>/actions.jsonl`` (dir 700,
file 600, like ``tokens.json``): an ``attempt`` line written BEFORE the action
runs, then a ``result`` line. The attempt is fail-closed: when it can't be
written, the caller refuses the action, so no remote write ever happens
without a record. The result is best-effort (the action already happened).

The log stays on the Mac. What reaches the unified logger is a short fixed
code per action (``remote/tools.py``), never this file's outcome text.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from agent_hq._locking import file_lock

from . import config as remote_config

log = logging.getLogger(__name__)

# Trim to the newest MAX_LINES once the file passes TRIM_AT, so a busy month
# can't grow it without bound (writes are human-scale: a rewrite is cheap).
MAX_LINES = 2000
TRIM_AT = 2500


class ActionLog:
    def __init__(self, path: Path | None = None) -> None:
        # Resolved per instance so the conftest STATE_DIR redirect applies.
        self.path = path if path is not None else remote_config.STATE_DIR / "actions.jsonl"

    def _lock(self):
        return file_lock(self.path.with_name(self.path.name + ".lock"))

    def _append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock():
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            self._trim()

    def _trim(self) -> None:
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        if len(lines) <= TRIM_AT:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines[-MAX_LINES:])
        os.replace(tmp, self.path)

    def attempt(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        now: float | None = None,
        client_id: str | None = None,
    ) -> str:
        """Record an attempt; returns its action id. Raises ``OSError`` when
        the line can't be written: the caller must then refuse the action."""
        action_id = uuid.uuid4().hex[:12]
        record: dict[str, Any] = {
            "ts": time.time() if now is None else now,
            "phase": "attempt",
            "action_id": action_id,
            "tool": tool,
            "args": args,
        }
        if client_id:
            record["client_id"] = client_id
        self._append(record)
        return action_id

    def result(
        self,
        action_id: str,
        tool: str,
        *,
        ok: bool,
        code: str,
        outcome: str = "",
        now: float | None = None,
    ) -> None:
        """Record how an attempt ended. Never raises."""
        try:
            self._append(
                {
                    "ts": time.time() if now is None else now,
                    "phase": "result",
                    "action_id": action_id,
                    "tool": tool,
                    "ok": ok,
                    "code": code,
                    "outcome": outcome,
                }
            )
        except OSError as exc:
            log.warning("cannot record remote action result %s: %s", action_id, exc)

    def recent_attempts(self, tool: str, *, since: float) -> list[dict[str, Any]]:
        """Attempt records for ``tool`` at or after ``since``, oldest first,
        each with ``result_code``: its result line's code, or None while no
        result has been written.

        A missing file is empty; an unreadable one raises ``OSError`` (a
        duplicate guard that can't see the log must refuse, not pass).
        Malformed lines are skipped."""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        attempts: list[dict[str, Any]] = []
        codes: dict[str, str] = {}
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("tool") != tool:
                continue
            if rec.get("phase") == "result" and isinstance(rec.get("code"), str):
                codes[str(rec.get("action_id"))] = rec["code"]
            elif (
                rec.get("phase") == "attempt"
                and isinstance(rec.get("ts"), (int, float))
                and rec["ts"] >= since
                and isinstance(rec.get("args"), dict)
            ):
                attempts.append(rec)
        return [{**rec, "result_code": codes.get(str(rec.get("action_id")))} for rec in attempts]
