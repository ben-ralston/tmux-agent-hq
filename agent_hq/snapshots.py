"""Durable rolling snapshot of the live-agent set, for crash recovery. #125

The daemon's poll loop already computes the full live-agent set every tick
(session id, cwd, repo, branch, state, phase) but historically persisted
nothing durable — its caches live in ``/tmp`` (wiped on reboot). After a tmux
crash there was no record of *which agents were live and how to resume each*.

``SnapshotStore`` appends a compact, deduped record of that set to
``~/.config/agent-hq/agent-snapshots.jsonl`` (outside ``/tmp``, survives a
reboot). One JSONL line per accepted snapshot:

    {"timestamp": 1749068580.1, "agents": [ {<compact per-agent record>}, ... ]}

A new line is written only when the captured fields change (roster OR any
agent's state / phase / branch / ...), so an idle roster doesn't churn the
file. Growth is bounded to the last ``max_records`` lines.

Mirrors ``pinned_store`` conventions: ``__init__(path or _DEFAULT)``,
tolerant reads, a flock (``_locking.file_lock``) around the read-modify-write,
and an atomic temp-file rename so a concurrent reader never sees a half-written
file. ``append_if_changed`` swallows its own I/O errors (it runs inside the
daemon's hot loop and must never wedge it).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from agent_hq import eventlog
from agent_hq._locking import file_lock
from agent_hq.models import Agent

log = logging.getLogger(__name__)

_DEFAULT_SNAPSHOTS_FILE = Path.home() / ".config" / "agent-hq" / "agent-snapshots.jsonl"
MAX_RECORDS = 1000

# Typed dispatch branches encode their issue number(s) as ``<prefix>/<n>[-<n>...]-slug``.
# Generalizes the worktree-scan regex in ``github.py`` to all typed prefixes.
_BRANCH_ISSUE_RE = re.compile(r"^(?:feature|bugfix|chore|docs)/(\d+(?:-\d+)*)-")


def issue_numbers_from_branch(branch: str | None) -> list[int]:
    """Issue number(s) encoded in a typed dispatch branch name, in order.

    ``feature/125-foo`` → ``[125]``; ``feature/12-34-foo`` → ``[12, 34]``.
    A plain branch (``main``) or ``None`` yields ``[]``.
    """
    if not branch:
        return []
    m = _BRANCH_ISSUE_RE.match(branch)
    if not m:
        return []
    return [int(n) for n in m.group(1).split("-") if n.isdigit()]


def agent_to_record(agent: Agent) -> dict:
    """Build the compact per-agent snapshot record (the fields #125 lists).

    ``session_id`` is the harness-generic stable session id
    (``agent.session_metadata.stable_id``) — the Claude *transcript* session
    id for Claude agents, the captured thread UUID for Codex, or ``None``
    for older Claude / other harnesses without one. ``stable_id`` duplicates
    the same value under the new #205 key name (compat window: both keys
    carry it for now). Deliberately NOT ``Agent.to_dict()``: that carries
    volatile resolver internals (``monotonic_ns`` / ``sequence``) that
    change every tick and would defeat change-detection.
    """
    nums = issue_numbers_from_branch(agent.branch)
    stable_id = agent.session_metadata.stable_id
    return {
        "session_id": stable_id,
        "stable_id": stable_id,
        "tool": agent.agent_type.value,
        "cwd": agent.cwd,
        "repo": agent.repo_name,
        "branch": agent.branch,
        "issue": nums[0] if nums else None,
        "tmux_session": agent.session_name,
        "pane": agent.pane_id,
        "state": agent.state.value,
        "phase": agent.phase.value if agent.phase else None,
    }


def _parse_jsonl_lines(text: str) -> list[dict]:
    """Parse ``text`` as JSONL, skipping blank/corrupt individual lines.
    Shared by ``_read`` and ``_read_or_raise`` so the per-line tolerance
    lives in one place."""
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _record_agents(agents: list[Agent]) -> list[dict]:
    """Compact records for ``agents``, sorted so change-detection is order-insensitive."""
    recs = [agent_to_record(a) for a in agents]
    recs.sort(key=lambda r: (r["pane"], r["cwd"], r["session_id"] or ""))
    return recs


class SnapshotStore:
    """Append-only, deduped, size-bounded store of live-agent snapshots."""

    def __init__(self, path: Path | None = None, max_records: int = MAX_RECORDS) -> None:
        self._path = path or _DEFAULT_SNAPSHOTS_FILE
        self._lock = self._path.with_suffix(self._path.suffix + ".lock")
        self._max_records = max_records

    def _read(self) -> list[dict]:
        try:
            text = self._path.read_text()
        except (FileNotFoundError, OSError):
            return []
        return _parse_jsonl_lines(text)

    def _read_or_raise(self) -> list[dict]:
        """Like ``_read`` but raises ``OSError`` instead of degrading to an
        empty list when the file exists yet can't be read. Only a
        genuinely missing file (fresh store) is legitimately empty. Used
        by ``append_if_changed`` so an EIO/permissions blip can't truncate
        the ``max_records``-record crash-recovery history to a single new
        record (AHQ-014). Per-line JSON corruption is a separate,
        already-tolerated concern — ``_parse_jsonl_lines`` skips malformed
        lines regardless; this only guards whole-file unreadability."""
        try:
            text = self._path.read_text()
        except FileNotFoundError:
            return []
        return _parse_jsonl_lines(text)

    def records(self) -> list[dict]:
        """All snapshot records oldest-first; tolerates a missing/corrupt file."""
        return self._read()

    def last(self) -> dict | None:
        """The most recent snapshot record, or ``None`` if there are none."""
        records = self._read()
        return records[-1] if records else None

    def last_nonempty(self) -> dict | None:
        """The most recent record with a non-empty ``agents`` list, else ``None``.

        This is the recovery baseline: it looks past a trailing empty record
        (a cold-start / transient / legacy 0-agent snapshot) so a reboot's
        first tick can't make ``recover`` report "nothing was live". #125
        """
        for rec in reversed(self._read()):
            if rec.get("agents"):
                return rec
        return None

    def append_if_changed(self, agents: list[Agent], *, timestamp: float) -> bool:
        """Append a snapshot iff its agent payload differs from the last record.

        An empty roster is never recorded — snapshots represent live rosters
        only, and a trailing empty record would defeat post-reboot recovery.
        Truncates to the newest ``max_records`` lines via an atomic rewrite.
        Returns True iff a line was written. Never raises — a failure here must
        not disturb the daemon poll loop; it's logged and swallowed.
        """
        if not agents:
            return False
        try:
            new_agents = _record_agents(agents)
            with file_lock(self._lock):
                records = self._read_or_raise()
                if records and records[-1].get("agents") == new_agents:
                    return False
                records.append({"timestamp": timestamp, "agents": new_agents})
                if len(records) > self._max_records:
                    records = records[-self._max_records :]
                self._atomic_write(records)
            return True
        except Exception as exc:
            log.warning("snapshot append failed (%s): %s", self._path, exc)
            eventlog.emit(
                "warn",
                "snapshot append failed",
                {"path": str(self._path), "error": repr(exc)},
                min_interval_s=900,
            )
            return False

    def _atomic_write(self, records: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in records))
        tmp.replace(self._path)
