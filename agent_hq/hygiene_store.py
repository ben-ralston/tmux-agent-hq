# agent_hq/hygiene_store.py
"""Hygiene findings store (#249, Phase 4 of #245).

Design: the hygiene-sweep spec, section 1 (not part of this repository).
One file, <data_dir>/hygiene/findings.json, one flock. Same conventions as
triage_store: tolerant reads, read-or-raise + atomic write on mutation (a
corrupt file is never rebuilt from empty — AHQ-014), unparseable entries
preserved across writes.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from agent_hq._locking import file_lock
from agent_hq.filters import parse_ts
from agent_hq.triage_store import _atomic_write_json, _is_int

log = logging.getLogger(__name__)


class FindingType(Enum):
    DUPLICATE = "duplicate"
    STALE = "stale"
    CLUSTER = "cluster"
    MISMATCH = "mismatch"
    # #273: an open issue that is not an item on its repo's project board —
    # invisible to Queue/Triage/dispatch until added. Pure code, like STALE.
    OFFBOARD = "offboard"


class FindingStatus(Enum):
    OPEN = "open"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"


# Required action-payload keys per type; ``Finding.from_dict`` refuses an
# entry missing any of them (the routes and templates subscript these).
_ACTION_KEYS: dict[FindingType, tuple[str, ...]] = {
    FindingType.DUPLICATE: ("close", "of"),
    FindingType.STALE: ("close",),
    FindingType.CLUSTER: ("repo", "umbrella_title", "children"),
    FindingType.MISMATCH: ("key", "proposed"),
    FindingType.OFFBOARD: ("key", "project_number", "project_owner"),
}


def _is_key(v: object) -> bool:
    return isinstance(v, str) and "#" in v and v.rsplit("#", 1)[1].isdigit()


def _action_well_formed(ftype: FindingType, action: dict) -> bool:
    """Required keys present AND their values typed (Codex round 5): a null
    ``close`` or a string ``proposed`` reaches the card template / the store
    otherwise. ``current`` is optional (None = unscored) but int when set."""
    if not all(k in action for k in _ACTION_KEYS[ftype]):
        return False
    if ftype is FindingType.DUPLICATE:
        return _is_key(action["close"]) and _is_key(action["of"])
    if ftype is FindingType.STALE:
        return _is_key(action["close"])
    if ftype is FindingType.CLUSTER:
        children = action["children"]
        return (
            isinstance(action["repo"], str)
            and isinstance(action["umbrella_title"], str)
            and isinstance(children, list)
            and all(_is_key(c) for c in children)
        )
    if ftype is FindingType.OFFBOARD:
        # The target board rides on the action so plan_steps needs no config;
        # title / project_name are optional (a failed REST lookup leaves them).
        return (
            _is_key(action["key"])
            and _is_int(action["project_number"])
            and isinstance(action["project_owner"], str)
            and all(
                action.get(k) is None or isinstance(action[k], str)
                for k in ("title", "project_name")
            )
        )
    current = action.get("current")
    return _is_key(action["key"]) and _is_int(action["proposed"]) and (
        current is None or _is_int(current)
    )


def finding_id(ftype: FindingType, issue_keys: Iterable[str]) -> str:
    """The fingerprint Dismiss suppresses: type + sorted keys. A cluster's
    umbrella title is deliberately NOT part of it, so a rephrased title
    still matches a dismissed cluster."""
    return f"{ftype.value}:" + "+".join(sorted(issue_keys))


@dataclass
class Finding:
    id: str
    type: FindingType
    issue_keys: list[str]
    action: dict
    reasoning: str
    status: FindingStatus = FindingStatus.OPEN
    found_at: str = ""
    sweep_id: str | None = None
    model: str | None = None
    decided_at: str | None = None
    execution: dict | None = None
    # "human" once the user reshaped the finding (a cluster child removed):
    # a sweep then never refreshes or supersedes it, and the card says so.
    edited_by: str | None = None

    @classmethod
    def new(
        cls, ftype: FindingType, issue_keys: Iterable[str], *, action: dict, reasoning: str
    ) -> Finding:
        keys = sorted(set(issue_keys))
        return cls(
            id=finding_id(ftype, keys),
            type=ftype,
            issue_keys=keys,
            action=dict(action),
            reasoning=reasoning,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "issue_keys": list(self.issue_keys),
            "action": dict(self.action),
            "reasoning": self.reasoning,
            "status": self.status.value,
            "found_at": self.found_at,
            "sweep_id": self.sweep_id,
            "model": self.model,
            "decided_at": self.decided_at,
            "execution": copy.deepcopy(self.execution),
            "edited_by": self.edited_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Finding | None:
        """None for anything we can't interpret — the caller preserves the raw
        entry verbatim instead of dropping it. The action payload is validated
        per type (review round): findings.json lives in a hand-editable data
        repo, and one malformed entry must not 500 the whole Hygiene page."""
        try:
            ftype = FindingType(d.get("type"))
            status = FindingStatus(d.get("status", "open"))
        except ValueError:
            return None
        keys = d.get("issue_keys")
        if not isinstance(keys, list) or not all(isinstance(k, str) and "#" in k for k in keys):
            return None
        fid = d.get("id")
        if not isinstance(fid, str) or not fid:
            return None
        action = d.get("action")
        if not isinstance(action, dict) or not _action_well_formed(ftype, action):
            return None
        execution = d.get("execution")
        return cls(
            id=fid,
            type=ftype,
            issue_keys=list(keys),
            action=dict(action),
            reasoning=str(d.get("reasoning") or ""),
            status=status,
            found_at=str(d.get("found_at") or ""),
            sweep_id=d.get("sweep_id") if isinstance(d.get("sweep_id"), str) else None,
            model=d.get("model") if isinstance(d.get("model"), str) else None,
            decided_at=d.get("decided_at") if isinstance(d.get("decided_at"), str) else None,
            execution=copy.deepcopy(execution) if isinstance(execution, dict) else None,
            edited_by=d.get("edited_by") if isinstance(d.get("edited_by"), str) else None,
        )


@dataclass
class SweepWrite:
    inserted: int = 0
    refreshed: int = 0
    suppressed: int = 0
    resolved: int = 0


@dataclass
class _FileState:
    last_swept_at: str | None
    findings: dict[str, Finding]
    unparsed: dict = field(default_factory=dict)


class HygieneStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._path = self.data_dir / "hygiene" / "findings.json"

    # ---- parsing --------------------------------------------------------

    def _from_raw(self, data: object) -> _FileState:
        if not isinstance(data, dict):
            return _FileState(None, {})
        swept = data.get("last_swept_at")
        state = _FileState(swept if isinstance(swept, str) else None, {})
        raw_findings = data.get("findings")
        if not isinstance(raw_findings, dict):
            return state
        for fid, raw in raw_findings.items():
            parsed = Finding.from_dict(raw) if isinstance(raw, dict) else None
            if parsed is None:
                log.warning("preserving malformed hygiene finding %s in %s", fid, self._path)
                state.unparsed[fid] = raw
            else:
                state.findings[fid] = parsed
        return state

    def _read(self) -> _FileState:
        try:
            data = json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return _FileState(None, {})
        return self._from_raw(data)

    def _read_or_raise(self) -> _FileState:
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return _FileState(None, {})
        except OSError as exc:
            raise OSError(f"cannot read {self._path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OSError(f"{self._path} corrupt: {exc}") from exc
        if not isinstance(data, dict):
            raise OSError(f"{self._path} root is not an object; refusing to overwrite")
        return self._from_raw(data)

    def _write(self, state: _FileState) -> None:
        findings = {**state.unparsed, **{k: f.to_dict() for k, f in state.findings.items()}}
        _atomic_write_json(
            self._path,
            {"last_swept_at": state.last_swept_at, "findings": dict(sorted(findings.items()))},
        )

    def _mutate(self, fn) -> object:
        """Lock → read-or-raise → fn(state) → write. Raises ``OSError`` on an
        unreadable/corrupt file (never rebuilt from empty, AHQ-014); the sweep
        writers propagate it so ``run_sweep`` reports a write failure, the
        bool writers below degrade to ``False``."""
        lock = self._path.with_suffix(self._path.suffix + ".lock")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(lock):
            state = self._read_or_raise()
            result = fn(state)
            self._write(state)
            return result

    def _mutate_bool(self, fn) -> bool:
        try:
            return self._mutate(fn) is True
        except OSError as exc:
            log.error("cannot read %s, skipping hygiene write: %s", self._path, exc)
            return False

    # ---- reads ------------------------------------------------------------

    def all_findings(self) -> list[Finding]:
        return list(self._read().findings.values())

    def get(self, fid: str) -> Finding | None:
        return self._read().findings.get(fid)

    def open_count(self) -> int:
        return sum(1 for f in self._read().findings.values() if f.status is FindingStatus.OPEN)

    def last_swept_at(self) -> str | None:
        return self._read().last_swept_at

    def snapshot(self, *, now: str | None = None) -> tuple[list[Finding], str | None]:
        """Findings + stamp from ONE tolerant read (the web's per-request
        snapshot; review round 2). With ``now``, a claim past its lease is
        rendered released (the in-memory copy only) so the card does not
        say "Executing…" for a process that died (review round 3)."""
        state = self._read()
        findings = list(state.findings.values())
        if now is not None:
            for i, f in enumerate(findings):
                if isinstance(f.execution, dict) and f.execution.get("in_flight"):
                    if not _in_flight(f, now):
                        f = copy.deepcopy(f)
                        f.execution.pop("in_flight", None)
                        findings[i] = f
        return findings, state.last_swept_at

    # ---- writes -----------------------------------------------------------

    def record_sweep(
        self,
        findings: Iterable[Finding],
        *,
        now: str,
        sweep_id: str,
        model: str | None,
        open_keys: set[str],
        offboard_repos: set[str] | None = None,
        offboard_statusless: set[str] | None = None,
        offboard_board_keys: set[str] | None = None,
    ) -> SweepWrite:
        """``open_keys`` is the board's open set. ``offboard_repos`` names the
        repos the off-board detector actually checked this sweep (#273): an
        OPEN off-board finding there that was not re-reported is closed or
        gone and resolves, exactly as the stale rule; ``None``/empty judges
        none (a cold board cache or an unseeded index skips the repo).
        ``offboard_statusless`` lists board items with no Status: an
        off-board finding whose item-add landed but whose Todo write failed
        keeps its card (and failed log) only while its item is in that set
        (review round 3) — once a Status is set, by hand or otherwise, the
        premise is gone and it resolves like any other. ``offboard_board_keys``
        is the set of keys sitting on their OWN repo's target board (round 8:
        ``open_keys`` spans every configured board, and an issue parked on
        another repo's board has not landed); ``None`` falls back to
        ``open_keys`` for callers without off-board inputs."""
        incoming = list(findings)
        # Repo names compare case-insensitively (Codex review): an empty board
        # keeps the config's spelling and the first add switches the sweep to
        # GitHub's, so the checked set and a finding's key can differ in case.
        checked_offboard = {r.lower() for r in (offboard_repos or ())}
        statusless = {k.lower() for k in (offboard_statusless or ())}  # case: round 10
        landed_keys = {
            k.lower() for k in (open_keys if offboard_board_keys is None else offboard_board_keys)
        }

        def apply(state: _FileState) -> SweepWrite:
            w = SweepWrite()
            offboard_by_fold = {
                fid.lower(): x
                for fid, x in state.findings.items()
                if x.type is FindingType.OFFBOARD
            }
            for f in incoming:
                existing = state.findings.get(f.id)
                if existing is None and f.type is FindingType.OFFBOARD:
                    # Same issue under the other spelling is the SAME finding
                    # (Codex review): refresh / suppress it, never a twin card.
                    existing = offboard_by_fold.get(f.id.lower())
                if existing is not None and existing.status is FindingStatus.RESOLVED:
                    # Revive (review round 3): RESOLVED means the premise looked
                    # gone — an issue parked in the board's Done column reads
                    # as closed to the sweep — not that a human decided. The
                    # same proposal returning means the premise is back, so
                    # it goes through the ordinary INSERT path below (round
                    # 4: the overlap guards and supersession apply to it like
                    # any new cluster). Dismissed / accepted stay suppressed.
                    # The insert overwrites the record under the same id; a
                    # guard that drops the proposal leaves it RESOLVED.
                    existing = None
                if (
                    existing is not None
                    and existing.status is FindingStatus.ACCEPTED
                    and f.type is FindingType.OFFBOARD
                    and (
                        _board_of(existing.action) != _board_of(f.action)
                        or _decided_before(existing, now, ACCEPTED_REVIVE_GRACE_S)
                    )
                ):
                    # #273 review round 5: an add accepted against one board
                    # says nothing about a re-pointed ``project:`` — the same
                    # issue off a DIFFERENT board is a new finding. Round 8:
                    # an add can be undone (item removed by hand), so a
                    # re-report well past the decision is a recurred premise;
                    # within the grace it is the sweep racing the accept.
                    # Dismissed (the user said no to the issue) stays dismissed.
                    existing = None
                if existing is not None and existing.status is not FindingStatus.OPEN:
                    w.suppressed += 1
                    continue
                if existing is not None and (
                    _in_flight(existing, now) or existing.edited_by or _partially_executed(existing)
                ):
                    # Mid-Accept (review round 2): the action being executed
                    # must not be rewritten under the executor's feet. A
                    # human-edited finding is the user's decision, not the
                    # model's to refresh; a partially executed one carries
                    # the umbrella a retry must reuse (Codex round 2).
                    continue
                if f.type is FindingType.CLUSTER and any(
                    other.type is FindingType.CLUSTER
                    and other.status is FindingStatus.OPEN
                    and (other.edited_by or _partially_executed(other))
                    and set(other.issue_keys) & set(f.issue_keys)
                    for other in state.findings.values()
                ):
                    # A regrouping that overlaps a cluster the user reshaped
                    # (or one whose umbrella already exists) would undo the
                    # edit / orphan the umbrella via supersession; it loses.
                    w.suppressed += 1
                    continue
                if existing is not None:
                    existing.reasoning = f.reasoning
                    action = dict(f.action)
                    if (
                        f.type is FindingType.OFFBOARD
                        and action.get("title") is None
                        and existing.action.get("title")
                    ):
                        # The title comes from a capped, failure-tolerant REST
                        # lookup (#273 review round 1): a miss must not
                        # degrade a card that already had one.
                        action["title"] = existing.action["title"]
                    existing.action = action
                    existing.found_at = now
                    existing.sweep_id = sweep_id
                    existing.model = model
                    w.refreshed += 1
                    continue
                if f.type is FindingType.CLUSTER:
                    # Supersession (review round 2): a regrouped cluster has
                    # a new fingerprint; the older overlapping OPEN cluster
                    # is moot, not a second card. Dismissed ones stay put.
                    for other in state.findings.values():
                        if (
                            other.type is FindingType.CLUSTER
                            and other.status is FindingStatus.OPEN
                            and other.id != f.id
                            and not _in_flight(other, now)
                            and set(other.issue_keys) & set(f.issue_keys)
                        ):
                            other.status = FindingStatus.RESOLVED
                            other.decided_at = now
                            w.resolved += 1
                state.findings[f.id] = Finding(
                    id=f.id,
                    type=f.type,
                    issue_keys=list(f.issue_keys),
                    action=dict(f.action),
                    reasoning=f.reasoning,
                    found_at=now,
                    sweep_id=sweep_id,
                    model=model,
                )
                w.inserted += 1
            incoming_ids = {f.id for f in incoming}
            incoming_fold = {i.lower() for i in incoming_ids}
            for f in state.findings.values():
                if f.status is not FindingStatus.OPEN or _in_flight(f, now):
                    continue
                if (
                    f.type is FindingType.STALE
                    and f.id not in incoming_ids
                    and not _partially_executed(f)
                    and _repo_of(f.issue_keys[0]) in open_repos
                    and f.issue_keys[0] in open_keys
                ):
                    # Stale detection is code and runs every sweep: an open
                    # stale finding this sweep did not re-report stopped
                    # qualifying (new activity / raised value) — Codex round 2.
                    f.status = FindingStatus.RESOLVED
                    f.decided_at = now
                    w.resolved += 1
                    continue
                if f.type is FindingType.OFFBOARD:
                    key = f.issue_keys[0]
                    repo, reported = _repo_of(key).lower(), f.id.lower() in incoming_fold
                    if _partially_executed(f) and (
                        key.lower() in statusless or repo not in checked_offboard
                    ):
                        # The retry still has the Todo write to finish — or the
                        # board was not checked this sweep (round 4: a failed
                        # refetch leaves last-good open_keys listing the
                        # status-less item) and unknown must not mean "done".
                        continue
                    # Never the finding this very sweep inserted (round 6: the
                    # web runner keeps a stale open set when its refetch
                    # returns nothing, and insert-then-resolve renders nothing).
                    landed = not reported and key.lower() in landed_keys
                    gone = not reported and repo in checked_offboard
                    if landed or gone:
                        f.status = FindingStatus.RESOLVED
                        f.decided_at = now
                        w.resolved += 1
                    continue
                if _premise_gone(f, open_keys, open_repos):
                    f.status = FindingStatus.RESOLVED
                    f.decided_at = now
                    w.resolved += 1
            state.last_swept_at = now
            return w

        open_repos = {_repo_of(k) for k in open_keys}
        return self._mutate(apply)

    def stamp_swept(self, now: str) -> None:
        def apply(state: _FileState) -> None:
            state.last_swept_at = now

        self._mutate(apply)

    def dismiss(self, fid: str, *, now: str, expected_found_at: str | None = None) -> bool:
        def apply(state: _FileState) -> bool:
            f = state.findings.get(fid)
            if f is None or f.status is not FindingStatus.OPEN or _in_flight(f, now):
                return False
            if expected_found_at is not None and f.found_at != expected_found_at:
                return False
            f.status = FindingStatus.DISMISSED
            f.decided_at = now
            return True

        return self._mutate_bool(apply)

    def claim(self, fid: str, *, now: str, expected_found_at: str | None = None) -> bool:
        """Atomically take an OPEN finding for execution (review round): the
        accept route claims under the flock before any GitHub write, so two
        Execute taps — or a retry racing an in-flight run — can't both run the
        plan. The prior step log is kept so a retry can reuse an umbrella it
        already created. Released by ``mark_accepted`` / ``mark_failed``."""

        def apply(state: _FileState) -> bool:
            f = state.findings.get(fid)
            if f is None or f.status is not FindingStatus.OPEN or _in_flight(f, now):
                return False
            if expected_found_at is not None and f.found_at != expected_found_at:
                return False
            prior = copy.deepcopy(f.execution) if isinstance(f.execution, dict) else {}
            f.execution = {**prior, "in_flight": True, "claimed_at": now}
            return True

        return self._mutate_bool(apply)

    def record_progress(
        self,
        fid: str,
        *,
        plan: list[str],
        done: list[dict],
        now: str | None = None,
        facts: dict | None = None,
    ) -> bool:
        """Live step log for the Execute checklist (hand-test round): the
        accept route writes the plan's labels once it is built and the
        cumulative results after each step. Only while the claim is held —
        the released log written by ``mark_accepted`` / ``mark_failed`` is
        the durable record, and ``_released`` drops both keys."""

        def apply(state: _FileState) -> bool:
            f = state.findings.get(fid)
            if f is None or f.status is not FindingStatus.OPEN or not _in_flight(f, now):
                return False
            f.execution = {
                **f.execution,
                **copy.deepcopy(facts or {}),  # e.g. umbrella — durable at once (round 4)
                "plan": list(plan),
                "progress": copy.deepcopy(done),
            }
            return True

        return self._mutate_bool(apply)

    def remove_child(
        self, fid: str, key: str, *, now: str, expected_found_at: str | None = None
    ) -> bool:
        """Drop one issue from an OPEN cluster (hand-test round: "I agree with
        the cluster, minus this one"). Revision-guarded like ``dismiss``;
        refused in flight, for a non-cluster, an unknown key, or when fewer
        than two children would remain. The edited finding is re-keyed to
        the fingerprint of its remaining children (review round 3: Dismiss
        must suppress the grouping on screen) and the AI's original id is
        left behind as a DISMISSED tombstone — the user rejected that
        grouping. ``found_at`` moves so a painted card/preview goes stale."""

        def apply(state: _FileState) -> bool:
            f = state.findings.get(fid)
            if f is None or f.status is not FindingStatus.OPEN or _in_flight(f, now):
                return False
            if f.type is not FindingType.CLUSTER or key not in f.issue_keys:
                return False
            if expected_found_at is not None and f.found_at != expected_found_at:
                return False
            remaining = [k for k in f.issue_keys if k != key]
            if len(remaining) < 2:
                return False
            children = [k for k in f.action.get("children", []) if k != key]
            edited = Finding(
                id=finding_id(f.type, remaining),
                type=f.type,
                issue_keys=remaining,
                action={**f.action, "children": children},
                reasoning=f.reasoning,
                status=FindingStatus.OPEN,
                found_at=now,
                sweep_id=f.sweep_id,
                model=f.model,
                execution=copy.deepcopy(f.execution),
                edited_by="human",
            )
            f.status = FindingStatus.DISMISSED
            f.decided_at = now
            f.execution = None
            state.findings[edited.id] = edited
            return True

        return self._mutate_bool(apply)

    def mark_accepted(self, fid: str, *, now: str, execution: dict) -> bool:
        def apply(state: _FileState) -> bool:
            f = state.findings.get(fid)
            if f is None or f.status is not FindingStatus.OPEN:
                return False
            f.status = FindingStatus.ACCEPTED
            f.decided_at = now
            f.execution = _released(_with_plan(f.execution, execution))
            return True

        return self._mutate_bool(apply)

    def mark_failed(self, fid: str, *, execution: dict) -> bool:
        def apply(state: _FileState) -> bool:
            f = state.findings.get(fid)
            if f is None or f.status is not FindingStatus.OPEN:
                return False
            f.execution = _released(_with_plan(f.execution, execution))
            return True

        return self._mutate_bool(apply)


def _repo_of(key: str) -> str:
    return key.rsplit("#", 1)[0]


def _board_of(action: dict) -> tuple:
    return (action.get("project_owner"), action.get("project_number"))


# An ACCEPTED off-board finding re-reported this long after its decision is
# treated as recurred (the item was removed again), not as a sweep that
# snapshotted the board before the add landed.
ACCEPTED_REVIVE_GRACE_S = 10 * 60


def _decided_before(f: Finding, now: str, grace_s: float) -> bool:
    decided, ref = parse_ts(f.decided_at), parse_ts(now)
    if decided is None or ref is None:
        return False
    return ref - decided > grace_s


# A claim is a lease, not a lock (review round 3): a process that died
# between claim and mark_* would otherwise wedge the finding forever. Long
# enough for a cluster of many children over a slow gh; short enough that a
# stuck card frees itself within the hour.
CLAIM_LEASE_S = 30 * 60


def _in_flight(f: Finding, now: str | None = None) -> bool:
    ex = f.execution
    if not (isinstance(ex, dict) and ex.get("in_flight")):
        return False
    if now is None:
        return True
    claimed, ref = parse_ts(ex.get("claimed_at")), parse_ts(now)
    if claimed is None or ref is None:
        return True  # unparseable: keep the conservative answer
    return ref - claimed < CLAIM_LEASE_S


def _partially_executed(f: Finding) -> bool:
    """A failed attempt that already wrote to GitHub — created its umbrella
    (Codex round 2) or posted its closing comment (built-in round 4): the
    retry must keep finding it, so the sweep neither supersedes it, nor
    premise-resolves it while any child is open, nor resolves a stale one
    for not being re-reported (our own comment moved its updated_at)."""
    ex = f.execution
    if not isinstance(ex, dict) or ex.get("ok"):
        return False
    return (
        isinstance(ex.get("umbrella"), int)
        or bool(ex.get("commented_at"))
        # #273 review round 2: item-add landed, the Todo write did not — the
        # issue is on the board now, so both off-board resolve rules would
        # fire and take the failed log with them, leaving a status-less item
        # nobody is told about. The card stays while the item has no Status
        # (round 3: ``record_sweep`` checks that), until the retry finishes.
        or bool(ex.get("item_id"))
    )


# Keys on ``Finding.execution`` that describe an attempt rather than facts a
# retry reuses (``umbrella``): ``plan`` / ``progress`` are the live checklist.
EXECUTION_BOOKKEEPING = ("ok", "at", "steps", "in_flight", "claimed_at", "plan", "progress")


def _with_plan(stored: dict | None, execution: dict) -> dict:
    """Carry the recorded plan labels into the final log unless it has its own."""
    if "plan" in execution or not isinstance(stored, dict) or "plan" not in stored:
        return execution
    return {**execution, "plan": stored["plan"]}


def _released(execution: dict) -> dict:
    """Drop the live keys; ``plan`` stays so the final checklist can list the
    steps that never ran (review round 3)."""
    out = copy.deepcopy(execution)
    for key in ("in_flight", "progress"):
        out.pop(key, None)
    return out


def _premise_gone(f: Finding, open_keys: set[str], open_repos: set[str]) -> bool:
    """Should an OPEN finding resolve given this sweep's open set?

    Only judged when every repo the finding touches was actually swept — a
    cold cache or a failed fetch for one project must not resolve (and
    thereby permanently suppress) findings it never saw (review round).
    Duplicate: either issue gone breaks the premise (the pair no longer
    exists). Cluster: the umbrella still makes sense for whatever children
    remain, so it resolves only once fewer than two are open (hand-test
    round: two of four children closed by earlier duplicate accepts left a
    valid pair; ``plan_steps`` skips the closed ones). Stale / mismatch: the
    one issue is gone. Off-board (#273) is judged inline by ``record_sweep``
    (its rules need the status-less set) and never reaches here.
    """
    if not {_repo_of(k) for k in f.issue_keys} <= open_repos:
        return False
    if f.type is FindingType.DUPLICATE:
        return any(k not in open_keys for k in f.issue_keys)
    if f.type is FindingType.CLUSTER:
        floor = 1 if _partially_executed(f) else 2
        return sum(1 for k in f.issue_keys if k in open_keys) < floor
    return not any(k in open_keys for k in f.issue_keys)


# ---- Thresholds (config.json "hygiene" key) ----

DEFAULT_HYGIENE_THRESHOLDS: dict = {"stale_days": 90, "stale_max_value": 2}


def load_hygiene_thresholds(data_dir: Path) -> dict:
    """Stale thresholds from <data_dir>/config.json {"hygiene": {...}}; every
    failure mode degrades to the defaults for that key."""
    merged = dict(DEFAULT_HYGIENE_THRESHOLDS)
    try:
        data = json.loads((Path(data_dir) / "config.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return merged
    if not isinstance(data, dict) or not isinstance(data.get("hygiene"), dict):
        return merged
    for key, val in data["hygiene"].items():
        if key in merged and _is_int(val) and val >= 0:
            merged[key] = val
    return merged
