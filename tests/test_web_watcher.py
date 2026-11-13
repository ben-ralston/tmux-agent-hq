"""Tests for the new web watcher (cache.json poll + states/ watchdog + overrides).

Watcher rewrite (#73):

* Cache schema v2 — ``_write_cache`` builds v2 envelope.
* ``Agent.phase`` is now a property over ``phase_snapshot``; the test
  helper translates ``phase=DevPhase.X`` into a minimal snapshot.
* ``refresh_pane`` replaced ``invalidate(fallback_phase=…)`` — phase
  route POSTs now go through the resolver which re-reads the freshly
  written overrides.json under flock.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent_hq.cache import SCHEMA_VERSION
from agent_hq.models import (
    Agent,
    AgentState,
    AgentType,
    DevPhase,
    InputsObserved,
    PhaseSnapshot,
)
from agent_hq.web.events import Broker
from agent_hq.web.watcher import Watcher, _agent_fingerprint, _load_agents_defensively


def _snap(
    *,
    phase: DevPhase | None,
    overrides_rev: int = 0,
    hook_rev: int = 0,
    monotonic_ns: int = 0,
    session_id: str | None = None,
    source: str = "rule",
) -> PhaseSnapshot:
    """Minimal snapshot helper for tests that only care about effective_phase."""
    return PhaseSnapshot(
        detected_phase=phase,
        effective_phase=phase,
        phase_source=source,
        phase_session_id=session_id,
        feedback=None,
        inputs_observed=InputsObserved(
            overrides_revision=overrides_rev, hook_state_revision=hook_rev
        ),
        producer="daemon",
        producer_epoch=0.0,
        sequence=0,
        monotonic_ns=monotonic_ns,
        timestamp=0.0,
    )


def _mk_agent(
    pane_id: str,
    state: AgentState = AgentState.IDLE,
    branch: str = "main",
    phase: DevPhase | None = None,
    session_slug: str | None = None,
    claude_session_id: str | None = "sess-A",
    overrides_rev: int = 0,
    hook_rev: int = 0,
    monotonic_ns: int = 0,
) -> Agent:
    """Build an Agent for tests. ``phase`` is converted to a
    ``PhaseSnapshot`` so the property shim returns it for fingerprint
    inputs and detail-route assertions."""
    snapshot = (
        _snap(
            phase=phase,
            overrides_rev=overrides_rev,
            hook_rev=hook_rev,
            monotonic_ns=monotonic_ns,
        )
        if phase is not None or overrides_rev or hook_rev or monotonic_ns
        else None
    )
    return Agent(
        pane_id=pane_id,
        session_name="s",
        window_index=0,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd="/tmp",
        repo_name="r",
        branch=branch,
        session_slug=session_slug,
        state=state,
        phase_snapshot=snapshot,
        claude_session_id=claude_session_id,
    )


def _write_cache(path, agents: list[Agent]) -> None:
    """Write a v2 cache.json the way the daemon does."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "producer": "daemon",
        "producer_epoch": time.time(),
        "sequence": 1,
        "timestamp": time.time(),
        "monotonic_ns": time.monotonic_ns(),
        "pid": 12345,
        "agents": [a.to_dict() for a in agents],
    }
    path.write_text(json.dumps(payload))


# --- Fingerprint --------------------------------------------------------------


def test_fingerprint_changes_with_state():
    a = _mk_agent("%1", AgentState.IDLE)
    b = _mk_agent("%1", AgentState.RUNNING)
    assert _agent_fingerprint(a, muted=False) != _agent_fingerprint(b, muted=False)


def test_fingerprint_changes_with_phase():
    a = _mk_agent("%1", phase=DevPhase.IMPLEMENT)
    b = _mk_agent("%1", phase=DevPhase.QA)
    assert _agent_fingerprint(a, muted=False) != _agent_fingerprint(b, muted=False)


def test_fingerprint_changes_with_muted():
    a = _mk_agent("%1")
    fp_active = _agent_fingerprint(a, muted=False)
    fp_muted = _agent_fingerprint(a, muted=True)
    assert fp_active != fp_muted


def test_fingerprint_stable_across_unrelated_changes():
    a = _mk_agent("%1", AgentState.IDLE, branch="main")
    b = _mk_agent("%1", AgentState.IDLE, branch="main")
    assert _agent_fingerprint(a, muted=False) == _agent_fingerprint(b, muted=False)


def test_fingerprint_changes_with_pane_id():
    """A tmux pane renumber (same logical agent, new ``pane_id``) must
    produce a fresh fingerprint so the SSE listener re-renders the card
    against the new pane — otherwise the card keeps the stale slug and
    its action routes 404 (#132)."""
    a = _mk_agent("%38")
    b = _mk_agent("%54")
    assert _agent_fingerprint(a, muted=False) != _agent_fingerprint(b, muted=False)


# --- Defensive cache parsing --------------------------------------------------


