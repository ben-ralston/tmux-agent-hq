"""`agent-hq recover` — diff a pre-crash snapshot against what's live. #125 #135

After a crash (tmux server death, reboot, OOM) some agents come back and some
don't. This command picks a *baseline* record from ``SnapshotStore``, diffs it
against the *currently* discovered agents, and lists each agent that was live
in the baseline but isn't now — with a ready-to-run ``claude --resume <id>`` in
its ``<cwd>`` for Claude agents. It productizes the manual post-crash forensics
that motivated #125.

The baseline is NOT simply the last snapshot: the daemon keeps polling after
tmux-resurrect restores the survivors, so by the time ``recover`` runs the last
record already reflects the post-restore world and the dead agent is missing
from *both* sides of the diff (#135, the 2026-06-07 crash). Instead
``select_baseline`` scans backwards for the most recent sharp roster collapse
and uses the last record *before* it; ``--since`` / ``--baseline`` override the
heuristic. The chosen baseline (and why) is surfaced in the output so a wrong
pick is visible instead of silent.

The session invoking ``recover`` (its ``$TMUX_PANE``) is excluded from both
sides of the diff: it is never a recovery target and would otherwise mask a
missing agent (#135). Other sessions in agent-hq's own checkout are real work
and are never hidden: a missing one is listed in its own "possibly self,
verify" section, since it may also be an earlier control session (#410).

A Claude record captured without a session id gets one from its transcript on
disk when exactly one unclaimed transcript fits (#410); otherwise the
candidates are listed.

The diff is identity-based: an agent is keyed by its stable session id (a
Claude transcript session id, or a Codex thread UUID once captured — stable
across a resume), else by ``tool:cwd``. An agent counts as "live" if discovery
finds it now, regardless of state. Membership is compared, never counts.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from agent_hq.adapters import ResumeSpec, get_adapter
from agent_hq.models import Agent, AgentType
from agent_hq.snapshots import SnapshotStore
from agent_hq.transcript import transcript_path_for

# Only roster collapses this recent are treated as "the crash" — an ancient
# cliff that everything already recovered from must not hijack the baseline.
CLIFF_LOOKBACK_SECONDS = 48 * 3600

# A crash is recorded across many poll ticks (the daemon polls every ~2 s and
# agents die over seconds), so the collapse is judged over the monotonic
# non-increasing run of records ending at each candidate — never over a
# single snapshot-to-snapshot step. The 2026-09-13 reboot went 14→1 as a
# 15-record staircase with no single step dropping both ≥ 2 and ≥ half
# (#274). This window bounds the DROP records that merge into one crash: the
# store appends within a tick of a change, so a record's timestamp is its
# drop time, and drops within the window of the bottom are one event. The
# record before the earliest merged drop is the pre-crash peak whatever its
# age — an idle roster is deduplicated, so that record is typically hours old.
# Known gap (code-review of #274, pre-existing): the window is anchored on the
# bottom record, so a daemon that dies mid-collapse and writes the collapsed
# roster only after a long reboot loses the pre-death drops to the window and
# baselines at the last pre-death tick. Time alone can't tell that shape from
# two unrelated exits 15 min apart, so `--baseline N` is the override.
CLIFF_MERGE_WINDOW_SECONDS = 300

# A sessionless Claude record is matched to its transcript by last write
# (#410). Transcripts last written within this window of the newest candidate
# can't be told apart by time, so they are all listed instead of guessing.
TRANSCRIPT_AMBIGUITY_SECONDS = 300
MAX_TRANSCRIPT_CANDIDATES = 5

_RELATIVE_SINCE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd])", re.IGNORECASE)
_SINCE_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Sentinel for run_recover kwargs whose production default is computed (repo
# root / $TMUX_PANE) but which tests inject explicitly — including None.
_AUTO = object()


@dataclass
class TranscriptCandidate:
    """A transcript that may belong to a sessionless missing agent. #410"""

    session_id: str
    mtime: float
    resume_command: str | None


@dataclass
class MissingAgent:
    """A snapshot agent that is no longer live, plus its resume command (if any).

    ``resolved_session_id`` is set when the record had no session id and one
    was resolved from its transcript (``transcript_mtime`` is that file's last
    write); ``candidates`` lists the transcripts when the match is ambiguous.
    """

    record: dict
    resume_command: str | None
    resolved_session_id: str | None = None
    transcript_mtime: float | None = None
    candidates: list[TranscriptCandidate] = field(default_factory=list)


