"""Phase-snapshot resolver — the single boundary that turns hook state +
overrides into a ``PhaseSnapshot``.

Pure read. No disk writes, no state-file mutation. Mutating concerns
live elsewhere:

* Hook writes happen in ``hooks.handle_hook_event`` (per-pane flock).
* Override writes happen in ``overrides.write_override`` /
  ``delete_override`` (overrides flock).
* Cleanup happens in the daemon's poll loop via
  ``hooks.prune_stale_hook_state_files`` and
  ``overrides.prune_stale_overrides``.

The resolver itself just reads, applies the 5-state override matrix,
and assembles a ``PhaseSnapshot``. Three producers call it:

* **Daemon**: every ~2 s tick, for every discovered agent
  (``producer="daemon"``).
* **Watcher**: between cache ticks, on hook-file change or override
  POST (``producer="watcher"``).
* **Live fallback**: ``cache.get_agents`` when the daemon's cache is
  unavailable (``producer="live"``).

All three converge on this one function so ``Agent.phase_snapshot``
always has the same shape regardless of which path produced it.
"""

from __future__ import annotations

import logging
import os
import time

from .adapters import get_adapter
from .hooks import read_hook_revision
from .models import (
    Agent,
    AgentState,
    AgentType,
    DevPhase,
    InputsObserved,
    PhaseFeedback,
    PhaseProducer,
    PhaseSnapshot,
    PhaseSource,
    normalize_phase_value,
    session_match_state,
)
from .phase import detect_branch_phase
from .transcript import transcript_path_for

log = logging.getLogger(__name__)


