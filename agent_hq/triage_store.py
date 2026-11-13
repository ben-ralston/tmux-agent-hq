"""Triage records, rank formula, and disk store (#246).

Design: the issue-prioritization redesign spec (not part of this repository),
Section 1. Records live under <data_dir>/triage/<owner>__<repo>.json, one JSON
object per repo keyed by full "owner/repo#number". Store mutations follow the
pinned_store.py conventions: flock + read-or-raise + atomic write.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from agent_hq._locking import file_lock

log = logging.getLogger(__name__)


class Effort(Enum):
    S = "S"
    M = "M"
    L = "L"


class AgentFitness(Enum):
    SOLO = "solo"
    PAIR = "pair"
    NEEDS_YOU = "needs_you"


class TriageState(Enum):
    UNTRIAGED = "untriaged"
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"


DIMENSIONS = ("value", "effort", "agent_fitness")
# Who wrote a dimension. "duet" is Claude Duet over the remote connector
# (#397): it writes directly like a human (never counted as the owner), and the AI
# worker never overwrites it.
ACTORS = ("ai", "human", "duet")
NON_AI_ACTORS = frozenset({"human", "duet"})


def _enum_or_none(cls, raw):
    try:
        return cls(raw) if raw is not None else None
    except ValueError:
        return None


def _is_int(v) -> bool:
    # bool subclasses int — a JSON `true` must not rank as 1.
    return isinstance(v, int) and not isinstance(v, bool)


@dataclass
class TriageRecord:
    issue_key: str  # "owner/repo#number"
    value: int | None = None  # 1-5
    effort: Effort | None = None
    agent_fitness: AgentFitness | None = None
    # Tri-state booleans: None means "not supplied" so partial updates
    # can't silently wipe a set flag. interest is human-only; AI never sets it.
    interest: bool | None = None
    state: TriageState = TriageState.UNTRIAGED
    pin: bool | None = None
    # Pin STAGED from the Triage inbox row (#267): human-only like ``pin``,
    # but inert — the Queue reads ``pin`` — until ``confirm`` folds it in
    # (``pin = pin_staged; pin_staged = None``). A direct ``pin`` write
    # supersedes it. Lets an inbox row pin-before-confirm without an AI
    # proposal ever moving, or un-moving, a human's Queue placement.
    pin_staged: bool | None = None
    # Human-only opt-in for the nightly autopilot (#302). Same guard as
    # ``interest``: AI upserts never set or clear it.
    autopilot: bool | None = None
    provenance: dict[str, str] = field(default_factory=dict)  # dim -> an ACTORS value
    reasoning: dict[str, str] = field(default_factory=dict)  # dim -> one-liner
    proposed_at: str | None = None  # ISO-8601
    model: str | None = None  # model id that proposed
    updated_at: str = ""
    body_hash: str | None = None  # issue-body hash at proposal time (Phase 3)
    tombstoned_at: str | None = None  # set when the issue closed on GitHub
    # Old Need/Want/Idea label captured at seeding (#247) — a hint for the
    # Phase-3 triage worker, never a confirmed value.
    priority_hint: str | None = None

    @classmethod
    def untriaged(cls, issue_key: str, *, updated_at: str) -> TriageRecord:
        return cls(issue_key=issue_key, updated_at=updated_at)

    def to_dict(self) -> dict:
        return {
            "issue_key": self.issue_key,
            "value": self.value,
            "effort": self.effort.value if self.effort else None,
            "agent_fitness": self.agent_fitness.value if self.agent_fitness else None,
            "interest": self.interest,
            "state": self.state.value,
            "pin": self.pin,
            "pin_staged": self.pin_staged,
            "autopilot": self.autopilot,
            "provenance": dict(self.provenance),
            "reasoning": dict(self.reasoning),
            "proposed_at": self.proposed_at,
            "model": self.model,
            "updated_at": self.updated_at,
            "body_hash": self.body_hash,
            "tombstoned_at": self.tombstoned_at,
            "priority_hint": self.priority_hint,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TriageRecord:
        value = d.get("value")
        if not (_is_int(value) and 1 <= value <= 5):
            value = None
        return cls(
            issue_key=d["issue_key"],
            value=value,
            effort=_enum_or_none(Effort, d.get("effort")),
            agent_fitness=_enum_or_none(AgentFitness, d.get("agent_fitness")),
            interest=d.get("interest") if isinstance(d.get("interest"), bool) else None,
            state=_enum_or_none(TriageState, d.get("state")) or TriageState.UNTRIAGED,
            pin=d.get("pin") if isinstance(d.get("pin"), bool) else None,
            pin_staged=d.get("pin_staged") if isinstance(d.get("pin_staged"), bool) else None,
            autopilot=d.get("autopilot") if isinstance(d.get("autopilot"), bool) else None,
            provenance={
                k: v for k, v in (d.get("provenance") or {}).items() if v in ACTORS
            },
            reasoning=dict(d.get("reasoning") or {}),
            proposed_at=d.get("proposed_at"),
            model=d.get("model"),
            updated_at=d.get("updated_at", ""),
            body_hash=d.get("body_hash"),
            tombstoned_at=d.get("tombstoned_at"),
            priority_hint=(
                d.get("priority_hint") if isinstance(d.get("priority_hint"), str) else None
            ),
        )


# ---- Rank formula (spec Section 1) ----

# rank = value*2 + effort bonus + fitness bonus + interest bonus.
# Tunable via <data_dir>/config.json {"rank_weights": {...}} — same key names.
DEFAULT_WEIGHTS: dict = {
    "value_multiplier": 2,
    "effort_bonus": {"S": 2, "M": 1, "L": 0},
    "fitness_bonus": {"solo": 2, "pair": 1, "needs_you": 0},
    "interest_bonus": 2,
}


def rank_breakdown(record: TriageRecord, weights: dict | None = None) -> list[tuple[str, int]]:
    """Legible score components: [("V4", 8), ("M", 1), ("solo", 2), ("★", 2)].

    Empty list when the record can't be ranked (missing dimensions or
    tombstoned) — mirrors rank_score returning None.
    """
    w = weights or DEFAULT_WEIGHTS
    if (
        record.tombstoned_at is not None
        or record.value is None
        or record.effort is None
        or record.agent_fitness is None
    ):
        return []
    parts = [
        (f"V{record.value}", record.value * w["value_multiplier"]),
        (record.effort.value, w["effort_bonus"][record.effort.value]),
        (record.agent_fitness.value, w["fitness_bonus"][record.agent_fitness.value]),
    ]
    if record.interest:
        parts.append(("★", w["interest_bonus"]))
    return parts


def rank_score(record: TriageRecord, weights: dict | None = None) -> int | None:
    parts = rank_breakdown(record, weights)
    return sum(p for _, p in parts) if parts else None


def load_rank_weights(data_dir: Path) -> dict:
    """Weights from <data_dir>/config.json deep-merged over DEFAULT_WEIGHTS.

    Missing/corrupt/partial files degrade to defaults — ranking must never
    fail because the data repo is in a weird state.
    """
    merged = copy.deepcopy(DEFAULT_WEIGHTS)
    try:
        data = json.loads((data_dir / "config.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return merged
    if not isinstance(data, dict):
        return merged
    raw = data.get("rank_weights")
    if not isinstance(raw, dict):
        return merged

    for key, val in raw.items():
        if key not in merged:
            continue
        if isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key].update({k: v for k, v in val.items() if k in merged[key] and _is_int(v)})
        elif _is_int(val):
            merged[key] = val
    return merged


# ---- Disk store (spec Section 1 "Store") ----


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
    tmp.replace(path)


class TriageStore:
    """Per-repo JSON triage records under <data_dir>/triage/.

    Mutations follow pinned_store.py conventions: flock around
    read-modify-write, read-or-raise so a corrupt file is never clobbered
    (AHQ-014), atomic temp+rename writes. Plain reads tolerate corruption
    by degrading to empty.
    """

    def __init__(self, data_dir: Path) -> None:
        # Kept public so callers can hand the same root to
        # load_rank_weights() without re-plumbing config (#247).
        self.data_dir = Path(data_dir)
        self._dir = self.data_dir / "triage"

    def _repo_file(self, repo: str) -> Path:
        # Owner-qualified: "ownerA/tools" and "ownerB/tools" must not share a
        # file (or its flock). owner__repo.json keeps names filesystem-safe.
        return self._dir / f"{repo.replace('/', '__')}.json"

    @staticmethod
    def _repo_of(issue_key: str) -> str:
        return issue_key.rsplit("#", 1)[0]

    @staticmethod
    def _records_from_raw(data: object, path: Path) -> tuple[dict[str, TriageRecord], dict]:
        """(parsed records, unparseable entries preserved verbatim).

        Malformed entries are skipped by readers but must survive writes
        untouched (AHQ-014 class: never destroy data we can't parse).
        """
        if not isinstance(data, dict):
            return {}, {}
        out: dict[str, TriageRecord] = {}
        unparsed: dict = {}
        for key, raw in data.items():
            try:
                out[key] = TriageRecord.from_dict(raw)
            except (KeyError, TypeError, AttributeError, ValueError):
                log.warning("preserving malformed triage record %s in %s", key, path)
                unparsed[key] = raw
        return out, unparsed

    def _read(self, path: Path) -> dict[str, TriageRecord]:
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return self._records_from_raw(data, path)[0]

    def _read_or_raise(self, path: Path) -> tuple[dict[str, TriageRecord], dict]:
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return {}, {}
        except OSError as exc:
            raise OSError(f"cannot read {path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OSError(f"{path} corrupt: {exc}") from exc
        if not isinstance(data, dict):
            # Valid JSON but not our schema (list/null root): treat as corrupt
            # on the mutating path so we never rebuild the file from empty.
            raise OSError(f"{path} root is not an object; refusing to overwrite")
        return self._records_from_raw(data, path)

    @staticmethod
    def _write(path: Path, records: dict[str, TriageRecord], unparsed: dict) -> None:
        payload = {**unparsed, **{k: r.to_dict() for k, r in records.items()}}
        _atomic_write_json(path, dict(sorted(payload.items())))

    def get(self, issue_key: str) -> TriageRecord | None:
        return self._read(self._repo_file(self._repo_of(issue_key))).get(issue_key)

    def all_records(self, include_tombstoned: bool = False) -> list[TriageRecord]:
        out: list[TriageRecord] = []
        if not self._dir.is_dir():
            return out
        for path in sorted(self._dir.glob("*.json")):
            for rec in self._read(path).values():
                if include_tombstoned or rec.tombstoned_at is None:
                    out.append(rec)
        return out

    def upsert(self, record: TriageRecord, *, actor: str) -> TriageRecord | None:
        """Insert/update a record. actor is one of ACTORS ("ai", "human", "duet").

        AI writes preserve human-provenance dimensions (spec Section 1) and
        never set interest or pin. Provenance is stamped per dimension the
        write actually supplies. Returns the record as stored, or ``None``
        when the write was skipped because the repo file is unreadable —
        callers must not report success on ``None`` (#247 review).
        """
        assert actor in ACTORS
        path = self._repo_file(self._repo_of(record.issue_key))
        with file_lock(path.with_suffix(path.suffix + ".lock")):
            try:
                records, unparsed = self._read_or_raise(path)
            except OSError as exc:
                log.error("cannot read %s, skipping triage write: %s", path, exc)
                return None
            existing = records.get(record.issue_key)
            stored = self._merge(existing, record, actor)
            records[record.issue_key] = stored
            self._write(path, records, unparsed)
            return stored

    def confirm(
        self,
        issue_key: str,
        *,
        now: str,
        expected_proposed_at: str | None = None,
    ) -> TriageRecord | None:
        """Flip one complete PROPOSED record to CONFIRMED (#248 Triage inbox).
        Dimensions, provenance ("ai" stays "ai" — the audit trail reads "AI
        proposed, human confirmed"), reasoning and proposal metadata are
        untouched; a ``pin_staged`` from the inbox row is folded into
        ``pin`` in the same write (#267). Idempotent on an already-confirmed record. None when
        missing, incomplete, tombstoned, or unreadable (``get`` degrades to
        {} on a corrupt file, so the None falls out naturally).

        ``expected_proposed_at`` is the revision the caller is approving —
        the ``proposed_at`` the card was rendered with. Confirm means
        "approve what I saw": when the worker re-proposed the issue after
        that paint (its body changed, so ``select_targets`` re-targeted it),
        the stored revision differs and the flip is refused (None) instead of
        silently signing off on dimensions the user never looked at. ``None``
        skips the check (legacy markup / non-inbox callers)."""
        self.confirm_many([issue_key], now=now, expected={issue_key: expected_proposed_at})
        rec = self.get(issue_key)
        if rec is None or rec.tombstoned_at is not None or rec.state is not TriageState.CONFIRMED:
            return None
        return rec

    def confirm_many(
        self,
        issue_keys: Iterable[str],
        *,
        now: str,
        expected: Mapping[str, str | None] | None = None,
    ) -> int:
        """Confirm-all: one lock + one write per repo file; returns records
        flipped. ``expected`` maps a key to the ``proposed_at`` revision the
        caller saw; a key whose stored revision differs is skipped (see
        ``confirm``). Keys absent from the map, or mapped to None, confirm
        unconditionally."""
        expected = dict(expected or {})
        by_repo: dict[str, list[str]] = {}
        for key in issue_keys:
            by_repo.setdefault(self._repo_of(key), []).append(key)
        flipped = 0
        for repo, keys in by_repo.items():
            path = self._repo_file(repo)
            with file_lock(path.with_suffix(path.suffix + ".lock")):
                try:
                    records, unparsed = self._read_or_raise(path)
                except OSError as exc:
                    log.error("cannot read %s, skipping triage confirm: %s", path, exc)
                    continue
                changed = False
                for key in keys:
                    rec = records.get(key)
                    if rec is None or rec.tombstoned_at is not None:
                        continue
                    if rec.state is TriageState.CONFIRMED:
                        continue
                    if any(getattr(rec, dim) is None for dim in DIMENSIONS):
                        continue
                    want = expected.get(key)
                    if want is not None and rec.proposed_at != want:
                        continue  # re-proposed since the caller looked — stale
                    rec.state = TriageState.CONFIRMED
                    if rec.pin_staged is not None:
                        rec.pin = rec.pin_staged  # the inbox row's staged pin commits here
                        rec.pin_staged = None
                    rec.updated_at = now
                    changed = True
                    flipped += 1
                if changed:
                    self._write(path, records, unparsed)
        return flipped

    @staticmethod
    def _merge(existing: TriageRecord | None, incoming: TriageRecord, actor: str) -> TriageRecord:
        merged = TriageRecord.from_dict(incoming.to_dict())  # copy
        prov = dict(existing.provenance) if existing else {}
        dims_supplied = False
        written: list[str] = []
        for dim in DIMENSIONS:
            if getattr(incoming, dim) is None:
                # Not supplied — a partial update keeps the existing value
                # (and its provenance) instead of wiping it.
                if existing is not None:
                    setattr(merged, dim, getattr(existing, dim))
            elif actor == "ai" and prov.get(dim) in NON_AI_ACTORS:
                setattr(merged, dim, getattr(existing, dim))  # human/duet wins
            else:
                dims_supplied = True
                prov[dim] = actor
                written.append(dim)
        merged.provenance = prov
        incoming_reasoning = incoming.reasoning
        if actor == "ai" and existing is not None:
            # A rejected AI value must not smuggle its reasoning in either —
            # human-owned dimensions keep their reasoning too.
            incoming_reasoning = {
                k: v
                for k, v in incoming.reasoning.items()
                if existing.provenance.get(k) not in NON_AI_ACTORS
            }
        base_reasoning = dict(existing.reasoning) if existing else {}
        if actor == "duet":
            # A Duet edit replaces the justification of every dimension it
            # wrote: without its own reasoning the old (AI) one-liner is
            # dropped, so it never sits beside a value it didn't argue for.
            for dim in written:
                base_reasoning.pop(dim, None)
        merged.reasoning = {**base_reasoning, **incoming_reasoning}
        # tombstoned_at is managed solely by apply_open_issue_set — an upsert
        # must never revive (or bury) a record as a side effect.
        if existing is not None:
            merged.tombstoned_at = existing.tombstoned_at
        # priority_hint is actor-agnostic metadata: None means "not supplied".
        if merged.priority_hint is None and existing is not None:
            merged.priority_hint = existing.priority_hint
        if actor == "ai":
            # AI never sets the human-only flags, and never downgrades a
            # human-confirmed record back to the inbox.
            merged.interest = existing.interest if existing else None
            merged.pin = existing.pin if existing else None
            merged.pin_staged = existing.pin_staged if existing else None
            merged.autopilot = existing.autopilot if existing else None
            if existing is not None and existing.state is TriageState.CONFIRMED:
                # A confirmed record is a human decision. ``confirm`` keeps
                # provenance "ai" (the audit trail reads "AI proposed, human
                # confirmed"), so the human-provenance guard above does NOT
                # protect it: a worker run still in flight when the user hit
                # Confirm would land afterwards and silently replace the
                # dimensions/reasoning that were just signed off on. Keep all
                # three plus provenance; proposal metadata (body_hash /
                # proposed_at / model) still refreshes from the incoming write.
                for dim in DIMENSIONS:
                    setattr(merged, dim, getattr(existing, dim))
                merged.provenance = dict(existing.provenance)
                merged.reasoning = dict(existing.reasoning)
                merged.state = TriageState.CONFIRMED
            elif dims_supplied:
                merged.state = TriageState.PROPOSED
            else:
                # Metadata-only AI write (e.g. a body_hash refresh): no
                # rankable content was proposed, so don't fake a proposal.
                merged.state = existing.state if existing else TriageState.UNTRIAGED
        else:
            # Human path: tri-state flags — None means "not supplied".
            if merged.interest is None and existing is not None:
                merged.interest = existing.interest
            if merged.autopilot is None and existing is not None:
                merged.autopilot = existing.autopilot
            if merged.pin_staged is not None and merged.pin is None:
                # ``pin_staged`` only ever lives on a PROPOSED record: a
                # staged write landing on a record confirmed meanwhile (the
                # inbox tap raced a Confirm elsewhere) or never proposed
                # becomes a direct pin — the row already cleared "takes
                # effect on Confirm", and an orphaned staged flag is one
                # ``confirm_many`` would skip forever. Decided here, under
                # the lock, because the route cannot check-and-write
                # atomically (#267 review, Codex round 2).
                live_proposal = (
                    existing is not None
                    and existing.tombstoned_at is None
                    and existing.state is TriageState.PROPOSED
                )
                if not live_proposal:
                    merged.pin = merged.pin_staged
                    merged.pin_staged = None
            if merged.pin is None and existing is not None:
                merged.pin = existing.pin
                if merged.pin_staged is None:
                    merged.pin_staged = existing.pin_staged
            elif merged.pin is not None:
                # An explicit pin write (Queue/sheet toggle) supersedes an
                # inbox-staged value — never replay the stale one on Confirm.
                merged.pin_staged = None
            # Human writes don't carry proposal metadata; preserve the AI's.
            if existing is not None:
                merged.proposed_at = incoming.proposed_at or existing.proposed_at
                merged.model = incoming.model or existing.model
                merged.body_hash = incoming.body_hash or existing.body_hash
            # A human write never changes state (#248 hand-test): editing a
            # chip is a correction, not an approval, so a PROPOSED record
            # stays in the inbox and an UNTRIAGED one stays untriaged until
            # the explicit ``confirm`` flips it. (Supersedes the #247 rule
            # "editing any dimension confirms" — it approved the two unseen
            # dims the moment the first was touched.)
            if existing is not None:
                merged.state = existing.state
            else:
                merged.state = TriageState.UNTRIAGED
        return merged

    def seed_missing(
        self, repo: str, numbered_hints: list[tuple[int, str | None]], *, now: str
    ) -> int:
        """Create untriaged records (with priority hints) for unseen issues.

        Existing records — any state, tombstoned included — are never
        touched. Returns the number of records created. One lock/write per
        repo file so first-run seeding doesn't take ~N upsert locks (#247).
        """
        path = self._repo_file(repo)
        with file_lock(path.with_suffix(path.suffix + ".lock")):
            try:
                records, unparsed = self._read_or_raise(path)
            except OSError as exc:
                log.error("cannot read %s, skipping seed pass: %s", path, exc)
                return 0
            created = 0
            for number, hint in numbered_hints:
                key = f"{repo}#{number}"
                if key in records or key in unparsed:
                    continue
                rec = TriageRecord.untriaged(key, updated_at=now)
                rec.priority_hint = hint
                records[key] = rec
                created += 1
            if created:
                self._write(path, records, unparsed)
            return created

    def apply_open_issue_set(self, repo: str, open_numbers: set[int], *, now: str) -> int:
        """Tombstone records whose issue closed; revive reopened ones."""
        path = self._repo_file(repo)
        with file_lock(path.with_suffix(path.suffix + ".lock")):
            try:
                records, unparsed = self._read_or_raise(path)
            except OSError as exc:
                log.error("cannot read %s, skipping tombstone pass: %s", path, exc)
                return 0
            changed = 0
            for key, rec in records.items():
                if self._repo_of(key) != repo:
                    continue
                try:
                    number = int(key.rsplit("#", 1)[1])
                except (ValueError, IndexError):
                    continue  # malformed key: degrade, never crash the pass
                is_open = number in open_numbers
                if is_open and rec.tombstoned_at is not None:
                    rec.tombstoned_at = None
                    changed += 1
                elif not is_open and rec.tombstoned_at is None:
                    rec.tombstoned_at = now
                    changed += 1
            if changed:
                self._write(path, records, unparsed)
            return changed