@dataclass
class Baseline:
    """The snapshot record chosen as the pre-crash roster, and why. #135"""

    record: dict | None
    reason: str
    newer_records: int  # records in the store after the baseline
    # When agents began dying: the first record after the baseline in a
    # DETECTED collapse. None for an override or fallback pick, whose next
    # record may be an ordinary state update rather than the crash. #410
    collapse_ts: float | None = None


def identity(session_id: str | None, tool: str | None, cwd: str | None) -> str:
    """Stable cross-restart identity: transcript session id, else ``tool:cwd``."""
    return session_id if session_id else f"{tool}:{cwd}"


def record_identity(rec: dict) -> str:
    """Identity for a snapshot record: generic ``stable_id``, else legacy ``session_id``.

    Task 4 started writing ``stable_id`` alongside ``session_id`` on every
    record (equal values today, but ``stable_id`` is the harness-generic key
    going forward — e.g. a Codex thread UUID). Older on-disk records predate
    ``stable_id`` entirely, hence the ``session_id`` fallback.
    """
    return identity(rec.get("stable_id") or rec.get("session_id"), rec.get("tool"), rec.get("cwd"))


def resume_spec_for(rec: dict) -> ResumeSpec | None:
    """The record's harness-specific resume spec (cwd + argv), via its adapter."""
    adapter = get_adapter(rec.get("tool", ""))
    return adapter.resume_spec(rec) if adapter else None


def resume_command(rec: dict) -> str | None:
    """Shell-quoted resume invocation for ANY harness with a resume spec, else None."""
    spec = resume_spec_for(rec)
    if spec is None:
        return None
    return f"(cd {shlex.quote(spec.cwd)} && {shlex.join(spec.argv)})"


def parse_since(value: str, *, now: float) -> float:
    """``--since`` value → epoch seconds: relative (``45m``/``2h``/``1d``) or ISO."""
    m = _RELATIVE_SINCE_RE.fullmatch(value.strip())
    if m:
        return now - float(m.group(1)) * _SINCE_UNIT_SECONDS[m.group(2).lower()]
    try:
        return datetime.fromisoformat(value.strip()).timestamp()
    except ValueError:
        raise ValueError(
            f"invalid --since value {value!r}: use a duration like '45m', '2h', '1d' "
            "or an ISO datetime like '2026-06-07 14:30'"
        ) from None


def _matches_invoker(rec: dict, invoking_pane: str | None) -> bool:
    """Whether an identity match with the invoking agent proves it is the same session.

    A record with its own session id matched by that id. A sessionless one
    matched only through the ``tool:cwd`` fallback, which every sessionless
    agent in that cwd shares, so only the invoking pane's own record counts
    (code-review and Codex review of #410).
    """
    return bool(rec.get("stable_id") or rec.get("session_id")) or (
        invoking_pane is not None and rec.get("pane") == invoking_pane
    )


def _agent_count(
    rec: dict, self_ids: set[str] | None = None, self_pane: str | None = None
) -> int:
    """Number of agents in a record, ignoring the invoking session.

    Baseline selection must not count the session running ``recover``: it
    joins the roster post-crash and would otherwise pad the collapsed count
    past the cliff gate, masking the crash (#135). Only its identity is
    discounted — every other session, the agent-hq main checkout's included,
    counts, so a crash that mostly killed main-checkout sessions is still a
    collapse (#410, code-review). A sessionless invoker is discounted only in
    its own pane (``self_pane``), see ``_matches_invoker``.
    """
    agents = rec.get("agents") or []
    if not self_ids:
        return len(agents)
    return sum(
        1
        for a in agents
        if isinstance(a, dict)
        and not (record_identity(a) in self_ids and _matches_invoker(a, self_pane))
    )