class TestDefensiveCacheParse:
    """Layer 1 readers must tolerate unknown phase enum values without
    dropping the whole snapshot. Otherwise a single legacy / bogus phase
    string in cache.json blanks the dashboard."""

    def test_parses_valid_cache(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        agents = [_mk_agent("%1"), _mk_agent("%2")]
        _write_cache(cache_file, agents)
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        loaded = _load_agents_defensively()
        assert {a.pane_id for a in loaded} == {"%1", "%2"}

    def test_returns_empty_list_when_cache_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", tmp_path / "no-such.json")
        assert _load_agents_defensively() == []

    def test_returns_empty_list_when_cache_corrupt(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("not json{")
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        assert _load_agents_defensively() == []

    def test_drops_corrupt_phase_snapshot_per_agent_without_losing_others(
        self, tmp_path, monkeypatch
    ):
        """One agent with a corrupt phase_snapshot must not nuke the
        whole list. The bad agent's snapshot is coerced to None; others
        survive intact. Regression guard for stale cache shapes."""
        good = _mk_agent("%1", phase=DevPhase.IMPLEMENT)
        bad_dict = _mk_agent("%2").to_dict()
        bad_dict["phase_snapshot"] = "not a dict"

        payload = {
            "schema_version": SCHEMA_VERSION,
            "producer": "daemon",
            "producer_epoch": time.time(),
            "sequence": 1,
            "timestamp": time.time(),
            "monotonic_ns": time.monotonic_ns(),
            "pid": 1,
            "agents": [good.to_dict(), bad_dict],
        }
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps(payload))
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        loaded = _load_agents_defensively()
        by_id = {a.pane_id: a for a in loaded}
        assert "%1" in by_id and "%2" in by_id
        assert by_id["%1"].phase is DevPhase.IMPLEMENT
        assert by_id["%2"].phase is None

    def test_handles_legacy_merged_phase_in_snapshot(self, tmp_path, monkeypatch):
        """A phase_snapshot whose ``effective_phase`` is the pre-#55
        ``merged`` value normalizes via the alias map. Cached snapshots
        from older daemons may carry it; readers must survive."""
        agent_dict = _mk_agent("%1", phase=DevPhase.IMPLEMENT).to_dict()
        agent_dict["phase_snapshot"]["detected_phase"] = "merged"
        agent_dict["phase_snapshot"]["effective_phase"] = "merged"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "producer": "daemon",
            "producer_epoch": time.time(),
            "sequence": 1,
            "timestamp": time.time(),
            "monotonic_ns": time.monotonic_ns(),
            "pid": 1,
            "agents": [agent_dict],
        }
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps(payload))
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        loaded = _load_agents_defensively()
        assert loaded[0].phase is DevPhase.DONE

    def test_rejects_v1_cache_without_schema_version(self, tmp_path, monkeypatch):
        """Schema v2 readers MUST reject anything without
        ``schema_version: 2``. v1 caches had a different per-agent
        shape; deserializing them would silently produce agents with
        phase_snapshot=None."""
        v1_payload = {
            "timestamp": time.time(),
            "pid": 1,
            "agents": [{"pane_id": "%1"}],  # any v1-shaped entry
        }
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps(v1_payload))
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        assert _load_agents_defensively() == []
        assert _load_agents_defensively(max_age=None) == []

    def test_drops_stale_cache_beyond_max_age(self, tmp_path, monkeypatch):
        """A cache file older than ``max_age`` seconds is treated as a
        miss. Without this, an uncleanly-killed daemon's last-written
        cache would keep zombie agents on the dashboard forever."""
        cache_file = tmp_path / "cache.json"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "producer": "daemon",
            "producer_epoch": time.time() - 3600,
            "sequence": 1,
            "timestamp": time.time() - 3600,  # one hour old
            "monotonic_ns": time.monotonic_ns(),
            "pid": 1,
            "agents": [_mk_agent("%1").to_dict()],
        }
        cache_file.write_text(json.dumps(payload))
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        # Default max_age (DEFAULT_MAX_AGE = 5s) → stale → empty.
        assert _load_agents_defensively() == []
        # Explicit None → no age check, agent comes through.
        loaded = _load_agents_defensively(max_age=None)
        assert {a.pane_id for a in loaded} == {"%1"}

    def test_drops_cache_with_no_timestamp_when_age_check_enabled(self, tmp_path, monkeypatch):
        """A v2 cache file with no ``timestamp`` field must not sneak
        past the age guard — without a timestamp we can't know whether
        it's fresh or zombie."""
        cache_file = tmp_path / "cache.json"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "producer": "daemon",
            "producer_epoch": time.time(),
            "sequence": 1,
            # no "timestamp" field
            "monotonic_ns": time.monotonic_ns(),
            "pid": 1,
            "agents": [_mk_agent("%1").to_dict()],
        }
        cache_file.write_text(json.dumps(payload))
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        assert _load_agents_defensively() == []
        # Opt out of the check → agent comes through.
        loaded = _load_agents_defensively(max_age=None)
        assert {a.pane_id for a in loaded} == {"%1"}


# --- Cache mtime poll ---------------------------------------------------------