def _phase_from_string(value: str | None) -> DevPhase | None:
    """Coerce a stored phase string (incl. legacy aliases) to the enum."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return DevPhase(normalize_phase_value(value))
    except ValueError:
        return None


def _feedback_from_entry(
    entry: dict, current_session_id: str | None, *, lenient_none: bool = False
) -> PhaseFeedback | None:
    """Build a ``PhaseFeedback`` from a stored overrides.json entry.

    Returns ``None`` when the entry shouldn't apply per the shared
    ``session_match_state`` matrix (``models.session_match_state``) on
    ``session_id``:

    | stored / current                | result                          |
    |----------------------------------|---------------------------------|
    | match (both non-None, or both None) | apply                       |
    | exactly one None, ``lenient_none``  | apply (codex id flap)       |
    | exactly one None, not lenient       | None — transient bridge-id |
    | both non-None, differ               | None — leak; prune drops it|
    | no ``session_id`` key                | None — legacy migration debris |

    ``lenient_none`` is passed by callers as ``True`` for codex agents
    (``resolve_phase_snapshot``, keyed on ``agent.agent_type``) since a
    codex thread id can flap to/from ``None`` in ordinary operation,
    unlike a claude bridge id.

    ``action`` must be ``"override"`` or ``"confirm"`` — unknown
    actions are treated as no feedback.
    """
    if not isinstance(entry, dict):
        return None
    if "session_id" not in entry:
        return None
    stored = entry.get("session_id")
    if session_match_state(stored, current_session_id, lenient_none=lenient_none) != "match":
        return None
    action = entry.get("action")
    if action not in ("override", "confirm"):
        return None
    return PhaseFeedback(
        action=action,
        phase=_phase_from_string(entry.get("phase")),
        auto_phase=_phase_from_string(entry.get("auto_phase")),
        set_at=str(entry.get("set_at", "")),
        note=entry.get("note"),
        session_id=stored,
    )


def resolve_phase_snapshot(
    *,
    agent: Agent,
    overrides_table: dict[str, dict],
    overrides_revision: int,
    producer: PhaseProducer,
    producer_epoch: float,
    sequence: int,
    previous_snapshot: PhaseSnapshot | None = None,
) -> PhaseSnapshot:
    """Build a ``PhaseSnapshot`` for ``agent``. Pure read.

    ``overrides_table`` / ``overrides_revision`` come from the same
    ``read_overrides()`` call — content + version observed atomically.
    The hook state file is read inline only for its revision + session
    validity (it no longer carries phase). Phase comes from the Claude
    transcript: a single ``os.stat`` records mtime/size into
    ``inputs_observed``, then ``detect_transcript_phase`` reverse-reads
    the JSONL for the last ``<phase>`` tag.

    ``previous_snapshot`` (the producer's last accepted snapshot for
    this pane) is an optional fast-path: when it covers the same
    transcript sessionId and the freshly-stat'd mtime+size are
    byte-identical to what it observed, the transcript scan is skipped
    and its phase reused. This runs every daemon tick / watcher event
    so a late flush is caught next tick and a stale value self-heals.
    """
    monotonic_ns = time.monotonic_ns()
    timestamp = time.time()

    detected_phase: DevPhase | None = None
    source: PhaseSource = "none"

    # Harness-generic session identity (#206): claude's stable_id IS the
    # transcript session id; codex's is the rollout thread UUID.
    sid = agent.session_metadata.stable_id

    # Hook state file: revision + session validity only (phase no longer
    # written there — see ``hooks.handle_hook_event``). The revision still
    # travels back through ``inputs_observed`` so ``replaces()`` orders
    # state-driven updates.
    _validity, hook_revision = read_hook_revision(agent.pane_id, current_session_id=sid)

    # Phase from the transcript. For claude, ``transcript_path_for`` is
    # pure path construction, returning current-scheme and (for an
    # underscore cwd) legacy-scheme candidates in priority order; for
    # codex the single candidate is the rollout path enrichment captured
    # (hook identity or heuristic). The stat loop below is the one
    # observation of existence + mtime + size, stopping at the first
    # candidate that exists. A missing file or unknown session id leaves
    # the transcript axis at 0 and falls through to the branch rule.
    if agent.agent_type is AgentType.CODEX:
        candidates = [agent.codex_rollout_path] if agent.codex_rollout_path else []
    else:
        candidates = transcript_path_for(agent.cwd, sid)

    transcript_mtime_ns = 0
    transcript_size = 0
    tpath: str | None = None
    for candidate in candidates:
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        tpath = candidate
        transcript_mtime_ns = st.st_mtime_ns
        transcript_size = st.st_size
        break

    if tpath is not None:
        reuse_ok = (
            previous_snapshot is not None
            and transcript_mtime_ns > 0
            and previous_snapshot.phase_session_id is not None
            and previous_snapshot.phase_session_id == sid
            and previous_snapshot.inputs_observed.transcript_mtime_ns == transcript_mtime_ns
            and previous_snapshot.inputs_observed.transcript_size == transcript_size
        )
        if (
            reuse_ok
            # "tag" is the only source this resolver produces now;
            # "tag_retry"/"tag_lookback" are legacy values that can only
            # arrive on a pre-#73 cached snapshot read from disk — kept
            # here purely for that back-compat tolerance, no live producer.
            and previous_snapshot.phase_source in ("tag", "tag_retry", "tag_lookback")
            and previous_snapshot.detected_phase is not None
        ):
            # Transcript byte-identical since the last resolve and it
            # yielded a tag — reuse it, skip the scan.
            detected_phase = previous_snapshot.detected_phase
            source = "tag"
        elif reuse_ok:
            # Transcript byte-identical but previously yielded no tag —
            # it still won't; skip the scan and let the rule run (its
            # inputs, e.g. PR state, change independently).
            pass
        else:
            # Adapter-dispatched read (#206): claude → projects JSONL,
            # codex → rollout (with session_meta verification against
            # ``sid``). Readers are written never to raise, but a bug in
            # one must cost only this agent's transcript axis — never the
            # producer's whole tick (degradation rule).
            adapter = get_adapter(agent.agent_type)
            transcript_phase = None
            if adapter is not None:
                try:
                    transcript_phase = adapter.transcript_phase(tpath, sid)
                except Exception:
                    log.debug("transcript phase read failed for %s", tpath, exc_info=True)
            if transcript_phase is not None:
                detected_phase = transcript_phase
                source = "tag"

    # Fall back to rule detection. ``pane_content`` is left empty: the
    # resolver runs on every daemon tick (~2 s), every watcher hook event,
    # and every override POST — a ``tmux capture-pane`` per agent per
    # resolve is exactly the cost the consolidation set out to avoid. So
    # the pane-content QA heuristic is unavailable here; a Claude agent
    # emitting ``<phase>qa</phase>`` is the supported way to land on QA.
    # ``agent.state`` is already populated by enrichment, so we pass the
    # real idle flag (free) for the cases ``detect_branch_phase`` can use
    # it without pane content.
    if detected_phase is None and agent.repo_root and agent.branch:
        rule_phase = detect_branch_phase(
            repo_root=agent.repo_root,
            branch=agent.branch,
            agent_state_idle=(agent.state == AgentState.IDLE),
            pane_content="",
        )
        if rule_phase is not None:
            detected_phase = rule_phase
            source = "rule"

    # Apply override/confirm via the 5-state session matrix. Codex thread
    # ids flap between a UUID and None in ordinary operation (capture-
    # after-write, dedupe, 3-day cutoff), so codex agents get the lenient
    # one-sided-None-is-compatible reading; claude behavior is unchanged.
    feedback = _feedback_from_entry(
        overrides_table.get(agent.pane_id, {}),
        current_session_id=agent.override_session_id,
        lenient_none=agent.agent_type is AgentType.CODEX,
    )
    if feedback is not None and feedback.action == "override" and feedback.phase is not None:
        effective_phase = feedback.phase
    else:
        effective_phase = detected_phase

    return PhaseSnapshot(
        detected_phase=detected_phase,
        effective_phase=effective_phase,
        phase_source=source,
        phase_session_id=sid,
        feedback=feedback,
        inputs_observed=InputsObserved(
            overrides_revision=overrides_revision,
            hook_state_revision=hook_revision,
            transcript_mtime_ns=transcript_mtime_ns,
            transcript_size=transcript_size,
        ),
        producer=producer,
        producer_epoch=producer_epoch,
        sequence=sequence,
        monotonic_ns=monotonic_ns,
        timestamp=timestamp,
    )