def select_baseline(
    records: list[dict],
    *,
    now: float,
    since_ts: float | None = None,
    baseline_back: int | None = None,
    self_ids: set[str] | None = None,
    self_pane: str | None = None,
) -> Baseline:
    """Pick the pre-crash roster to diff against the live set. #135

    Priority: ``--baseline`` override > ``--since`` override > cliff heuristic
    (most recent collapse of ≥ half the roster and ≥ 2 agents within
    ``CLIFF_LOOKBACK_SECONDS``, judged over the whole non-increasing run of
    records within ``CLIFF_MERGE_WINDOW_SECONDS`` — a staggered decline
    counts as one collapse, equal-count state-only records included — and
    baselining at the run's start) > last record with non-self agents (the
    #125 behavior; its reason names the largest windowed drop seen, so a
    collapse the thresholds rejected is visible). All counts ignore the
    invoking session (``self_ids`` / ``self_pane``).
    """
    if baseline_back is not None:
        if not 0 <= baseline_back < len(records):
            raise ValueError(
                f"--baseline {baseline_back} is out of range "
                f"({len(records)} snapshot record(s) on disk)"
            )
        idx = len(records) - 1 - baseline_back
        return Baseline(records[idx], f"selected by --baseline {baseline_back}", baseline_back)

    if since_ts is not None:
        nonempty = [(i, r) for i, r in enumerate(records) if _agent_count(r, self_ids, self_pane)]
        if not nonempty:
            return Baseline(None, "no snapshots", 0)
        at_or_before = [
            (i, r)
            for i, r in nonempty
            if (ts := _timestamp(r)) is not None and ts <= since_ts
        ]
        idx, rec = at_or_before[-1] if at_or_before else nonempty[0]
        return Baseline(rec, "selected by --since", len(records) - 1 - idx)

    counts = [_agent_count(r, self_ids, self_pane) for r in records]
    largest_drop: tuple[int, int] | None = None  # (peak, bottom) of the biggest windowed drop
    for i in range(len(records) - 1, 0, -1):
        ts = _timestamp(records[i])
        if ts is None:
            continue  # age unknown (malformed line): not a candidate, but keep scanning
        if ts < now - CLIFF_LOOKBACK_SECONDS:
            break
        if counts[i] >= counts[i - 1]:
            # Only a record that is itself a drop can be the collapse bottom: a
            # state-only record inside the lookback must not revive a collapse
            # whose drops all landed outside it (Codex review of #274).
            continue
        j = _run_start(records, counts, i)
        n_peak, n_cur = counts[j], counts[i]
        if n_peak - n_cur >= 2 and n_cur * 2 <= n_peak:
            # Baseline at the NEWEST record of the peak count: an equal-count
            # record can be membership churn ([A,B,C] → [A,B,D]), and the
            # newest one holds the agents that were actually live at the crash
            # (plus the freshest state/phase for a state-only plateau).
            while counts[j + 1] == n_peak:
                j += 1
            return Baseline(
                records[j],
                f"roster collapse {n_peak}→{n_cur} detected",
                len(records) - 1 - j,
                collapse_ts=_timestamp(records[j + 1]),
            )
        if n_peak > n_cur and (
            largest_drop is None or n_peak - n_cur > largest_drop[0] - largest_drop[1]
        ):
            largest_drop = (n_peak, n_cur)

    for i in range(len(records) - 1, -1, -1):
        if counts[i]:
            reason = "latest snapshot" if i == len(records) - 1 else "last non-empty snapshot"
            if largest_drop is not None:
                peak, bottom = largest_drop
                reason += f" (no roster collapse; largest drop seen {peak}→{bottom})"
            return Baseline(records[i], reason, len(records) - 1 - i)
    return Baseline(None, "no snapshots", 0)


def _timestamp(rec: dict) -> float | None:
    """Record timestamp as a finite float; ``None`` when missing or malformed.

    The store only ever writes ``time.time()``, but a hand-edited or truncated
    line must degrade to "age unknown", never a ``TypeError`` mid-scan — and
    ``"Infinity"`` must not read as newer than every cutoff.
    """
    try:
        ts = float(rec.get("timestamp"))
    except (TypeError, ValueError):
        return None
    return ts if math.isfinite(ts) else None