class TestCachePoll:
    """Layer 1: cache.json mtime poll detects daemon writes."""

    async def test_initial_load_populates_snapshot(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        _write_cache(cache_file, [_mk_agent("%1"), _mk_agent("%2")])
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)
        await w._reload_from_cache()

        ids = {a.pane_id for a in w.snapshot()}
        assert ids == {"%1", "%2"}

    async def test_publishes_invalidated_for_changed_agent(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)

        _write_cache(cache_file, [_mk_agent("%1", AgentState.IDLE)])
        await w._reload_from_cache()
        # First load publishes one invalidated event for the new pane.

        _write_cache(cache_file, [_mk_agent("%1", branch="feature/new-branch")])
        await w._reload_from_cache()

        topics = [(e.topic, e.data.get("pane_id")) for e in broker.replay_since(0)]
        # Two distinct fingerprints → two events for %1.
        assert topics.count(("agent.invalidated", "%1")) == 2

    async def test_no_event_when_nothing_changed(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        _write_cache(cache_file, [_mk_agent("%1", AgentState.IDLE)])
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)
        await w._reload_from_cache()
        await w._reload_from_cache()

        # First load fires once; second load same fingerprint → deduped.
        topics = [e.topic for e in broker.replay_since(0)]
        assert topics.count("agent.invalidated") == 1

    async def test_publishes_removed_when_pane_disappears(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)

        _write_cache(cache_file, [_mk_agent("%1"), _mk_agent("%2")])
        await w._reload_from_cache()
        # Removal is debounced (#118): it takes two consecutive reloads
        # missing %2 to confirm a real disappearance and publish the event.
        _write_cache(cache_file, [_mk_agent("%1")])
        await w._reload_from_cache()
        _write_cache(cache_file, [_mk_agent("%1")])
        await w._reload_from_cache()

        topics = [(e.topic, e.data.get("pane_id")) for e in broker.replay_since(0)]
        assert ("agent.removed", "%2") in topics

    async def test_transient_empty_reload_keeps_agents(self, tmp_path, monkeypatch):
        """#118: a single empty cache read (transient tmux/daemon glitch)
        must NOT wipe a non-empty snapshot or publish ``agent.removed``.
        Otherwise the client deletes every card while the server-rendered
        group headers + counts stay — cards vanish under the grouped views
        until the next 10s poll. The disappearance is held pending one
        extra reload instead."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)

        _write_cache(cache_file, [_mk_agent("%1"), _mk_agent("%2")])
        await w._reload_from_cache()
        assert {a.pane_id for a in w.snapshot()} == {"%1", "%2"}

        before = len(broker.replay_since(0))
        _write_cache(cache_file, [])
        await w._reload_from_cache()
        new_events = broker.replay_since(0)[before:]

        assert not any(e.topic == "agent.removed" for e in new_events)
        assert {a.pane_id for a in w.snapshot()} == {"%1", "%2"}

    async def test_removal_confirmed_after_two_absent_reloads(self, tmp_path, monkeypatch):
        """A disappearance confirmed across two consecutive reloads is a
        real removal: publish ``agent.removed`` and drop it. The pane is
        carried forward in the snapshot during the one-reload grace
        window so its card/count survive a transient miss."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)

        _write_cache(cache_file, [_mk_agent("%1"), _mk_agent("%2")])
        await w._reload_from_cache()

        # First absence — pending, not yet published; %2 carried forward.
        _write_cache(cache_file, [_mk_agent("%1")])
        await w._reload_from_cache()
        assert {a.pane_id for a in w.snapshot()} == {"%1", "%2"}

        before = len(broker.replay_since(0))
        # Second consecutive absence — confirmed removal.
        _write_cache(cache_file, [_mk_agent("%1")])
        await w._reload_from_cache()
        new_events = [(e.topic, e.data.get("pane_id")) for e in broker.replay_since(0)[before:]]

        assert ("agent.removed", "%2") in new_events
        assert {a.pane_id for a in w.snapshot()} == {"%1"}

    async def test_reappearing_agent_cancels_pending_removal(self, tmp_path, monkeypatch):
        """A pane absent for one reload then present again is a transient
        glitch, not a removal: ``agent.removed`` must never fire and the
        pane stays in the snapshot."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)

        _write_cache(cache_file, [_mk_agent("%1"), _mk_agent("%2")])
        await w._reload_from_cache()

        before = len(broker.replay_since(0))
        # %2 vanishes for one reload...
        _write_cache(cache_file, [_mk_agent("%1")])
        await w._reload_from_cache()
        # ...then reappears before the removal is confirmed.
        _write_cache(cache_file, [_mk_agent("%1"), _mk_agent("%2")])
        await w._reload_from_cache()
        new_events = broker.replay_since(0)[before:]

        assert not any(e.topic == "agent.removed" for e in new_events)
        assert {a.pane_id for a in w.snapshot()} == {"%1", "%2"}

    async def test_torn_cache_reads_never_remove_cards(self, tmp_path, monkeypatch):
        """#338: a cache.json that can't be parsed is no evidence the agents
        are gone. Two such reads in a row used to confirm the #118 debounce
        and publish ``agent.removed`` for every card — the client deleted
        them all and left the phase headers standing. Keep the snapshot,
        publish nothing, and report the problem instead."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)

        _write_cache(cache_file, [_mk_agent("%1"), _mk_agent("%2")])
        await w._reload_from_cache()
        before = len(broker.replay_since(0))

        cache_file.write_text('{"schema_version": 2, "agents": [{"pane_')
        await w._reload_from_cache()
        await w._reload_from_cache()

        assert broker.replay_since(0)[before:] == []
        assert {a.pane_id for a in w.snapshot()} == {"%1", "%2"}
        assert w.health().problem == "unreadable"
        assert w.health().loaded is True

        # A good read clears the problem; the agents were never pending removal.
        _write_cache(cache_file, [_mk_agent("%1"), _mk_agent("%2")])
        await w._reload_from_cache()
        assert w.health().problem is None
        assert {a.pane_id for a in w.snapshot()} == {"%1", "%2"}

    async def test_health_reports_daemon_write_time(self, tmp_path, monkeypatch):
        """#338 error state: the cache's own ``timestamp`` is the daemon's
        last successful tick, so a failing daemon shows up as an aging
        ``updated_at`` even though the file is still readable."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        w = Watcher(broker=Broker(buffer_size=50), cache_poll_interval=10)

        assert w.health().loaded is False

        _write_cache(cache_file, [_mk_agent("%1")])
        data = json.loads(cache_file.read_text())
        data["timestamp"] = 1000.0
        cache_file.write_text(json.dumps(data))
        await w._reload_from_cache()

        health = w.health()
        assert health.loaded is True
        assert health.problem is None
        assert health.updated_at == 1000.0

    async def test_unreadable_after_cards_were_retired_does_not_claim_a_list(
        self, tmp_path, monkeypatch
    ):
        """Review round 1: a missing cache retires every card (AHQ-020); a
        corrupt file arriving after that must not say it is showing a list."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        w = Watcher(broker=Broker(buffer_size=50), cache_poll_interval=10)

        _write_cache(cache_file, [_mk_agent("%1")])
        await w._reload_from_cache()
        cache_file.unlink()
        await w._reload_from_cache()
        await w._reload_from_cache()
        assert w.snapshot() == []

        cache_file.write_text("{")
        await w._reload_from_cache()
        kind, text = w.health().notice(time.time())
        assert kind == "error"
        assert "Showing" not in text

    def test_stale_problem_renders_the_stale_notice(self):
        from agent_hq.web.watcher import CacheHealth

        health = CacheHealth(problem="stale", updated_at=time.time() - 3600)
        kind, text = health.notice(time.time())
        assert kind == "stale"
        assert "1h ago" in text

    async def test_missing_cache_is_reported(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        w = Watcher(broker=Broker(buffer_size=50), cache_poll_interval=10)
        await w._reload_from_cache()
        assert w.health().problem == "missing"
        assert w.health().loaded is False

    async def test_tick_cache_completes_removal_debounce_when_file_deleted(
        self, tmp_path, monkeypatch
    ):
        """AHQ-020: cache.json deleted while the service runs. Before the
        fix, the first tick after deletion (mtime 0) reloads once (pending
        removal), but every subsequent tick short-circuits on
        ``0 == _last_cache_mtime_ns`` — the confirming second reload that
        the two-reload debounce (#118) needs never runs, so dead agent
        cards render as live indefinitely."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)

        _write_cache(cache_file, [_mk_agent("%1")])
        await w._tick_cache()
        assert {a.pane_id for a in w.snapshot()} == {"%1"}

        cache_file.unlink()
        await w._tick_cache()  # first miss — pending, not yet confirmed
        assert {a.pane_id for a in w.snapshot()} == {"%1"}

        before = len(broker.replay_since(0))
        await w._tick_cache()  # second consecutive miss — must confirm
        new_events = [(e.topic, e.data.get("pane_id")) for e in broker.replay_since(0)[before:]]

        assert ("agent.removed", "%1") in new_events
        assert w.snapshot() == []

    async def test_reload_does_not_wipe_snapshot_when_cache_timestamp_is_stale(
        self, tmp_path, monkeypatch
    ):
        """Regression: ``_reload_from_cache`` is driven by mtime change,
        which already proves the file is fresh. The age check on
        ``_load_agents_defensively`` would otherwise reject the just-
        written cache during a daemon hiccup (>5 s between ticks) and
        return ``[]`` — wiping the snapshot and publishing
        ``agent.removed`` for every pane on every connected browser."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)

        _write_cache(cache_file, [_mk_agent("%1"), _mk_agent("%2")])
        await w._reload_from_cache()
        assert {a.pane_id for a in w.snapshot()} == {"%1", "%2"}

        # Daemon write with a deliberately old timestamp — what we'd see
        # on a heavily-loaded laptop or after a sleep/wake.
        payload = {
            "schema_version": SCHEMA_VERSION,
            "producer": "daemon",
            "producer_epoch": time.time() - 3600,
            "sequence": 2,
            "timestamp": time.time() - 3600,
            "monotonic_ns": time.monotonic_ns(),
            "pid": 1,
            "agents": [_mk_agent("%1").to_dict(), _mk_agent("%2").to_dict()],
        }
        cache_file.write_text(json.dumps(payload))

        # Drop the events buffer so the assertions below only see this
        # reload's events.
        before = len(broker.replay_since(0))
        await w._reload_from_cache()
        new_events = broker.replay_since(0)[before:]

        # Snapshot intact, NO removed events emitted.
        assert {a.pane_id for a in w.snapshot()} == {"%1", "%2"}
        assert not any(e.topic == "agent.removed" for e in new_events)


# --- Pane renumber (#132) -----------------------------------------------------


class TestPaneRenumber:
    """#132: a tmux server restart / resurrect renumbers panes. The watcher
    must key agents on a stable identity (transcript session id, else
    ``tool:cwd:pane_id``) so a still-running agent that moves to a new pane
    is treated as the *same* card — updated in place — rather than
    removed-then-readded (which leaves its card missing on the live SSE
    view until the next 10s full reload)."""

    def _claude_agent(self, pane_id, session_id, *, state=AgentState.RUNNING):
        a = _mk_agent(pane_id, state)
        a.claude_transcript_session_id = session_id
        return a

    async def test_pane_renumber_updates_card_in_place(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)

        _write_cache(cache_file, [self._claude_agent("%38", "trans-1")])
        await w._reload_from_cache()
        assert {a.pane_id for a in w.snapshot()} == {"%38"}

        before = len(broker.replay_since(0))
        # Same transcript identity, brand-new pane number (the resurrect).
        _write_cache(cache_file, [self._claude_agent("%54", "trans-1")])
        await w._reload_from_cache()
        new_events = broker.replay_since(0)[before:]

        # The renumbered agent is NOT removed...
        assert not any(e.topic == "agent.removed" for e in new_events)
        # ...it's invalidated, pointing at the NEW pane and carrying the
        # stable key so the client can find its existing card by key.
        inval = [e for e in new_events if e.topic == "agent.invalidated"]
        assert any(e.data.get("pane_id") == "%54" and e.data.get("key") == "trans-1" for e in inval)
        # Snapshot now reflects only the new pane.
        assert {a.pane_id for a in w.snapshot()} == {"%54"}

    async def test_renumber_does_not_carry_forward_old_pane(self, tmp_path, monkeypatch):
        """The old pane must not linger as a duplicate card. After the
        renumber the snapshot holds exactly one agent for the identity."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)

        _write_cache(cache_file, [self._claude_agent("%38", "trans-1")])
        await w._reload_from_cache()
        _write_cache(cache_file, [self._claude_agent("%54", "trans-1")])
        await w._reload_from_cache()

        panes = [a.pane_id for a in w.snapshot()]
        assert panes == ["%54"]

    async def test_session_id_resolving_after_launch_does_not_remove_card(
        self, tmp_path, monkeypatch
    ):
        """A Claude agent launches before its transcript session id is
        written (the ``sessions/{pid}.json`` file lands a tick or two
        later), so its ``stable_key`` starts as the ``tool:cwd:pane``
        fallback and flips to the transcript id once it resolves — the
        pane never changes. The watcher must NOT emit ``agent.removed``
        for the retired fallback key: its pane is still live under the new
        key, and the client's slug fallback would match and clobber the
        freshly re-keyed card, reintroducing the #132 symptom for the
        routine new-agent path."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)

        # Reload A: no transcript id yet → fallback key.
        a0 = self._claude_agent("%54", None)
        _write_cache(cache_file, [a0])
        await w._reload_from_cache()

        before = len(broker.replay_since(0))
        # Reload B: transcript id resolves; SAME pane.
        a1 = self._claude_agent("%54", "trans-1")
        _write_cache(cache_file, [a1])
        await w._reload_from_cache()
        # Reload C: steady state — would confirm a debounced removal.
        _write_cache(cache_file, [self._claude_agent("%54", "trans-1")])
        await w._reload_from_cache()

        new_events = broker.replay_since(0)[before:]
        assert not any(e.topic == "agent.removed" for e in new_events)
        # Exactly one live agent at the pane, now keyed by the transcript id.
        assert [a.pane_id for a in w.snapshot()] == ["%54"]
        assert w.get_agent("%54").stable_key == "trans-1"

    async def test_distinct_sessionless_agents_in_same_cwd_both_survive(
        self, tmp_path, monkeypatch
    ):
        """Two session-less agents (no transcript id) sharing a cwd must
        not collide to one key — that would silently drop a card. The
        fallback key includes the pane to keep them distinct."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)

        a = _mk_agent("%1")
        b = _mk_agent("%2")
        a.claude_transcript_session_id = None
        b.claude_transcript_session_id = None
        a.cwd = b.cwd = "/home/me/scratch"
        _write_cache(cache_file, [a, b])
        await w._reload_from_cache()

        assert {ag.pane_id for ag in w.snapshot()} == {"%1", "%2"}


# --- Overrides overlay --------------------------------------------------------


class TestCacheOverridesSnapshot:
    """The daemon's resolver applies overrides BEFORE writing cache —
    the watcher reads the snapshot as-is. Verify a cache file that
    encodes an override-applied phase_snapshot surfaces correctly."""

    async def test_cache_snapshot_carries_override_phase(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        # Daemon-realistic: cache snapshot has effective_phase=QA, the
        # override-applied value. Detected was IMPLEMENT pre-override.
        agent = _mk_agent("%1", phase=DevPhase.IMPLEMENT)
        # Replace its snapshot with one that mirrors what the resolver
        # would produce for an active override.
        from agent_hq.models import PhaseFeedback

        agent.phase_snapshot = PhaseSnapshot(
            detected_phase=DevPhase.IMPLEMENT,
            effective_phase=DevPhase.QA,
            phase_source="rule",
            phase_session_id=None,
            feedback=PhaseFeedback(
                action="override",
                phase=DevPhase.QA,
                auto_phase=DevPhase.IMPLEMENT,
                set_at="2026-05-10T00:00:00Z",
                note=None,
                session_id="sess-A",
            ),
            inputs_observed=InputsObserved(overrides_revision=3, hook_state_revision=0),
            producer="daemon",
            producer_epoch=0.0,
            sequence=1,
            monotonic_ns=0,
            timestamp=0.0,
        )
        _write_cache(cache_file, [agent])
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=10)
        await w._reload_from_cache()

        loaded = w.get_agent("%1")
        assert loaded is not None
        assert loaded.phase is DevPhase.QA
        assert loaded.phase_snapshot.feedback.action == "override"


# --- Hook layer ---------------------------------------------------------------


class TestHookLayer:
    """Layer 2: states/{pane}.json watchdog with pane_id derived from filename."""

    async def test_derives_pane_id_from_filename_not_payload(self, tmp_path, monkeypatch):
        """Hook JSON does *not* contain a ``pane_id`` field — the daemon
        and feature/39 watcher had different conventions. Filename is
        the source of truth: ``states/5.json`` → ``%5``."""
        states_dir = tmp_path / "states"
        states_dir.mkdir()
        cache_file = tmp_path / "cache.json"
        _write_cache(cache_file, [_mk_agent("%5", AgentState.IDLE)])

        monkeypatch.setattr("agent_hq.web.watcher.HOOKS_DIR", states_dir)
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=60)
        await w._reload_from_cache()
        await w.start_hooks_observer()
        try:
            await asyncio.sleep(0.05)
            (states_dir / "5.json").write_text(json.dumps({"state": "running"}))
            for _ in range(40):
                await asyncio.sleep(0.05)
                hits = [
                    e
                    for e in broker.replay_since(0)
                    if e.topic == "agent.invalidated" and e.data.get("pane_id") == "%5"
                ]
                # First reload fires once; expect a second event from the hook.
                if len(hits) >= 2:
                    break

            hits = [
                e
                for e in broker.replay_since(0)
                if e.topic == "agent.invalidated" and e.data.get("pane_id") == "%5"
            ]
            assert len(hits) >= 2
        finally:
            await w.stop()

    async def test_hook_retries_partial_json(self, tmp_path, monkeypatch):
        """A hook file written without atomic rename can be read mid-write;
        the watcher retries before giving up."""
        states_dir = tmp_path / "states"
        states_dir.mkdir()
        cache_file = tmp_path / "cache.json"
        _write_cache(cache_file, [_mk_agent("%5")])

        monkeypatch.setattr("agent_hq.web.watcher.HOOKS_DIR", states_dir)
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=60)
        await w._reload_from_cache()
        await w.start_hooks_observer()
        try:
            path = states_dir / "5.json"
            path.write_text("{")  # truncated mid-write

            async def finish():
                await asyncio.sleep(0.04)
                path.write_text(json.dumps({"state": "running"}))

            asyncio.create_task(finish())
            path.touch()  # nudge watchdog

            for _ in range(40):
                await asyncio.sleep(0.05)
                hits = [
                    e
                    for e in broker.replay_since(0)
                    if e.topic == "agent.invalidated" and e.data.get("pane_id") == "%5"
                ]
                if len(hits) >= 2:
                    break

            hits = [
                e
                for e in broker.replay_since(0)
                if e.topic == "agent.invalidated" and e.data.get("pane_id") == "%5"
            ]
            assert len(hits) >= 2
        finally:
            await w.stop()


class TestHookPathResolvesAgainstStateFile:
    """End-to-end: a hook file landing a new revision must thread that
    revision through ``_resolve_pane`` → ``inputs_observed`` → ``replaces``,
    while phase is derived from the transcript (the resolver's real
    source post-#73). Patches the resolver's actual read paths
    (``agent_hq.hooks.HOOK_STATE_DIR`` for revision,
    ``agent_hq.transcript.CLAUDE_PROJECTS_DIR`` for phase), not just the
    watcher's observer dir — otherwise the test proves nothing.
    """

    def _wire(self, tmp_path, monkeypatch):
        states_dir = tmp_path / "states"
        states_dir.mkdir()
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        cache_file = tmp_path / "cache.json"
        overrides_file = tmp_path / "overrides.json"
        # Agent with a transcript sessionId; repo_root unset → no rule
        # fallback, so phase comes purely from the transcript JSONL.
        agent = _mk_agent("%5", AgentState.RUNNING)
        agent.claude_transcript_session_id = "trans-5"
        agent.cwd = "/tmp"
        _write_cache(cache_file, [agent])
        # Transcript: Claude last emitted <phase>qa</phase>.
        tdir = projects_dir / "-tmp"
        tdir.mkdir(parents=True)
        (tdir / "trans-5.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Done.\n\n<phase>qa</phase>"}]
                    },
                }
            )
        )
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        monkeypatch.setattr("agent_hq.web.watcher.HOOKS_DIR", states_dir)
        monkeypatch.setattr("agent_hq.hooks.HOOK_STATE_DIR", states_dir)
        monkeypatch.setattr("agent_hq.transcript.CLAUDE_PROJECTS_DIR", projects_dir)
        monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_file)
        return states_dir

    def _write_state(self, states_dir, *, revision):
        (states_dir / "5.json").write_text(
            json.dumps(
                {
                    "revision": revision,
                    "state": "running",
                    "session_id": "trans-5",
                    "timestamp": time.time(),
                }
            )
        )

    async def test_hook_revision_flows_into_snapshot_and_replaces(self, tmp_path, monkeypatch):
        states_dir = self._wire(tmp_path, monkeypatch)
        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=60)
        await w._reload_from_cache()

        # Hook lands revision=7. Phase (qa) comes from the transcript.
        self._write_state(states_dir, revision=7)
        await w._process_hook_file(states_dir / "5.json")

        snap = w.get_agent("%5").phase_snapshot
        assert snap is not None
        assert snap.effective_phase is DevPhase.QA  # from transcript
        assert snap.phase_source == "tag"
        assert snap.inputs_observed.hook_state_revision == 7
        invalidations = [
            e
            for e in broker.replay_since(0)
            if e.topic == "agent.invalidated" and e.data.get("pane_id") == "%5"
        ]
        assert len(invalidations) >= 2  # initial reload + the hook update

    async def test_older_hook_revision_is_rejected(self, tmp_path, monkeypatch):
        states_dir = self._wire(tmp_path, monkeypatch)
        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=60)
        await w._reload_from_cache()

        self._write_state(states_dir, revision=7)
        await w._process_hook_file(states_dir / "5.json")
        assert w.get_agent("%5").phase_snapshot.inputs_observed.hook_state_revision == 7

        # A lower hook revision must not roll the snapshot back, even
        # though the transcript (phase source) is unchanged.
        self._write_state(states_dir, revision=6)
        await w._process_hook_file(states_dir / "5.json")

        snap = w.get_agent("%5").phase_snapshot
        assert snap.inputs_observed.hook_state_revision == 7
        assert snap.effective_phase is DevPhase.QA


# --- _HooksHandler unit tests -------------------------------------------------


class _StubEvent:
    """Lightweight stand-in for watchdog FileSystemEvent."""

    def __init__(self, *, src_path: str, dest_path: str = "", is_directory: bool = False):
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


class TestHooksHandlerOnMoved:
    """``_HooksHandler`` must respond to rename events too.

    Atomic-rename writers (hooks.py:500-510) emit ``on_moved`` rather
    than ``on_modified`` when the tempfile is renamed into place. Without
    a handler the watcher misses hook updates whenever the writer is
    atomic, which defeats the prep PR's whole point.
    """

    def _make_handler(self):
        from agent_hq.web.watcher import _HooksHandler

        loop = asyncio.new_event_loop()
        try:
            queue: asyncio.Queue[str] = asyncio.Queue(maxsize=10)
            handler = _HooksHandler(loop=loop, queue=queue)
            return handler, loop, queue
        except Exception:
            loop.close()
            raise

    def test_on_moved_enqueues_dest_path(self):
        handler, loop, queue = self._make_handler()
        try:
            handler.on_moved(
                _StubEvent(
                    src_path="/tmp/agent-hq/states/5_abc.json.tmp",
                    dest_path="/tmp/agent-hq/states/5.json",
                )
            )
            # call_soon_threadsafe schedules the put on the loop; drain it.
            loop.run_until_complete(asyncio.sleep(0))
            assert queue.qsize() == 1
            assert queue.get_nowait() == "/tmp/agent-hq/states/5.json"
        finally:
            loop.close()

    def test_on_moved_ignores_directories(self):
        handler, loop, queue = self._make_handler()
        try:
            handler.on_moved(
                _StubEvent(
                    src_path="/tmp/agent-hq/states",
                    dest_path="/tmp/agent-hq/states-old",
                    is_directory=True,
                )
            )
            loop.run_until_complete(asyncio.sleep(0))
            assert queue.qsize() == 0
        finally:
            loop.close()

    def test_on_moved_ignores_non_json_dest(self):
        """Mirrors the real writer: tempfiles end in ``.json.tmp`` — the
        ``.endswith(".json")`` filter must reject that suffix so the
        watcher isn't woken by intermediate rename events."""
        handler, loop, queue = self._make_handler()
        try:
            handler.on_moved(
                _StubEvent(
                    src_path="/tmp/agent-hq/states/5_abc.json.tmp",
                    dest_path="/tmp/agent-hq/states/5_abc.json.tmp",
                )
            )
            loop.run_until_complete(asyncio.sleep(0))
            assert queue.qsize() == 0
        finally:
            loop.close()


# --- Public invalidate API ----------------------------------------------------


class TestRefreshPane:
    """Phase routes call ``watcher.refresh_pane(pane_id)`` after writing
    overrides.json. The watcher re-runs the resolver, which reads the
    just-written overrides under flock — no ``fallback_phase`` plumbing
    needed because the resolver always sees the current overrides
    revision.
    """

    async def test_refresh_pane_publishes_event(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        overrides_file = tmp_path / "overrides.json"
        _write_cache(cache_file, [_mk_agent("%1", phase=DevPhase.IMPLEMENT)])
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=60)
        await w._reload_from_cache()
        await w.refresh_pane("%1")

        topics = [(e.topic, e.data.get("pane_id")) for e in broker.replay_since(0)]
        # Initial reload + refresh_pane publish.
        assert topics.count(("agent.invalidated", "%1")) >= 2

    async def test_refresh_pane_picks_up_override_via_resolver(self, tmp_path, monkeypatch):
        """Writing an override (which bumps overrides.json's revision)
        and then calling refresh_pane re-runs the resolver and the new
        effective_phase wins comparison because its
        ``inputs_observed.overrides_revision`` is strictly newer."""
        from agent_hq.overrides import write_override

        cache_file = tmp_path / "cache.json"
        overrides_file = tmp_path / "overrides.json"
        _write_cache(cache_file, [_mk_agent("%1", phase=DevPhase.IMPLEMENT)])
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=60)
        await w._reload_from_cache()
        assert w.get_agent("%1").phase is DevPhase.IMPLEMENT

        write_override(
            "%1",
            {
                "action": "override",
                "phase": "qa",
                "auto_phase": "implement",
                "set_at": "2026-05-10T00:00:00Z",
                "note": None,
            },
            session_id="sess-A",
        )
        await w.refresh_pane("%1")

        agent = w.get_agent("%1")
        assert agent is not None
        assert agent.phase is DevPhase.QA

    async def test_refresh_pane_drops_feedback_when_override_cleared(self, tmp_path, monkeypatch):
        """Clearing the override bumps the revision again; resolver sees
        the now-absent entry and the new snapshot has no feedback.

        The resolver re-resolves detected_phase from scratch each call
        (no carry-forward of prior detected). The invariant the test
        enforces is: post-clear, the feedback is gone."""
        from agent_hq.overrides import delete_override, write_override

        cache_file = tmp_path / "cache.json"
        overrides_file = tmp_path / "overrides.json"
        _write_cache(cache_file, [_mk_agent("%1", phase=DevPhase.IMPLEMENT)])
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=60)
        await w._reload_from_cache()

        write_override(
            "%1",
            {
                "action": "override",
                "phase": "qa",
                "auto_phase": "implement",
                "set_at": "2026-05-10T00:00:00Z",
                "note": None,
            },
            session_id="sess-A",
        )
        await w.refresh_pane("%1")
        agent = w.get_agent("%1")
        assert agent.phase is DevPhase.QA
        assert agent.phase_snapshot.feedback is not None
        assert agent.phase_snapshot.feedback.action == "override"

        delete_override("%1")
        await w.refresh_pane("%1")
        agent = w.get_agent("%1")
        assert agent.phase_snapshot.feedback is None

    async def test_refresh_pane_unknown_pane_publishes_bare_event(self, tmp_path, monkeypatch):
        """Hook events / route POSTs against a pane the cache hasn't
        seen yet must not crash. Publish a bare invalidation and let
        the next cache reload materialize the agent."""
        cache_file = tmp_path / "cache.json"
        overrides_file = tmp_path / "overrides.json"
        _write_cache(cache_file, [])
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_file)

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=60)
        await w._reload_from_cache()
        await w.refresh_pane("%99")

        topics = [(e.topic, e.data.get("pane_id")) for e in broker.replay_since(0)]
        assert ("agent.invalidated", "%99") in topics


