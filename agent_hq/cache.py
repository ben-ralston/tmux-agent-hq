"""Cache for agent discovery results — schema v2.

Schema v2 fields (top-level keys in ``cache.json``):

* ``schema_version: 2`` — readers MUST reject any other value. The
  shape of per-agent dicts changed (``phase_snapshot`` replaces flat
  ``phase``) so a v1 cache from an older daemon must not be deserialized.
* ``producer: "daemon" | "live"`` — diagnostic. Always ``"daemon"``
  when written here; ``"live"`` is set by ``get_agents`` when the
  resolver builds snapshots on cache miss.
* ``producer_epoch: float`` — daemon instance start time (wall clock).
* ``sequence: int`` — per-instance tick counter.
* ``timestamp: float`` — wall clock at write (display + age check).
* ``monotonic_ns: int`` — ``time.monotonic_ns()`` at write (used as
  the watcher's per-pane tiebreaker for equal-input snapshots).
* ``pid: int`` — writer PID; used by ``daemon.find_orphan_daemon_pids``
  to identify ``run_dashboard``-forked daemon children whose argv
  doesn't carry ``daemon`` (so ``pgrep`` alone can't spot them).
* ``agents: list[dict]`` — each agent dict's ``phase_snapshot`` is a
  full ``PhaseSnapshot.to_dict()`` payload including
  ``inputs_observed`` so the watcher can apply ``replaces()``.

The cache lives in ``/tmp`` and is rewritten every tick. No on-disk
migration: if the daemon restarts with new code, the next tick
overwrites whatever was there.
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from .discovery import discover_agents
from .enrichment import enrich_all_agents
from .models import Agent, PhaseProducer
from .overrides import read_overrides
from .resolver import resolve_phase_snapshot
from .runtime_dir import check_trusted, ensure_dir

log = logging.getLogger(__name__)

CACHE_FILE = Path("/tmp/agent-hq/cache.json")
DEFAULT_MAX_AGE = 5.0  # seconds
SCHEMA_VERSION = 2


def write_cache(
    agents: list[Agent],
    *,
    producer: PhaseProducer = "daemon",
    producer_epoch: float | None = None,
    sequence: int = 0,
    timestamp: float | None = None,
    monotonic_ns: int | None = None,
) -> None:
    """Atomically write agents + producer/epoch/sequence metadata to cache.

    All metadata fields are optional for callers that don't care
    (tests, ad-hoc uses); production callers (daemon, live-fallback)
    pass them explicitly so the watcher's comparison rule has the
    full ordering primitives. Missing values default to ``time.time()``
    / ``time.monotonic_ns()`` / 0 — safe but degrades comparison
    precision.
    """
    ensure_dir(CACHE_FILE.parent)
    data = {
        "schema_version": SCHEMA_VERSION,
        "producer": producer,
        "producer_epoch": producer_epoch if producer_epoch is not None else time.time(),
        "sequence": sequence,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "monotonic_ns": monotonic_ns if monotonic_ns is not None else time.monotonic_ns(),
        "pid": os.getpid(),
        "agents": [a.to_dict() for a in agents],
    }
    # A temp name unique to this write (#317): two daemons once shared the
    # fixed ``cache.json.tmp``, so one's rename found the temp already moved
    # (FileNotFoundError, every tick) and a rename could publish a temp the
    # other was still writing — a torn cache.json that readers parse as zero
    # agents (#338).
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=CACHE_FILE.parent, prefix=".cache-", suffix=".json", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream)
        os.replace(temporary, CACHE_FILE)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_cache(max_age: float = DEFAULT_MAX_AGE) -> tuple[list[Agent], float] | None:
    """Read cached agents and the timestamp they were captured at.

    Returns ``(agents, timestamp)`` or ``None`` if cache is missing,
    corrupt, has the wrong schema version, or is stale.
    """
    try:
        check_trusted(CACHE_FILE)
        raw = CACHE_FILE.read_text()
    except (FileNotFoundError, OSError):
        log.debug("cache miss: file not found (%s)", CACHE_FILE)
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        log.debug("cache miss: corrupt JSON")
        return None

    if not isinstance(data, dict):
        log.debug("cache miss: unexpected JSON type")
        return None

    if data.get("schema_version") != SCHEMA_VERSION:
        log.debug(
            "cache miss: schema_version=%r, expected %d",
            data.get("schema_version"),
            SCHEMA_VERSION,
        )
        return None

    timestamp = data.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        log.debug("cache miss: no timestamp field")
        return None

    age = time.time() - timestamp
    if age > max_age:
        log.debug("cache miss: stale (%.1fs old, max %.1fs)", age, max_age)
        return None

    try:
        agents = [Agent.from_dict(a) for a in data["agents"]]
    except (KeyError, ValueError):
        log.debug("cache miss: failed to deserialize agents")
        return None

    log.debug("cache hit: %d agents (%.1fs old)", len(agents), age)
    return agents, float(timestamp)


def get_agents(filter_commands: set[str] | None = None) -> tuple[list[Agent], float]:
    """Get agents from cache if available, otherwise live discovery.

    On cache miss, live discovery builds the same ``PhaseSnapshot`` the
    daemon would have via ``resolve_phase_snapshot`` with
    ``producer="live"`` — so consumers can't tell which path produced
    the snapshot from the agent shape alone, but the producer field
    in the snapshot identifies it for debugging.
    """
    t0 = time.time()
    cached = read_cache()
    if cached is not None:
        agents, timestamp = cached
        log.debug(
            "get_agents: served from cache (%d agents, %.0fms)",
            len(agents),
            (time.time() - t0) * 1000,
        )
    else:
        raw = discover_agents(list(filter_commands) if filter_commands else None)
        agents = enrich_all_agents(raw)
        # Build PhaseSnapshots so live-fallback agents have the same
        # shape as cache-hit agents — the only signal that they came
        # from this path is ``snapshot.producer == "live"``.
        overrides_table, overrides_rev = read_overrides()
        epoch = time.time()
        for agent in agents:
            agent.phase_snapshot = resolve_phase_snapshot(
                agent=agent,
                overrides_table=overrides_table,
                overrides_revision=overrides_rev,
                producer="live",
                producer_epoch=epoch,
                sequence=0,
            )
        timestamp = time.time()
        log.debug(
            "get_agents: live discovery (%d agents, %.0fms)", len(agents), (time.time() - t0) * 1000
        )
    if filter_commands:
        agents = [a for a in agents if a.agent_type.value in filter_commands]
    from .status_collector import apply_live_status

    return apply_live_status(agents), timestamp