def _run_start(records: list[dict], counts: list[int], i: int) -> int:
    """Index where the non-increasing run of counts ending at ``records[i]`` starts.

    ``records[i]`` is a drop from ``records[i-1]`` (the caller guarantees it).
    Walking back from there, the record before a drop joins the run while it
    holds at least as many agents (equal-count state-only records don't
    break the run) and the drop it precedes happened within
    ``CLIFF_MERGE_WINDOW_SECONDS`` of the bottom. The window bounds the DROP
    records, never the peak: the store appends within a tick of a change, so
    a record's timestamp is its drop time, and the record before the earliest
    merged drop is the pre-crash roster however long it sat idle (deduped
    for hours in the 2026-06-07 shape, #135, and before the 2026-09-13
    staircase, #274 — both review rounds). The collapse is then judged as
    ``counts[start] → counts[i]``, so a staggered decline qualifies as a
    whole even when no single step does. An age-unknown drop record ends the
    walk — the window can't be judged against it.
    """
    bottom_ts = _timestamp(records[i])
    j = i
    while j > 0 and counts[j - 1] >= counts[j]:
        drop_ts = _timestamp(records[j])
        if j < i and (
            bottom_ts is None
            or drop_ts is None
            or bottom_ts - drop_ts > CLIFF_MERGE_WINDOW_SECONDS
        ):
            break
        j -= 1
    return j


def self_repo_root() -> str | None:
    """Root of the checkout this agent_hq package runs from (editable install).

    ``None`` for a non-editable (site-packages) install — self-cwd filtering
    is then skipped, leaving only the ``$TMUX_PANE`` exclusion.
    """
    root = Path(__file__).resolve().parents[1]
    return str(root) if (root / ".git").exists() else None


def _is_self_cwd(cwd: str | None, self_root: str | None) -> bool:
    if not cwd or not self_root:
        return False
    try:
        cwd_path = Path(cwd).resolve()
    except OSError:
        return False
    root = Path(self_root)
    return cwd_path == root or root in cwd_path.parents


def exclude_self(
    record: dict | None,
    live_agents: list[Agent],
    *,
    invoking_pane: str | None,
) -> tuple[dict | None, list[Agent], int]:
    """Drop the session invoking ``recover`` from both sides of the diff. #135 #410

    A live agent is "self" only if it sits in the pane invoking ``recover``
    (``$TMUX_PANE`` — exact). Baseline records are dropped when their identity
    matches that excluded live agent (the invoking session may have resumed a
    pre-crash session id). Identity matches consume excluded live slots as a
    multiset — mirroring ``compute_missing`` — so a session-less sibling
    sharing the invoking agent's ``tool:cwd`` identity is still reported. A
    sessionless invoking agent matches only through that fallback identity,
    which can't prove a record is the same session, so it consumes a record
    only from its own pane (code-review of #410).

    Nothing is excluded by cwd: other sessions in agent-hq's own checkout are
    real work (#410 — the 2026-10-29 reboot hid one). ``split_possibly_self``
    flags the missing ones for a human to verify instead.

    Returns ``(filtered_record, filtered_live, n_excluded)`` where
    ``n_excluded`` counts unique excluded identities (an agent present on both
    sides is one exclusion, not two).
    """
    excluded_ids: set[str] = set()
    live_excluded = Counter()
    kept_live: list[Agent] = []
    for agent in live_agents:
        if invoking_pane is not None and agent.pane_id == invoking_pane:
            aid = identity(agent.session_metadata.stable_id, agent.agent_type.value, agent.cwd)
            excluded_ids.add(aid)
            live_excluded[aid] += 1
        else:
            kept_live.append(agent)
    if record is not None:
        kept_recs: list[dict] = []
        for rec in record.get("agents", []):
            rid = record_identity(rec)
            if live_excluded[rid] > 0 and _matches_invoker(rec, invoking_pane):
                live_excluded[rid] -= 1  # consume one excluded live slot
            else:
                kept_recs.append(rec)
        record = {**record, "agents": kept_recs}
    return record, kept_live, len(excluded_ids)


def split_possibly_self(
    missing: list[MissingAgent], self_root: str | None
) -> tuple[list[MissingAgent], list[MissingAgent]]:
    """Split out missing agents whose cwd is inside agent-hq's own checkout. #410

    Such an agent may be an earlier agent-hq control session (replaced after
    the crash, #135) or real work in the main checkout (#410). The cwd can't
    tell the two apart, so it is reported separately for a human to verify —
    never hidden, never mixed in with the recovery targets.
    """
    if not self_root:
        return missing, []
    plain: list[MissingAgent] = []
    possibly: list[MissingAgent] = []
    for m in missing:
        (possibly if _is_self_cwd(m.record.get("cwd"), self_root) else plain).append(m)
    return plain, possibly