class TestStaleInputRace:
    """Codex v3 finding: a daemon mid-tick that began before a web POST
    overwrote the watcher's POST resolution would roll the UI back. The
    new comparison rule (``replaces``) rejects daemon snapshots whose
    ``inputs_observed`` is older than the watcher's current snapshot.
    """

    async def test_watcher_snapshot_wins_over_stale_daemon_cache(self, tmp_path, monkeypatch):
        """Sequence:
        1. Daemon writes a cache with overrides_revision=5.
        2. Web POST writes an override, bumping revision to 6.
        3. Watcher refreshes — its snapshot observes revision=6.
        4. Daemon completes a tick that started BEFORE the POST and
           writes a cache with overrides_revision=5 again.
        5. Watcher's cache reload must REJECT step 4's snapshot for
           that pane — its inputs_observed.overrides_revision=5 is
           older than the watcher's=6.
        """
        from agent_hq.overrides import write_override

        cache_file = tmp_path / "cache.json"
        overrides_file = tmp_path / "overrides.json"
        # Seed overrides at revision=5.
        overrides_file.write_text(json.dumps({"revision": 5, "entries": {}}))
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_file)

        # Step 1: daemon cache with overrides_revision=5 (no override active).
        agent_before = _mk_agent("%1", phase=DevPhase.IMPLEMENT, overrides_rev=5)
        _write_cache(cache_file, [agent_before])

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=60)
        await w._reload_from_cache()
        assert w.get_agent("%1").phase is DevPhase.IMPLEMENT

        # Step 2-3: web POST writes override → revision=6, refresh_pane.
        write_override(
            "%1",
            {
                "action": "override",
                "phase": "qa",
                "auto_phase": "implement",
                "set_at": "2026-05-10T00:00:00Z",
                "note": None,
            },
            session_id="sess-A",
        )
        await w.refresh_pane("%1")
        assert w.get_agent("%1").phase is DevPhase.QA
        post_resolve_snap = w.get_agent("%1").phase_snapshot
        # Watcher observed the post-POST revision (=6).
        assert post_resolve_snap.inputs_observed.overrides_revision == 6

        # Step 4: stale daemon tick — wrote cache with revision=5 still.
        stale_agent = _mk_agent("%1", phase=DevPhase.IMPLEMENT, overrides_rev=5)
        _write_cache(cache_file, [stale_agent])
        await w._reload_from_cache()

        # Step 5: watcher rejected the stale daemon snapshot for this pane.
        agent = w.get_agent("%1")
        assert agent is not None
        assert agent.phase is DevPhase.QA, "stale daemon snapshot rolled back the UI"


