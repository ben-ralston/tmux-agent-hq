"""Autopilot state (#302): pause switch, per-issue attempts, run log.

One file, <data_dir>/autopilot/state.json, following the triage_store /
hygiene_store conventions: flock around read-modify-write, read-or-raise so
a corrupt file is never rebuilt from empty, atomic temp+rename writes.

Fail-closed rules (Codex plan review):
- an unparseable attempt still blocks its issue and counts as in flight;
- a launch is RESERVED (``LAUNCHING``) before launch.sh runs, and the
  reservation re-checks pause / existing attempt / capacity under the lock;
- outcome updates are compare-and-set on (attempt_id, expected outcome), so
  a slow refresh can never rewrite a replacement attempt.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from agent_hq._locking import file_lock
from agent_hq.triage_store import _atomic_write_json

log = logging.getLogger(__name__)

MAX_RUNS = 30
STALE_LAUNCH_S = 3600


class Outcome(Enum):
    LAUNCHING = "launching"
    RUNNING = "running"
    PR_OPEN = "pr_open"
    UNKNOWN = "unknown"
    MERGED = "merged"
    CLOSED = "closed"
    ENDED_WITHOUT_PR = "ended_without_pr"
    LAUNCH_FAILED = "launch_failed"


IN_FLIGHT_OUTCOMES = frozenset(
    {Outcome.LAUNCHING, Outcome.RUNNING, Outcome.PR_OPEN, Outcome.UNKNOWN}
)
RETRYABLE_OUTCOMES = frozenset(
    {Outcome.LAUNCH_FAILED, Outcome.ENDED_WITHOUT_PR, Outcome.CLOSED, Outcome.UNKNOWN}
)

_STR_FIELDS = ("issue_key", "repo", "branch", "run_id", "dispatched_at", "attempt_id")
_OPT_FIELDS = ("session_name", "worktree_path", "pr_url", "detail")


class ReserveResult(Enum):
    OK = "ok"
    PAUSED = "paused"
    EXISTS = "exists"
    FULL = "full"


@dataclass
class Attempt:
    issue_key: str
    repo: str
    branch: str
    run_id: str
    dispatched_at: str
    outcome: Outcome
    attempt_id: str
    session_name: str | None = None
    worktree_path: str | None = None
    pr_url: str | None = None
    detail: str | None = None
    updated_at: str = ""

    def to_dict(self) -> dict:
        d = {f: getattr(self, f) for f in (*_STR_FIELDS, *_OPT_FIELDS)}
        d["outcome"] = self.outcome.value
        d["updated_at"] = self.updated_at
        return d

    @classmethod
    def from_dict(cls, d: object) -> Attempt | None:
        if not isinstance(d, dict) or not all(isinstance(d.get(f), str) for f in _STR_FIELDS):
            return None
        try:
            outcome = Outcome(d.get("outcome"))
        except ValueError:
            return None
        opts = {f: d.get(f) if isinstance(d.get(f), str) else None for f in _OPT_FIELDS}
        updated = d.get("updated_at")
        return cls(
            **{f: d[f] for f in _STR_FIELDS},
            outcome=outcome,
            **opts,
            updated_at=updated if isinstance(updated, str) else "",
        )


@dataclass(frozen=True)
class OutcomeUpdate:
    attempt_id: str
    expected: Outcome
    new: Outcome
    pr_url: str | None = None
    session_name: str | None = None


@dataclass
class RunRecord:
    run_id: str
    started_at: str
    finished_at: str
    launched: list[str]
    skipped: list[tuple[str, str]]
    throttled: bool
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "launched": list(self.launched),
            "skipped": [list(s) for s in self.skipped],
            "throttled": self.throttled,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: object) -> RunRecord | None:
        if not isinstance(d, dict) or not isinstance(d.get("run_id"), str):
            return None
        # Non-list values are dropped rather than iterated: a TypeError here
        # would escape snapshot()'s OSError net and crash every reader.
        raw_skipped = d.get("skipped")
        raw_launched = d.get("launched")
        skipped = [
            (s[0], s[1])
            for s in (raw_skipped if isinstance(raw_skipped, list) else [])
            if isinstance(s, list) and len(s) == 2 and all(isinstance(x, str) for x in s)
        ]
        err = d.get("error")
        return cls(
            run_id=d["run_id"],
            started_at=str(d.get("started_at", "")),
            finished_at=str(d.get("finished_at", "")),
            launched=[
                k
                for k in (raw_launched if isinstance(raw_launched, list) else [])
                if isinstance(k, str)
            ],
            skipped=skipped,
            throttled=d.get("throttled") is True,
            error=err if isinstance(err, str) else None,
        )


@dataclass
class AutopilotState:
    paused: bool = False
    attempts: dict[str, Attempt] = field(default_factory=dict)
    runs: list[RunRecord] = field(default_factory=list)
    refreshed_at: str | None = None
    unparsed: dict = field(default_factory=dict)

    @property
    def blocked_keys(self) -> set[str]:
        return set(self.attempts) | set(self.unparsed)

    @property
    def in_flight(self) -> int:
        live = sum(1 for a in self.attempts.values() if a.outcome in IN_FLIGHT_OUTCOMES)
        return live + len(self.unparsed)


def _age_s(ts: str, now: str) -> float | None:
    try:
        return (datetime.fromisoformat(now) - datetime.fromisoformat(ts)).total_seconds()
    except (ValueError, TypeError):
        return None


def _retryable(a: Attempt, now: str) -> bool:
    """A finished-without-merge attempt, or a launch stuck past the stale
    window. Anything else still holds a live agent or a decision."""
    stale_launch = a.outcome is Outcome.LAUNCHING and (
        (_age_s(a.dispatched_at, now) or 0) > STALE_LAUNCH_S
    )
    return a.outcome in RETRYABLE_OUTCOMES or stale_launch


class AutopilotStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "autopilot" / "state.json"

    def _from_raw(self, data: dict) -> AutopilotState:
        # The kill switch fails closed: absent means a fresh, unpaused file,
        # but a present non-bool value (a hand edit like "true" or 1) pauses.
        paused = data.get("paused", False)
        state = AutopilotState(paused=paused if isinstance(paused, bool) else True)
        ref = data.get("refreshed_at")
        state.refreshed_at = ref if isinstance(ref, str) else None
        for key, raw in (data.get("attempts") or {}).items():
            parsed = Attempt.from_dict(raw)
            if parsed is None or parsed.issue_key != key:
                log.warning("preserving malformed autopilot attempt %s in %s", key, self.path)
                state.unparsed[key] = raw
            else:
                state.attempts[key] = parsed
        for raw in data.get("runs") or []:
            run = RunRecord.from_dict(raw)
            if run is not None:
                state.runs.append(run)
        return state

    def _read_or_raise(self) -> AutopilotState:
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return AutopilotState()
        except UnicodeDecodeError as exc:
            # A ValueError, not an OSError: re-raise so the fail-closed
            # contract (snapshot degrades, writers refuse) still holds.
            raise OSError(f"{self.path} corrupt: {exc}") from exc
        except OSError as exc:
            raise OSError(f"cannot read {self.path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OSError(f"{self.path} corrupt: {exc}") from exc
        if not isinstance(data, dict):
            raise OSError(f"{self.path} root is not an object; refusing to overwrite")
        if not isinstance(data.get("attempts", {}), dict) or not isinstance(
            data.get("runs", []), list
        ):
            raise OSError(f"{self.path} attempts/runs malformed; refusing to dispatch or overwrite")
        return self._from_raw(data)

    def _write(self, state: AutopilotState) -> None:
        attempts = {**state.unparsed, **{k: a.to_dict() for k, a in state.attempts.items()}}
        _atomic_write_json(
            self.path,
            {
                "paused": state.paused,
                "refreshed_at": state.refreshed_at,
                "attempts": dict(sorted(attempts.items())),
                "runs": [r.to_dict() for r in state.runs[-MAX_RUNS:]],
            },
        )

    def _mutate(self, fn, *, changed=lambda result: True):
        """Read-modify-write under the lock. ``changed(result)`` False skips
        the write: the triage data repo's syncer commits and pushes every
        write, so a no-op must not touch the file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            state = self._read_or_raise()
            result = fn(state)
            if changed(result):
                self._write(state)
            return result

    # ---- reads ----

    def assert_readable(self) -> None:
        self._read_or_raise()

    def snapshot(self) -> AutopilotState:
        try:
            return self._read_or_raise()
        except OSError:
            return AutopilotState()

    # ---- writes ----

    def set_paused(self, paused: bool) -> None:
        def apply(s):
            s.paused = paused

        self._mutate(apply)

    def reserve(self, attempt: Attempt, *, max_open: int) -> ReserveResult:
        def apply(s: AutopilotState) -> ReserveResult:
            if s.paused:
                return ReserveResult.PAUSED
            if attempt.issue_key in s.blocked_keys:
                return ReserveResult.EXISTS
            if s.in_flight >= max_open:
                return ReserveResult.FULL
            attempt.outcome = Outcome.LAUNCHING
            s.attempts[attempt.issue_key] = attempt
            return ReserveResult.OK

        return self._mutate(apply)

    def finish_launch(
        self, issue_key, attempt_id, *, outcome, session_name, worktree_path, detail, now
    ) -> bool:
        def apply(s: AutopilotState) -> bool:
            a = s.attempts.get(issue_key)
            if a is None or a.attempt_id != attempt_id or a.outcome is not Outcome.LAUNCHING:
                return False
            a.outcome, a.session_name, a.worktree_path = outcome, session_name, worktree_path
            a.detail, a.updated_at = detail, now
            return True

        return self._mutate(apply) is True

    def apply_outcomes(self, updates: dict[str, OutcomeUpdate], *, now: str) -> int:
        """Apply the CAS updates; returns how many landed. Writes (and stamps
        ``refreshed_at``) only when at least one did — never on a no-op."""
        if not updates:
            return 0

        def apply(s: AutopilotState) -> int:
            n = 0
            for key, u in updates.items():
                a = s.attempts.get(key)
                if a is None or a.attempt_id != u.attempt_id or a.outcome is not u.expected:
                    continue
                a.outcome = u.new
                a.pr_url = u.pr_url or a.pr_url
                a.session_name = u.session_name or a.session_name
                a.updated_at = now
                n += 1
            if n:
                s.refreshed_at = now
            return n

        return self._mutate(apply, changed=bool)

    def retry(self, issue_key: str, *, now: str) -> bool:
        def apply(s: AutopilotState) -> bool:
            a = s.attempts.get(issue_key)
            if a is None or not _retryable(a, now):
                return False
            del s.attempts[issue_key]
            return True

        return self._mutate(apply) is True

    def can_retry(self, issue_key: str, *, now: str) -> bool:
        """Read-only twin of ``retry``'s guard — the Discard route asks first
        so it never unflags an issue whose attempt it then can't forget."""
        a = self.snapshot().attempts.get(issue_key)
        return a is not None and _retryable(a, now)

    def record_run(self, run: RunRecord) -> None:
        def apply(s):
            s.runs.append(run)
            del s.runs[:-MAX_RUNS]

        self._mutate(apply)