def _with_session(rec: dict, session_id: str) -> dict:
    return {**rec, "session_id": session_id, "stable_id": session_id}


def _transcripts_for(cwd: str, cutoff: float) -> list[tuple[float, str]]:
    """``(mtime, session_id)`` of Claude transcripts for ``cwd``, newest first.

    Only transcripts last written at or before ``cutoff`` count: a later write
    belongs to a session that started after the crash. ``transcript_path_for``
    owns the cwd encoding (including the legacy underscore directory), so it
    is asked for paths with a placeholder id and only the directories are kept.
    Top-level ``*.jsonl`` only — subagent transcripts live in subdirectories.
    """
    found: dict[str, float] = {}
    for directory in dict.fromkeys(Path(p).parent for p in transcript_path_for(cwd, "_")):
        try:
            paths = list(directory.glob("*.jsonl"))
        except OSError:
            continue
        for path in paths:
            if path.stem in found:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime <= cutoff:
                found[path.stem] = mtime
    return sorted(((mtime, sid) for sid, mtime in found.items()), reverse=True)


def _transcript_dir_key(cwd: str) -> Path:
    """The current-scheme transcript directory for ``cwd``.

    Distinct cwds can encode to the same directory (``/x/foo-bar`` and
    ``/x/foo_bar``), so siblings are counted per directory, not per cwd —
    otherwise each could claim the same transcript (Codex review of #410).
    """
    return Path(transcript_path_for(cwd, "_")[0]).parent


def _slot(rec: dict) -> tuple:
    """Where an agent ran — the closest thing a sessionless record has to an id."""
    return (rec.get("pane"), rec.get("tool"), rec.get("cwd"))


def _history_ids(records: list[dict]) -> dict[tuple, set[str]]:
    """Every session id the store has recorded, by the slot it was seen in."""
    by_slot: dict[tuple, set[str]] = {}
    for record in records:
        for a in record.get("agents") or []:
            if isinstance(a, dict) and (sid := a.get("stable_id") or a.get("session_id")):
                by_slot.setdefault(_slot(a), set()).add(sid)
    return by_slot


def _first_seen(records: list[dict], baseline_index: int, rec: dict) -> float | None:
    """When the agent's slot first appeared in the run of records ending at the baseline."""
    slot = _slot(rec)
    seen: float | None = None
    for k in range(baseline_index, -1, -1):
        agents = records[k].get("agents") or []
        if not any(isinstance(a, dict) and _slot(a) == slot for a in agents):
            break
        seen = _timestamp(records[k]) or seen
    return seen