class TestSessionIdReset:
    """A new Claude session reusing the same pane number is a real
    reset — the watcher must accept the new snapshot even when its
    revisions are lower than the old session's."""

    async def test_session_change_accepts_lower_revisions(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        overrides_file = tmp_path / "overrides.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_file)

        # Old session: high revisions.
        old_agent = _mk_agent("%1", phase=DevPhase.QA, overrides_rev=100, hook_rev=50)
        old_agent.phase_snapshot = PhaseSnapshot(
            detected_phase=DevPhase.QA,
            effective_phase=DevPhase.QA,
            phase_source="tag",
            phase_session_id="session-A",
            feedback=None,
            inputs_observed=InputsObserved(100, 50),
            producer="daemon",
            producer_epoch=0.0,
            sequence=1,
            monotonic_ns=0,
            timestamp=0.0,
        )
        _write_cache(cache_file, [old_agent])

        broker = Broker(buffer_size=50)
        w = Watcher(broker=broker, cache_poll_interval=60)
        await w._reload_from_cache()
        assert w.get_agent("%1").phase is DevPhase.QA

        # New session: low revisions but DIFFERENT phase_session_id.
        new_agent = _mk_agent("%1", phase=DevPhase.IMPLEMENT)
        new_agent.phase_snapshot = PhaseSnapshot(
            detected_phase=DevPhase.IMPLEMENT,
            effective_phase=DevPhase.IMPLEMENT,
            phase_source="rule",
            phase_session_id="session-B",  # different from old
            feedback=None,
            inputs_observed=InputsObserved(1, 1),  # lower than old's 100, 50
            producer="daemon",
            producer_epoch=0.0,
            sequence=2,
            monotonic_ns=0,
            timestamp=0.0,
        )
        _write_cache(cache_file, [new_agent])
        await w._reload_from_cache()

        agent = w.get_agent("%1")
        assert agent is not None
        # Despite lower revisions, the session change accepted.
        assert agent.phase is DevPhase.IMPLEMENT
        assert agent.phase_snapshot.phase_session_id == "session-B"


class TestFingerprintFeedbackAction:
    """Fingerprint includes ``feedback.action`` so a confirm (which
    doesn't change effective_phase) still triggers an SSE update for
    the badge transition."""

    def test_confirm_action_changes_fingerprint(self):
        from agent_hq.models import PhaseFeedback

        plain = _mk_agent("%1", phase=DevPhase.IMPLEMENT)
        confirmed = _mk_agent("%1", phase=DevPhase.IMPLEMENT)
        # Same effective_phase, but a confirm feedback attached.
        confirmed.phase_snapshot = PhaseSnapshot(
            detected_phase=DevPhase.IMPLEMENT,
            effective_phase=DevPhase.IMPLEMENT,
            phase_source="tag",
            phase_session_id=None,
            feedback=PhaseFeedback(
                action="confirm",
                phase=None,
                auto_phase=DevPhase.IMPLEMENT,
                set_at="2026-05-10T00:00:00Z",
                note=None,
                session_id="sess-A",
            ),
            inputs_observed=InputsObserved(1, 0),
            producer="daemon",
            producer_epoch=0.0,
            sequence=1,
            monotonic_ns=0,
            timestamp=0.0,
        )
        assert _agent_fingerprint(plain, muted=False) != _agent_fingerprint(confirmed, muted=False)


class TestPathInjection:
    """#133: the watcher's cache/states paths are injectable via the
    constructor (mirroring ``SnapshotStore(path=...)`` from #125), so
    tests can wire explicit tmp paths instead of monkeypatching the
    module globals."""

    async def test_injected_cache_file_is_read(self, tmp_path):
        """No monkeypatching here — the conftest isolation fixture
        points the module-level CACHE_FILE at an *empty* sandbox path,
        so finding the agent proves the injected path was read."""
        cache_file = tmp_path / "injected-cache.json"
        _write_cache(cache_file, [_mk_agent("%7", phase=DevPhase.IMPLEMENT)])

        broker = Broker(buffer_size=10)
        w = Watcher(broker=broker, cache_poll_interval=60, cache_file=cache_file)
        await w._reload_from_cache()

        agent = w.get_agent("%7")
        assert agent is not None
        assert agent.phase is DevPhase.IMPLEMENT

    async def test_injected_hooks_dir_is_observed(self, tmp_path):
        hooks_dir = tmp_path / "injected-states"

        broker = Broker(buffer_size=10)
        w = Watcher(broker=broker, cache_poll_interval=60, hooks_dir=hooks_dir)
        try:
            await w.start_hooks_observer()
            # The observer creates + watches the injected dir.
            assert hooks_dir.is_dir()
        finally:
            await w.stop()


# Suppress filter warnings from third-party deps under our event loop config
@pytest.fixture(autouse=True)
def _quiet_loop_warnings():
    yield


class TestDefensiveCodexIdentityReconciliation:
    """Workflow review: the defensive fallback must apply the same
    new-field-wins reconciliation as Agent.from_dict, or the watcher and
    the daemon compute different stable_keys from one record."""

    def _record(self, **kw):
        d = {
            "pane_id": "%5",
            "session_name": "s",
            "window_index": 0,
            "pane_index": 0,
            "agent_type": "codex",
            "harness": "codex",
            "cwd": "/w",
            "raw_command": "codex",
            "state": "idle",
            "phase_snapshot": {"bogus": object()},  # forces the defensive path
        }
        d.update(kw)
        return d

    def test_stable_id_backfills_codex_thread_id(self):
        from agent_hq.web.watcher import _agent_from_dict_defensive

        agent = _agent_from_dict_defensive(self._record(stable_id="uuid-7"))
        assert agent is not None
        assert agent.codex_thread_id == "uuid-7"
        assert agent.stable_key == "uuid-7"

    def test_explicit_codex_thread_id_kept(self):
        from agent_hq.web.watcher import _agent_from_dict_defensive

        agent = _agent_from_dict_defensive(
            self._record(stable_id="uuid-7", codex_thread_id="uuid-7")
        )
        assert agent.codex_thread_id == "uuid-7"

    def test_native_name_fields_survive_defensive_path(self):
        """#287 review: the best-effort rebuild must carry the native name."""
        from agent_hq.web.watcher import _agent_from_dict_defensive

        agent = _agent_from_dict_defensive(
            self._record(native_name="agent-hq-c3", native_name_source="derived")
        )
        assert agent.native_name == "agent-hq-c3"
        assert agent.native_name_source == "derived"
        bad = _agent_from_dict_defensive(self._record(native_name={"x": 1}))
        assert bad.native_name is None
