"""Cross-process watcher for agent state.

Three phase update paths converge on a single ``_resolve_pane`` boundary so
``Agent.phase_snapshot`` always has the same shape regardless of which
path produced it:

1. **Cache poll** (``_reload_from_cache``) — base layer, polled by
   mtime every ~500 ms. Reads snapshots straight from cache.json;
   no override re-application here (cache snapshots already came
   through the resolver in the daemon).
2. **Hook file change** (``_HooksHandler`` + ``_process_hook_file``) —
   per-pane sub-second push. On unknown pane, publishes a bare
   invalidation and lets the next cache poll materialize the agent.
3. **Override POST/clear/confirm** (``refresh_pane``) — phase routes
   call this after writing overrides.json so the SSE event fires
   immediately rather than waiting for the next cache poll.

Live execution status is separate: a 250 ms loop overlays the daemon's
native runtime snapshot without waiting for cache or phase resolution.
Public reads also overlay it so expiry is enforced between poll ticks.

Per-pane acceptance uses ``replaces(new, old)``: a snapshot whose
``inputs_observed`` is older on any axis is rejected. Equal-input
ties break on ``monotonic_ns``. A positive ``phase_session_id``
change (new Claude session in the same pane number) accepts
unconditionally — the old snapshot's inputs aren't comparable.

Defensive parse: a single agent with a corrupt ``phase_snapshot``
field must not nuke the whole snapshot. Treat it as
``phase_snapshot=None`` and keep the rest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from starlette.concurrency import run_in_threadpool
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from agent_hq import eventlog
from agent_hq.cache import CACHE_FILE, DEFAULT_MAX_AGE, SCHEMA_VERSION
from agent_hq.filters import _compact_duration
from agent_hq.models import (
    Agent,
    AgentState,
    AgentType,
    FileStats,
    PhaseSnapshot,
    optional_str,
    replaces,
)
from agent_hq.overrides import read_overrides
from agent_hq.resolver import resolve_phase_snapshot
from agent_hq.runtime_dir import UnsafeRuntimeDirError, check_trusted, ensure_dir
from agent_hq.state import load_inactive_state
from agent_hq.status_collector import apply_live_status
from agent_hq.status_types import LiveStatus
from agent_hq.web.events import Broker, Event

log = logging.getLogger(__name__)

HOOKS_DIR = Path("/tmp/agent-hq/states")


def _reconciled_sid(
    data: dict, agent_type: AgentType, owner: AgentType, legacy_key: str
) -> str | None:
    """Mirror ``Agent.from_dict``'s new-field-wins identity reconciliation.

    Without this the defensive path and the normal path can derive
    different ``stable_key``s from the same record, so an SSE update keyed
    on the daemon's key never matches the rendered card.
    """
    legacy = data.get(legacy_key)
    if agent_type is not owner:
        return legacy
    stable_id = data.get("stable_id")
    return stable_id or legacy


def _agent_from_dict_defensive(data: dict) -> Agent | None:
    """Like ``Agent.from_dict`` but tolerant of corrupt ``phase_snapshot``.

    Other structural problems (missing required fields) still return
    ``None`` — a malformed entry shouldn't appear in the snapshot at all.
    """
    try:
        return Agent.from_dict(data)
    except (KeyError, ValueError, TypeError):
        pass

    # Best-effort recovery: drop the snapshot, keep the agent.
    try:
        agent_type = AgentType(data["agent_type"])
        state = AgentState(data["state"])
    except (KeyError, ValueError):
        return None

    file_stats_val = data.get("file_stats")
    file_stats: FileStats | None = None
    if isinstance(file_stats_val, dict):
        try:
            file_stats = FileStats.from_dict(file_stats_val)
        except (KeyError, ValueError):
            file_stats = None

    try:
        return Agent(
            pane_id=data["pane_id"],
            session_name=data["session_name"],
            window_index=data["window_index"],
            pane_index=data["pane_index"],
            agent_type=agent_type,
            cwd=data["cwd"],
            repo_root=data.get("repo_root"),
            repo_name=data.get("repo_name"),
            branch=data.get("branch"),
            worktree_name=data.get("worktree_name"),
            raw_command=data.get("raw_command", ""),
            state=state,
            session_slug=data.get("session_slug"),
            phase_snapshot=None,
            file_stats=file_stats,
            latest_summary=data.get("latest_summary"),
            claude_session_id=data.get("claude_session_id"),
            claude_transcript_session_id=_reconciled_sid(
                data, agent_type, AgentType.CLAUDE, "claude_transcript_session_id"
            ),
            codex_thread_id=_reconciled_sid(data, agent_type, AgentType.CODEX, "codex_thread_id"),
            codex_rollout_path=data.get("codex_rollout_path"),
            native_name=optional_str(data.get("native_name")),
            native_name_source=optional_str(data.get("native_name_source")),
            agent_pid=(data["agent_pid"] if type(data.get("agent_pid")) is int
                       and data["agent_pid"] > 0 else None),
            status_snapshot=(LiveStatus.from_dict(data["status_snapshot"])
                             if data.get("status_snapshot") is not None else None),
        )
    except KeyError:
        return None


# How old the daemon's last write may get before the Agents tab says its
# list may be out of date (#338). The daemon ticks every ~2-6 s; a minute of
# silence means its tick keeps failing (#317) or it is gone.
STALE_AFTER_S = 60.0


@dataclass(frozen=True)
class CacheHealth:
    """What the Agents tab can say about its data source (#338).

    ``loaded``: a cache read has succeeded at least once. ``problem``: why the
    latest read failed — ``"missing"`` (no cache.json: the daemon is not
    running), ``"unreadable"`` (torn, corrupt, untrusted or another schema
    version) or ``"stale"`` (older than the caller's ``max_age``); ``None``
    when it succeeded. ``updated_at``: the daemon's write time of the last
    good read. ``loaded`` drops back to False once a missing cache has
    retired every card, so an error notice never claims a list it lacks.
    """

    loaded: bool = False
    problem: str | None = None
    updated_at: float | None = None

    def notice(self, now: float) -> tuple[str, str] | None:
        """``(kind, text)`` for the Agents tab, or ``None`` when all is well.

        ``kind`` is ``"loading"`` (nothing read yet), ``"error"`` or
        ``"stale"``. Ages use ``filters._compact_duration`` so this banner and
        the /issues one read alike.
        """
        age = None if self.updated_at is None else _compact_duration(now - self.updated_at)
        if self.problem == "missing":
            return ("error", "Agent data unavailable: the daemon's cache file is missing."
                    " Is the agent-hq daemon running?")
        if self.problem == "unreadable":
            text = "Agent data unavailable: the daemon's cache file could not be read."
            if self.loaded and age is not None:
                text += f" Showing the list the daemon wrote {age} ago."
            return ("error", text)
        stale = self.problem == "stale" or (
            self.updated_at is not None and now - self.updated_at > STALE_AFTER_S
        )
        if stale and age is not None:
            return ("stale", "Agent list may be out of date: the daemon last updated it "
                    f"{age} ago.")
        if not self.loaded:
            return ("loading", "Discovering agents…")
        return None


def _read_cache(
    *, max_age: float | None = DEFAULT_MAX_AGE, cache_file: Path | None = None
) -> tuple[list[Agent], CacheHealth]:
    """Read cache.json; return its agents and a ``CacheHealth`` for this read.

    The agents are ``[]`` whenever ``health.problem`` is set. Callers that
    must not mistake a bad read for "every agent left" (the watcher, #338)
    check the problem instead of the list.
    """
    path = cache_file or CACHE_FILE
    try:
        check_trusted(path)
        raw = path.read_text()
    except UnsafeRuntimeDirError:
        return [], CacheHealth(problem="unreadable")
    except FileNotFoundError:
        return [], CacheHealth(problem="missing")
    except OSError:
        return [], CacheHealth(problem="unreadable")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return [], CacheHealth(problem="unreadable")
    if not isinstance(data, dict):
        return [], CacheHealth(problem="unreadable")
    # v1 caches have no ``schema_version`` field — reject. The shape of
    # per-agent dicts changed; deserializing a v1 cache as v2 would
    # silently produce agents with phase_snapshot=None.
    if data.get("schema_version") != SCHEMA_VERSION:
        # Deploy-skew detector: a wholesale cache rejection blanks the
        # agent list with zero user-visible signal.
        if "schema_version" in data:
            eventlog.emit(
                "warn",
                "agent cache schema mismatch",
                {"found": data.get("schema_version"), "expected": SCHEMA_VERSION},
                min_interval_s=3600,
            )
        return [], CacheHealth(problem="unreadable")
    timestamp = data.get("timestamp")
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        timestamp = None
    if max_age is not None and (timestamp is None or (time.time() - timestamp) > max_age):
        return [], CacheHealth(problem="stale", updated_at=timestamp)
    agents_raw = data.get("agents")
    if not isinstance(agents_raw, list):
        return [], CacheHealth(problem="unreadable")

    agents: list[Agent] = []
    for entry in agents_raw:
        if not isinstance(entry, dict):
            continue
        agent = _agent_from_dict_defensive(entry)
        if agent is not None:
            agents.append(agent)
    return apply_live_status(agents), CacheHealth(loaded=True, updated_at=timestamp)


def _load_agents_defensively(
    *, max_age: float | None = DEFAULT_MAX_AGE, cache_file: Path | None = None
) -> list[Agent]:
    """Read cache.json and overlay authoritative live status.

    Returns ``[]`` if the file is missing, corrupt, wrong schema_version,
    older than ``max_age`` seconds, or contains no parsable agents.
    Pass ``max_age=None`` to skip the age check (used by the mtime-
    driven poll loop where the tick itself proves the file is fresh).
    ``cache_file`` overrides the module-level ``CACHE_FILE`` (#133
    injection — mirrors ``SnapshotStore(path=...)``).
    """
    return _read_cache(max_age=max_age, cache_file=cache_file)[0]


def _agent_fingerprint(agent: Agent, *, muted: bool) -> str:
    """SHA1 of the user-visible fields. Truncated to 12 hex chars.

    Inputs: pane_id | state | branch | session_slug | worktree_name
    | effective_phase | phase_source | feedback.action | feedback.phase
    | status_label | status_reason | muted. Observation timestamps are
    deliberately excluded: collector heartbeats are not UI changes.

    Includes ``pane_id`` so a tmux pane renumber (same logical agent, new
    number) produces a fresh fingerprint and re-renders the card against
    the new pane — its action routes (``/agents/<slug>/...``) target the
    pane, so a stale slug would 404 (#132). Includes ``feedback.action``
    and ``feedback.phase`` so badge transitions (override → confirm, or a
    confirm where ``effective_phase`` is unchanged) still trigger SSE
    updates.
    """
    snap = agent.phase_snapshot
    parts = (
        agent.pane_id,
        agent.state.value,
        agent.status_label,
        agent.status_reason,
        agent.branch or "",
        agent.session_slug or "",
        agent.worktree_name or "",
        snap.effective_phase.value if snap and snap.effective_phase else "",
        snap.phase_source if snap else "",
        snap.feedback.action if snap and snap.feedback else "",
        snap.feedback.phase.value if snap and snap.feedback and snap.feedback.phase else "",
        "1" if muted else "0",
    )
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def _load_muted_pane_ids() -> set[str]:
    """Read inactive-state file. Used to fold mute status into fingerprints."""
    try:
        return set(load_inactive_state().keys())
    except Exception:
        log.debug("failed to read inactive state", exc_info=True)
        return set()


def _pane_id_from_filename(path: Path) -> str | None:
    """``states/5.json`` → ``%5``. Returns None for non-numeric stems."""
    stem = path.stem
    if not stem.isdigit():
        return None
    return f"%{stem}"


class Watcher:
    """Owns the merged snapshot + publishes SSE events on change."""

    def __init__(
        self,
        broker: Broker,
        cache_poll_interval: float = 0.5,
        *,
        cache_file: Path | None = None,
        hooks_dir: Path | None = None,
    ):
        self.broker = broker
        self.cache_poll_interval = cache_poll_interval
        # Path injection (#133) — ``None`` falls back to the module-level
        # constants at use time, so tests may either pass explicit paths
        # here or monkeypatch ``CACHE_FILE`` / ``HOOKS_DIR``.
        self._cache_file = cache_file
        self._hooks_dir = hooks_dir

        # stable_key → Agent (with current phase_snapshot). Keyed on the
        # pane-renumber-stable identity (``Agent.stable_key``), NOT pane_id,
        # so an agent that moves to a new pane on a tmux restart is the same
        # entry — updated in place rather than removed-then-readded (#132).
        self._snapshot: dict[str, Agent] = {}
        # stable_key → fingerprint of last-published state
        self._fingerprints: dict[str, str] = {}
        # stable_keys absent for exactly one reload, awaiting a second
        # confirming reload before we publish agent.removed (debounce — see
        # _reload_from_cache).
        self._pending_removal: set[str] = set()
        # What the Agents tab shows about this data (#338): loading until the
        # first good read, then the latest read's problem and the daemon's
        # last write time.
        self._health = CacheHealth()
        # Last seen mtime_ns of cache.json. None means "currently missing" —
        # kept distinct from any real mtime value so a missing file is
        # never cached as a stable value (see _tick_cache, AHQ-020).
        self._last_cache_mtime_ns: int | None = 0

        # Watcher producer identity — used when this instance resolves a
        # snapshot locally. Stable across the watcher's lifetime so
        # snapshot-vs-snapshot tiebreaks have a meaningful epoch.
        self._epoch: float = time.time()
        self._sequence: int = 0

        self._cache_task: asyncio.Task | None = None
        self._status_task: asyncio.Task | None = None
        self._hooks_worker: asyncio.Task | None = None
        self._observer: Observer | None = None
        self._hooks_queue: asyncio.Queue[str] | None = None
        self._stopping = asyncio.Event()
        # Coordinate concurrent reload + hook fire so we don't race on
        # _snapshot. Single-process so a plain asyncio.Lock is enough.
        self._lock = asyncio.Lock()

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Start the cache poll loop + hooks observer."""
        await self._reload_from_cache()
        self._cache_task = asyncio.create_task(self._cache_poll_loop(), name="watcher-cache")
        self._status_task = asyncio.create_task(self._status_poll_loop(), name="watcher-status")
        await self.start_hooks_observer()

    async def stop(self) -> None:
        self._stopping.set()
        for task in (self._cache_task, self._status_task, self._hooks_worker):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)

    # --- public read API ------------------------------------------------------

    def snapshot(self) -> list[Agent]:
        """Current merged snapshot. Order is insertion order."""
        return apply_live_status(deepcopy(list(self._snapshot.values())))

    def health(self) -> CacheHealth:
        """The data source's state for the Agents tab (#338)."""
        return self._health

    def get_agent(self, pane_id: str) -> Agent | None:
        """Look an agent up by its *current* pane.

        ``_snapshot`` is keyed by ``stable_key`` (#132), so a pane lookup
        scans values. The set is small (one entry per live agent) and the
        pane → agent mapping is unique at any instant, so the first match
        wins.
        """
        return next((a for a in self.snapshot() if a.pane_id == pane_id), None)

    def _find_by_pane(self, pane_id: str) -> Agent | None:
        return next((a for a in self._snapshot.values() if a.pane_id == pane_id), None)

    # --- public mutate API (for routes) --------------------------------------

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _resolve_for_agent(self, agent: Agent, *, sequence: int) -> PhaseSnapshot:
        """Synchronous resolver call for ``agent``. **Blocking I/O** — only
        call from a worker thread (see ``_resolve_pane``).

        Reads overrides atomically (single ``read_overrides`` call) so the
        snapshot's ``inputs_observed.overrides_revision`` matches the
        entries used to derive its feedback. ``resolve_phase_snapshot``
        also opens the hook state file and may shell out to git / ``gh``.
        ``self._epoch`` is set once in ``__init__`` and never mutated, so
        reading it off-thread is safe.
        """
        overrides_table, overrides_rev = read_overrides()
        return resolve_phase_snapshot(
            agent=agent,
            overrides_table=overrides_table,
            overrides_revision=overrides_rev,
            producer="watcher",
            producer_epoch=self._epoch,
            sequence=sequence,
            # The agent we hold carries the last accepted snapshot — let
            # the resolver skip the transcript scan when it's unchanged.
            previous_snapshot=agent.phase_snapshot,
        )

    async def _resolve_pane(self, pane_id: str) -> PhaseSnapshot | None:
        """Re-resolve ``pane_id`` with the blocking I/O off the event loop.

        Returns None if we don't know that pane yet. Acquires
        ``self._lock`` only briefly — to read the agent reference and
        reserve a sequence number — then runs ``read_overrides`` +
        ``resolve_phase_snapshot`` in a worker thread so a slow hook-file
        read or a ``gh pr view`` (10 s timeout in ``detect_branch_phase``)
        can't stall the FastAPI event loop / SSE delivery. **Caller must
        NOT hold ``self._lock``.**
        """
        async with self._lock:
            agent = self._find_by_pane(pane_id)
            if agent is None:
                return None
            seq = self._next_sequence()
            agent = deepcopy(agent)
        return await run_in_threadpool(self._resolve_for_agent, agent, sequence=seq)

    async def _maybe_update(
        self,
        pane_id: str,
        new_snapshot: PhaseSnapshot,
        *,
        muted_ids: set[str] | None = None,
    ) -> bool:
        """Accept ``new_snapshot`` iff ``replaces(new, old)`` says so.

        Returns True if accepted + published. Caller holds ``self._lock``.
        Publishes ``agent.invalidated`` with the new fingerprint so the
        broker can dedupe identical states. The event carries the stable
        ``key`` alongside ``pane_id`` so the client keys the card on the
        renumber-stable identity (#132).
        """
        agent = self._find_by_pane(pane_id)
        if agent is None:
            return False
        old = agent.phase_snapshot
        if not replaces(new_snapshot, old):
            return False
        agent.phase_snapshot = new_snapshot
        key = agent.stable_key
        if muted_ids is None:
            muted_ids = await run_in_threadpool(_load_muted_pane_ids)
        fp = _agent_fingerprint(agent, muted=pane_id in muted_ids)
        if self._fingerprints.get(key) == fp:
            # Snapshot accepted but produces the same user-visible
            # output — record the new snapshot so revision comparison
            # stays monotonic, but don't bother the SSE clients.
            self._fingerprints[key] = fp
            return False
        self._fingerprints[key] = fp
        await self.broker.publish(
            Event(
                topic="agent.invalidated",
                data={"pane_id": pane_id, "key": key},
                fingerprint=fp,
            )
        )
        return True

    async def refresh_pane(self, pane_id: str) -> None:
        """Re-resolve ``pane_id`` and publish ``agent.invalidated``.

        Phase POST routes call this after writing overrides.json so the
        SSE event fires immediately rather than waiting for the next
        cache poll. The re-resolution's blocking reads run off the event
        loop (see ``_resolve_pane``). Unknown panes publish a bare
        invalidation — the next cache poll materializes them.
        """

        async def _publish_bare(key: str | None = None) -> None:
            data = {"pane_id": pane_id}
            if key is not None:
                data["key"] = key
            await self.broker.publish(Event(topic="agent.invalidated", data=data, fingerprint=None))

        new = await self._resolve_pane(pane_id)
        if new is None:
            # Pane unknown — bypass dedupe and let the next cache poll
            # fill in the agent.
            await _publish_bare()
            return
        async with self._lock:
            agent = self._find_by_pane(pane_id)
            if agent is None:
                # Vanished between resolve and apply — still ping the UI.
                await _publish_bare()
                return
            key = agent.stable_key
            old = agent.phase_snapshot
            if not replaces(new, old):
                # Reject the new snapshot but still publish — the route
                # call represents an explicit user action and the UI
                # should reflect it (e.g. note-only feedback that doesn't
                # change phase state).
                await _publish_bare(key)
                return
            agent.phase_snapshot = new
            muted_ids = await run_in_threadpool(_load_muted_pane_ids)
            fp = _agent_fingerprint(agent, muted=pane_id in muted_ids)
            self._fingerprints[key] = fp
            # Bypass dedupe for explicit user action — even an identical
            # fingerprint should ping the UI.
            await _publish_bare(key)

    # --- cache poll loop ------------------------------------------------------

    async def _status_poll_loop(self) -> None:
        """Read the daemon's native snapshot independently of discovery/phase I/O."""
        try:
            while not self._stopping.is_set():
                started = time.monotonic()
                try:
                    await self._tick_status()
                except Exception:
                    log.exception("runtime status poll failed")
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=max(.01, .25 - (time.monotonic() - started))
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass

    async def _tick_status(self) -> None:
        muted = await run_in_threadpool(_load_muted_pane_ids)
        async with self._lock:
            originals = list(self._snapshot.items())
            copies = deepcopy([agent for _, agent in originals])
        overlaid = await run_in_threadpool(apply_live_status, copies)
        async with self._lock:
            for (key, original), updated in zip(originals, overlaid):
                if self._snapshot.get(key) is not original:
                    # Discovery replaced this identity while the file was read.
                    continue
                original.state = updated.state
                original.status_snapshot = updated.status_snapshot
                original.codex_thread_id = updated.codex_thread_id
                original.codex_rollout_path = updated.codex_rollout_path
                new_key = original.stable_key
                if new_key != key:
                    self._snapshot.pop(key)
                    self._fingerprints.pop(key, None)
                    self._pending_removal.discard(key)
                    self._snapshot[new_key] = original
                fp = _agent_fingerprint(original, muted=original.pane_id in muted)
                if self._fingerprints.get(new_key) == fp:
                    continue
                self._fingerprints[new_key] = fp
                await self.broker.publish(Event(
                    topic="agent.invalidated",
                    data={"pane_id": original.pane_id, "key": new_key}, fingerprint=fp,
                ))

    async def _cache_poll_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    await self._tick_cache()
                except Exception as exc:
                    log.exception("cache poll tick failed")
                    eventlog.emit(
                        "error",
                        "web cache poll loop failing",
                        {"error": repr(exc)},
                        min_interval_s=900,
                    )
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.cache_poll_interval)
                    return
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass

    async def _tick_cache(self) -> None:
        try:
            mtime_ns = (self._cache_file or CACHE_FILE).stat().st_mtime_ns
        except FileNotFoundError:
            # A missing file has no stable identity to dedupe against. If we
            # cached "0" as the last-seen value, every subsequent tick while
            # the file stays missing would short-circuit on 0 == 0 and the
            # two-reload removal debounce (#118) could never complete,
            # freezing dead agent cards as live forever (AHQ-020).
            self._last_cache_mtime_ns = None
            await self._reload_from_cache()
            return
        if mtime_ns == self._last_cache_mtime_ns:
            return
        self._last_cache_mtime_ns = mtime_ns
        await self._reload_from_cache()

    async def _reload_from_cache(self) -> None:
        muted = await run_in_threadpool(_load_muted_pane_ids)
        # The mtime tick that drives this call already proves the file is
        # fresh — pass ``max_age=None`` so a daemon hiccup past
        # ``DEFAULT_MAX_AGE`` doesn't make the cache parser return ``[]``
        # and wipe the entire snapshot.
        agents, health = await run_in_threadpool(
            _read_cache, max_age=None, cache_file=self._cache_file
        )
        if health.problem == "unreadable":
            # A torn, corrupt or foreign-schema read says nothing about which
            # agents exist (#338). Treating it as "zero agents" let two such
            # reads confirm the removal debounce below and delete every card
            # while the phase headers stayed. Keep the snapshot and the
            # pending set as they are; the tab shows the problem instead.
            self._health = replace(self._health, problem=health.problem)
            return
        if health.problem is None:
            self._health = health
        else:
            # Missing: proceed with the empty list, so the AHQ-020 debounce
            # still retires agents whose daemon (and cache) are gone.
            self._health = replace(self._health, problem=health.problem)

        # Key on the renumber-stable identity, not pane_id (#132). A tmux
        # restart that renumbers panes keeps the same stable_key, so the
        # agent stays the *same* dict entry — the diff below sees it as
        # present (an in-place update against its new pane), not as an old
        # pane removed + a new pane added.
        new_agents = {a.stable_key: a for a in agents}
        # Replace ``self._snapshot`` *before* publishing so SSE-triggered
        # fetches that race in see the new state. Publish under the lock
        # so events from concurrent callers can't interleave.
        async with self._lock:
            old_keys = set(self._snapshot.keys())
            new_keys = set(new_agents.keys())

            events: list[Event] = []
            for key in new_keys:
                new_agent = new_agents[key]
                old_agent = self._snapshot.get(key)
                old_snap = old_agent.phase_snapshot if old_agent else None
                new_snap = new_agent.phase_snapshot
                # Daemon snapshots arrive with `inputs_observed`
                # already populated. Apply ``replaces`` so a stale
                # daemon snapshot (observed lower revisions than a
                # watcher-published snapshot for the same agent) can't
                # roll the UI back.
                accept = (
                    True
                    if old_agent is None
                    else (new_snap is None and old_snap is None)
                    or (new_snap is not None and replaces(new_snap, old_snap))
                )
                if not accept:
                    # Daemon's snapshot is staler than ours — preserve our
                    # phase_snapshot, but still adopt the rest of the freshly
                    # read agent. Its ``pane_id`` may have changed on a tmux
                    # renumber, so we must NOT keep the old agent wholesale or
                    # the card would stay pinned to the dead pane. Fall through
                    # to fingerprinting so a pane change re-renders the card.
                    new_agent.phase_snapshot = old_agent.phase_snapshot
                fp = _agent_fingerprint(new_agent, muted=new_agent.pane_id in muted)
                if self._fingerprints.get(key) == fp:
                    continue
                self._fingerprints[key] = fp
                events.append(
                    Event(
                        topic="agent.invalidated",
                        data={"pane_id": new_agent.pane_id, "key": key},
                        fingerprint=fp,
                    )
                )

            # Debounce removals. An identity absent for a single reload is far
            # more likely a transient discovery glitch (an empty/short tmux
            # read feeding cache.json) than a real removal. Publishing
            # agent.removed immediately makes the client delete every card
            # while the server-rendered group headers + counts stay — cards
            # vanish under the grouped views until the next 10s poll (#118).
            # Require the disappearance to persist across two consecutive
            # reloads; carry the agent's last-known record forward in the
            # snapshot meanwhile so its card/count survive the grace window.
            # A pane renumber does NOT enter ``absent`` (the stable_key is
            # still present under the new pane), so it never trips removal.
            absent = old_keys - new_keys

            # Supersede, don't remove, an absent identity whose pane is now
            # owned by a *different* live key (#132 review). This happens when
            # an agent's identity changes in place — most commonly a Claude
            # agent launched before its transcript session id was written:
            # its key starts as the ``tool:cwd:pane`` fallback, then flips to
            # the transcript id on a later tick while the pane stays put. The
            # old key would otherwise debounce into an ``agent.removed`` whose
            # ``pane_id`` is still live, and the client's slug fallback would
            # match and clobber the freshly re-keyed card — re-creating the
            # vanishing-card symptom for the routine new-agent path. Retire the
            # old key silently: no event, no carry-forward.
            live_panes = {new_agents[k].pane_id for k in new_keys}
            superseded = {
                k
                for k in absent
                if (old := self._snapshot.get(k)) is not None and old.pane_id in live_panes
            }
            for key in superseded:
                self._fingerprints.pop(key, None)
            absent -= superseded
            self._pending_removal -= superseded

            confirmed = absent & self._pending_removal
            newly_absent = absent - self._pending_removal

            for key in confirmed:
                carried = self._snapshot.get(key)
                self._fingerprints.pop(key, None)
                events.append(
                    Event(
                        topic="agent.removed",
                        data={
                            "pane_id": carried.pane_id if carried else None,
                            "key": key,
                        },
                    )
                )

            # Reset pending to exactly the identities absent *this* reload but
            # not yet confirmed. This drops just-confirmed removals and any
            # agent that reappeared (it's in new_keys, so never entered
            # `absent`).
            self._pending_removal = newly_absent
            for key in newly_absent:
                new_agents[key] = self._snapshot[key]

            self._snapshot = new_agents
            if not new_agents and self._health.problem == "missing":
                self._health = replace(self._health, loaded=False)

            for event in events:
                await self.broker.publish(event)

    # --- hooks observer -------------------------------------------------------

    async def start_hooks_observer(self) -> None:
        hooks_dir = self._hooks_dir or HOOKS_DIR
        if not hooks_dir.exists():
            ensure_dir(hooks_dir)

        loop = asyncio.get_running_loop()
        self._hooks_queue = asyncio.Queue(maxsize=500)
        handler = _HooksHandler(loop=loop, queue=self._hooks_queue)
        self._observer = Observer()
        self._observer.schedule(handler, str(hooks_dir), recursive=False)
        self._observer.start()

        self._hooks_worker = asyncio.create_task(self._drain_hooks_queue(), name="watcher-hooks")

    async def _drain_hooks_queue(self) -> None:
        try:
            while not self._stopping.is_set():
                assert self._hooks_queue is not None
                path_str = await self._hooks_queue.get()
                try:
                    await self._process_hook_file(Path(path_str))
                except Exception:
                    log.exception("processing hook file %s failed", path_str)
        except asyncio.CancelledError:
            pass

    async def _process_hook_file(self, path: Path) -> None:
        pane_id = _pane_id_from_filename(path)
        if pane_id is None:
            return

        new = await self._resolve_pane(pane_id)
        if new is None:
            # Hook fired for a pane the cache hasn't seen yet. Publish a
            # bare event; the next cache reload will fill in. Bypass
            # dedupe (no fingerprint) so the SSE fires even though the
            # broker hasn't seen this pane before.
            await self.broker.publish(
                Event(topic="agent.invalidated", data={"pane_id": pane_id}, fingerprint=None)
            )
            return
        async with self._lock:
            await self._maybe_update(pane_id, new)


class _HooksHandler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str]):
        self.loop = loop
        self.queue = queue

    def _enqueue(self, path: str) -> None:
        if not path.endswith(".json"):
            return
        self.loop.call_soon_threadsafe(self._put_nowait, path)

    def _put_nowait(self, path: str) -> None:
        try:
            self.queue.put_nowait(path)
        except asyncio.QueueFull:
            pass

    def on_created(self, event):
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_moved(self, event):
        # Atomic-rename writers finish a write by renaming a tmp file
        # into place — watchdog reports that as a moved event, not a
        # modify. Pick up the destination path so those updates aren't
        # silently dropped.
        if not event.is_directory:
            self._enqueue(event.dest_path)


def _drain_iterable(items: Iterable) -> None:
    """Helper for tests; not used in production."""
    for _ in items:
        pass