def resolve_from_transcripts(
    missing: list[MissingAgent],
    *,
    claimed: set[str],
    cutoff: float,
    unidentified_live: dict[str, int] | None = None,
    records: list[dict] | None = None,
    baseline_index: int | None = None,
) -> list[MissingAgent]:
    """Give sessionless missing Claude agents a session id from their transcript. #410

    A Claude record captured with no session id has no resume command. Its
    transcript is still on disk under ``~/.claude/projects/<encoded-cwd>/``:
    the candidates are that directory's transcripts not ``claimed`` by a known
    session (every live agent and every baseline record) and last written at
    or before ``cutoff`` (around the crash). When exactly one is newest by
    more than ``TRANSCRIPT_AMBIGUITY_SECONDS`` and the record is the only
    sessionless Claude agent in its cwd, it is used and marked resolved.
    Otherwise every close candidate is listed (newest first, at most
    ``MAX_TRANSCRIPT_CANDIDATES``, or one per sibling) — with several sessionless agents in one
    cwd, which pane wrote which transcript is unknowable, so the newest one
    per agent plus any within the window are offered to all of them.

    ``unidentified_live`` counts live Claude agents per cwd that have no
    session id (the invoking one included). Their transcripts can't be
    claimed, so each one counts as a sibling: otherwise a survivor's
    transcript could be "resolved" as the dead agent's (Codex review).

    With the store's ``records`` (and the baseline's index in them), two more
    guards apply (code-review of #410): an id recorded for a DIFFERENT slot
    (pane, tool, cwd) in any record is that session's, so it is claimed —
    the same slot's earlier ids stay eligible, since that may be this very
    session before it lost its id; and a transcript last written before the
    agent's slot first appeared in the run leading to the baseline predates
    the agent, so it is only ever a candidate, never a confident match.
    """

    def needs_resolution(rec: dict) -> bool:
        return (
            rec.get("tool") == AgentType.CLAUDE.value
            and not (rec.get("stable_id") or rec.get("session_id"))
            and bool(rec.get("cwd"))
        )

    per_dir = Counter(
        _transcript_dir_key(m.record["cwd"]) for m in missing if needs_resolution(m.record)
    )
    for live_cwd, n in (unidentified_live or {}).items():
        per_dir[_transcript_dir_key(live_cwd)] += n
    history = _history_ids(records) if records else {}
    all_history = set().union(*history.values()) if history else set()
    listings: dict[str, list[tuple[float, str]]] = {}
    resolved: list[MissingAgent] = []
    for m in missing:
        rec = m.record
        if not needs_resolution(rec):
            resolved.append(m)
            continue
        cwd = rec["cwd"]
        if cwd not in listings:
            listings[cwd] = _transcripts_for(cwd, cutoff)
        taken = claimed | (all_history - history.get(_slot(rec), set()))
        found = [(mtime, sid) for mtime, sid in listings[cwd] if sid not in taken]
        siblings = per_dir[_transcript_dir_key(cwd)]
        if not found:
            resolved.append(m)
            continue
        floor = found[min(siblings, len(found)) - 1][0] - TRANSCRIPT_AMBIGUITY_SECONDS
        close = [(mtime, sid) for mtime, sid in found if mtime >= floor]
        seen = (
            _first_seen(records, baseline_index, rec)
            if records and baseline_index is not None
            else None
        )
        if siblings == 1 and len(close) == 1 and (seen is None or close[0][0] >= seen):
            mtime, sid = close[0]
            resolved.append(
                replace(
                    m,
                    resume_command=resume_command(_with_session(rec, sid)),
                    resolved_session_id=sid,
                    transcript_mtime=mtime,
                )
            )
            continue
        candidates = [
            TranscriptCandidate(sid, mtime, resume_command(_with_session(rec, sid)))
            for mtime, sid in close[: max(MAX_TRANSCRIPT_CANDIDATES, siblings)]
        ]
        resolved.append(replace(m, candidates=candidates))
    return resolved


def compute_missing(baseline_record: dict | None, live_agents: list[Agent]) -> list[MissingAgent]:
    """Baseline agents whose identity is absent from the live set.

    Identities are matched as a multiset, not a set: two session-less agents
    sharing a ``tool:cwd`` fallback identity (e.g. two Codex agents in one cwd)
    each consume one live slot, so a dead one is still reported when only a
    sibling survived. #125

    A record's own stable-id identity can fail to match even though the SAME
    agent is still live: a codex thread id is captured after the pane starts
    (and can later flap back to ``None`` — dedupe, 3-day rollout cutoff), so
    a snapshot taken across that boundary records a UUID the live agent
    doesn't currently carry. When the direct identity lookup misses, retry
    against the record's ``tool:cwd`` fallback identity in the SAME
    ``live_counts`` multiset (not a separate pool) — an uncaptured live
    agent's own identity already lives at that key, so this can't double-
    consume a live agent already claimed by a different record (#205 fix
    wave Fix 3).
    """
    if not baseline_record:
        return []
    live_counts = Counter(
        identity(a.session_metadata.stable_id, a.agent_type.value, a.cwd) for a in live_agents
    )
    missing: list[MissingAgent] = []
    for rec in baseline_record.get("agents", []):
        rid = record_identity(rec)
        if live_counts[rid] > 0:
            live_counts[rid] -= 1  # this snapshot agent is accounted for by a live one
            continue
        fallback_id = identity(None, rec.get("tool"), rec.get("cwd"))
        if fallback_id != rid and live_counts[fallback_id] > 0:
            live_counts[fallback_id] -= 1  # accounted for by an uncaptured live agent
            continue
        missing.append(MissingAgent(record=rec, resume_command=resume_command(rec)))
    return missing


def _humanize_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _context_line(rec: dict) -> str:
    parts = [p for p in (rec.get("repo"), rec.get("branch")) if p]
    loc = "/".join(parts) if parts else (rec.get("cwd") or "?")
    issue = f" (#{rec['issue']})" if rec.get("issue") else ""
    state = rec.get("state") or "?"
    phase = rec.get("phase") or "—"
    return f"{loc}{issue} — was {state}/{phase}"


def _baseline_header(baseline: Baseline, *, now: float, self_excluded: int) -> str:
    rec = baseline.record
    assert rec is not None
    recorded = len(rec.get("agents", []))
    age = _humanize_age(now - rec.get("timestamp", now))
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec.get("timestamp", now)))
    notes = [baseline.reason]
    if baseline.newer_records:
        notes.append(f"{baseline.newer_records} newer record(s) skipped")
    if self_excluded:
        notes.append(f"{self_excluded} invoking session(s) excluded")
    return f"Baseline: {when} ({age}) — {recorded} agent(s) recorded ({'; '.join(notes)})."


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _entry_lines(m: MissingAgent) -> list[str]:
    lines = [f"  {_context_line(m.record)}"]
    if m.resume_command and m.resolved_session_id:
        lines.append(
            f"    {m.resume_command}  (resolved from transcript, last written "
            f"{_when(m.transcript_mtime or 0.0)})"
        )
    elif m.resume_command:
        lines.append(f"    {m.resume_command}")
    elif m.candidates:
        lines.append(
            f"    no session id recorded; {len(m.candidates)} candidate transcript(s) "
            "(newest first) — pick the right one:"
        )
        for c in m.candidates:
            lines.append(f"      {c.resume_command}  (last written {_when(c.mtime)})")
    else:
        tool = m.record.get("tool")
        cwd = m.record.get("cwd")
        lines.append(f"    no resume id (tool={tool}); cwd: {cwd}")
    lines.append("")
    return lines


def format_report(
    baseline: Baseline,
    missing: list[MissingAgent],
    *,
    now: float,
    self_excluded: int = 0,
    possibly_self: list[MissingAgent] | None = None,
) -> str:
    """Human-readable recovery report."""
    possibly_self = possibly_self or []
    if baseline.record is None:
        return "No snapshots recorded yet. Is the agent-hq daemon running?"

    recorded = len(baseline.record.get("agents", []))
    header = _baseline_header(baseline, now=now, self_excluded=self_excluded)

    if recorded == 0:
        return f"{header}\n\nBaseline snapshot has no live agents."

    if not missing and not possibly_self:
        return (
            f"{header}\n\nAll {recorded} agent(s) from the baseline are "
            "still live. Nothing to recover."
        )

    lines = [header, ""]
    if missing:
        lines += [f"{len(missing)} agent(s) from the baseline are no longer live:", ""]
        for m in missing:
            lines += _entry_lines(m)
    if possibly_self:
        lines += [
            f"{len(possibly_self)} agent(s) in agent-hq's own checkout are no longer live. "
            "Possibly self (an earlier agent-hq session) — verify before resuming:",
            "",
        ]
        for m in possibly_self:
            lines += _entry_lines(m)
    return "\n".join(lines).rstrip()


def _resume_payload(rec: dict) -> dict | None:
    spec = resume_spec_for(rec)
    return {"cwd": spec.cwd, "argv": list(spec.argv)} if spec else None


def _missing_entry(m: MissingAgent) -> dict:
    rec = m.record
    resume_rec = _with_session(rec, m.resolved_session_id) if m.resolved_session_id else rec
    return {
        **rec,
        "resume_command": m.resume_command,
        "resume": _resume_payload(resume_rec),
        "resolved_session_id": m.resolved_session_id,
        "transcript_mtime": m.transcript_mtime,
        "transcript_candidates": [
            {
                "session_id": c.session_id,
                "mtime": c.mtime,
                "resume_command": c.resume_command,
                "resume": _resume_payload(_with_session(rec, c.session_id)),
            }
            for c in m.candidates
        ],
    }


def format_json(
    baseline: Baseline,
    missing: list[MissingAgent],
    *,
    self_excluded: int = 0,
    possibly_self: list[MissingAgent] | None = None,
) -> str:
    """Machine-readable recovery report.

    ``possibly_self`` is a separate key so a script that resumes every
    ``missing`` entry never resumes an earlier agent-hq session unseen (#410).
    """
    possibly_self = possibly_self or []
    rec = baseline.record
    return json.dumps(
        {
            "snapshot_timestamp": rec.get("timestamp") if rec else None,
            "recorded": len(rec.get("agents", [])) if rec else 0,
            "baseline_reason": baseline.reason,
            "newer_records": baseline.newer_records,
            "self_excluded": self_excluded,
            "missing": [_missing_entry(m) for m in missing],
            "possibly_self": [_missing_entry(m) for m in possibly_self],
        },
        indent=2,
    )


def _transcript_cutoff(baseline: Baseline, *, now: float) -> float:
    """Latest last-write a dead agent's transcript can have. #410

    For a detected collapse, ``collapse_ts`` is when agents began dying, and
    a staircase merges drops for ``CLIFF_MERGE_WINDOW_SECONDS`` after that,
    so an agent that died late in it may have written up to then. An override
    or fallback baseline has no known crash time (its next record may be an
    ordinary state update, Codex review), so ``now`` is used.
    """
    if baseline.collapse_ts is not None:
        return baseline.collapse_ts + CLIFF_MERGE_WINDOW_SECONDS
    return now


def run_recover(
    *,
    as_json: bool = False,
    store: SnapshotStore | None = None,
    live_agents: list[Agent] | None = None,
    out=None,
    now: float | None = None,
    since: str | None = None,
    baseline_back: int | None = None,
    self_root: str | None = _AUTO,  # type: ignore[assignment]
    invoking_pane: str | None = _AUTO,  # type: ignore[assignment]
) -> int:
    """CLI handler: diff the chosen baseline vs the live set and print the report."""
    store = store or SnapshotStore()
    if live_agents is None:
        from agent_hq.discovery import discover_agents
        from agent_hq.enrichment import enrich_all_agents

        live_agents = enrich_all_agents(discover_agents())
    out = out or sys.stdout
    now = now if now is not None else time.time()
    if self_root is _AUTO:
        self_root = self_repo_root()
    if invoking_pane is _AUTO:
        invoking_pane = os.environ.get("TMUX_PANE")

    records = store.records()
    # The invoking session's identity, discounted when picking the baseline.
    self_ids = {
        identity(a.session_metadata.stable_id, a.agent_type.value, a.cwd)
        for a in live_agents
        if invoking_pane is not None and a.pane_id == invoking_pane
    }
    try:
        since_ts = parse_since(since, now=now) if since is not None else None
        baseline = select_baseline(
            records,
            now=now,
            since_ts=since_ts,
            baseline_back=baseline_back,
            self_ids=self_ids,
            self_pane=invoking_pane,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=out)
        return 2

    # Every session id already known — live or in the baseline, self included —
    # owns its transcript, so none of them can be a sessionless agent's. #410
    claimed = {a.session_metadata.stable_id for a in live_agents} | {
        r.get("stable_id") or r.get("session_id")
        for r in (baseline.record or {}).get("agents", [])
    }
    claimed.discard(None)
    unidentified_live = Counter(
        a.cwd
        for a in live_agents
        if a.agent_type is AgentType.CLAUDE and not a.session_metadata.stable_id and a.cwd
    )
    filtered_record, filtered_live, self_excluded = exclude_self(
        baseline.record, live_agents, invoking_pane=invoking_pane
    )
    baseline = replace(baseline, record=filtered_record)
    missing = resolve_from_transcripts(
        compute_missing(filtered_record, filtered_live),
        claimed=claimed,
        cutoff=_transcript_cutoff(baseline, now=now),
        unidentified_live=unidentified_live,
        records=records,
        baseline_index=len(records) - 1 - baseline.newer_records,
    )
    missing, possibly_self = split_possibly_self(missing, self_root)
    rendered = (
        format_json(baseline, missing, self_excluded=self_excluded, possibly_self=possibly_self)
        if as_json
        else format_report(
            baseline,
            missing,
            now=now,
            self_excluded=self_excluded,
            possibly_self=possibly_self,
        )
    )
    print(rendered, file=out)
    return 0
