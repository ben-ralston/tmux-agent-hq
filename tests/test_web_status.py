"""Native-only web status: independent polling, expiry and backend presentation."""

import asyncio
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent_hq.models import AgentState
from agent_hq.status_types import LiveStatus
from agent_hq.web.events import Broker
from agent_hq.web.watcher import Watcher, _agent_fingerprint, _agent_from_dict_defensive
from tests.test_web_watcher import _mk_agent, _write_cache


@pytest.fixture
def native(tmp_path, monkeypatch):
    path = tmp_path / "runtime.json"
    monkeypatch.setattr("agent_hq.status_collector.STATUS_FILE", path)
    monkeypatch.setattr("agent_hq.status_collector._alive", lambda pid: True)

    def write(state="running", *, age=0, label=None, reason=""):
        status = LiveStatus(state, state, label or state.title(), reason, "claude",
                            "native-session", 123, time.time() - age,
                            time.monotonic_ns() - int(age * 1e9))
        path.write_text(json.dumps({"version": 1, "heartbeat_at": status.observed_at,
                                   "heartbeat_ns": status.observed_ns,
                                   "observations": [{"status": status.to_dict()}],
                                   "source_errors": {}}))
        return status

    return write


def live_agent():
    agent = _mk_agent("%1")
    agent.agent_pid = 123
    agent.claude_transcript_session_id = "native-session"
    return agent


@pytest.mark.asyncio
async def test_status_only_change_and_expiry_publish_without_cache_change(
    tmp_path, native, monkeypatch
):
    cache = tmp_path / "cache.json"
    _write_cache(cache, [live_agent()])
    native()
    broker = Broker()
    watcher = Watcher(broker, cache_file=cache)
    await watcher._tick_cache()
    initial = broker.replay_since(0)[-1].event_id
    native("waiting", reason="Approve command")
    await watcher._tick_status()
    events = broker.replay_since(initial)
    assert len(events) == 1
    assert events[0].topic == "agent.invalidated"
    assert watcher.get_agent("%1").state is AgentState.WAITING
    monkeypatch.setattr("agent_hq.status_collector.MAX_STATUS_AGE", .03)
    await asyncio.sleep(.04)
    await watcher._tick_status()
    assert watcher.get_agent("%1").state is AgentState.UNKNOWN
    assert len(broker.replay_since(initial)) == 2
    await watcher._tick_status()
    assert len(broker.replay_since(initial)) == 2


@pytest.mark.asyncio
async def test_cache_and_public_reads_always_overlay_native_status(tmp_path, native):
    cache = tmp_path / "cache.json"
    stale = live_agent()
    stale.state = AgentState.RUNNING
    _write_cache(cache, [stale])
    native("waiting")
    watcher = Watcher(Broker(), cache_file=cache)
    await watcher._reload_from_cache()
    assert watcher.get_agent("%1").state is AgentState.WAITING
    _write_cache(cache, [stale])
    await watcher._reload_from_cache()
    assert watcher.snapshot()[0].state is AgentState.WAITING
    native(age=10)
    assert watcher.snapshot()[0].state is AgentState.UNKNOWN
    assert watcher.get_agent("%1").state is AgentState.UNKNOWN


@pytest.mark.asyncio
async def test_slow_phase_resolver_does_not_delay_status_poll(tmp_path, native, monkeypatch):
    cache = tmp_path / "cache.json"
    _write_cache(cache, [live_agent()])
    native()
    watcher = Watcher(Broker(), cache_file=cache, hooks_dir=tmp_path / "hooks")
    entered, release = threading.Event(), threading.Event()

    def slow(agent, **kwargs):
        entered.set()
        release.wait(3)
        return None

    monkeypatch.setattr(watcher, "_resolve_for_agent", slow)
    monkeypatch.setattr(watcher, "start_hooks_observer", AsyncMock())
    await watcher.start()
    resolving = asyncio.create_task(watcher._resolve_pane("%1"))
    try:
        await asyncio.to_thread(entered.wait, 1)
        native("waiting")
        deadline = time.monotonic() + .8
        while watcher._snapshot["native-session"].state is not AgentState.WAITING:
            assert time.monotonic() < deadline
            await asyncio.sleep(.02)
        assert not resolving.done()
    finally:
        release.set()
        await resolving
        await watcher.stop()


@pytest.mark.asyncio
async def test_status_read_does_not_mutate_shared_agents_or_replace_new_discovery(
    tmp_path, native, monkeypatch
):
    cache = tmp_path / "cache.json"
    _write_cache(cache, [live_agent()])
    native()
    watcher = Watcher(Broker(), cache_file=cache)
    await watcher._reload_from_cache()
    original = watcher._snapshot["native-session"]
    entered, release = threading.Event(), threading.Event()

    def delayed_overlay(agents):
        agents[0].state = AgentState.WAITING
        entered.set()
        release.wait(3)
        return agents

    monkeypatch.setattr("agent_hq.web.watcher.apply_live_status", delayed_overlay)
    poll = asyncio.create_task(watcher._tick_status())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert original.state is AgentState.RUNNING
        replacement = live_agent()
        replacement.state = AgentState.UNKNOWN
        watcher._snapshot["native-session"] = replacement
    finally:
        release.set()
        await poll
    assert watcher._snapshot["native-session"] is replacement
    assert replacement.state is AgentState.UNKNOWN


def test_fingerprint_tracks_label_reason_but_not_observation_time(native):
    agent = live_agent()
    agent.status_snapshot = native()
    initial = _agent_fingerprint(agent, muted=False)
    agent.status_snapshot = replace(agent.status_snapshot, observed_at=time.time() + 1)
    assert _agent_fingerprint(agent, muted=False) == initial
    agent.status_snapshot = replace(agent.status_snapshot, label="Native label")
    assert _agent_fingerprint(agent, muted=False) != initial
    initial = _agent_fingerprint(agent, muted=False)
    agent.status_snapshot = replace(agent.status_snapshot, reason="New reason")
    assert _agent_fingerprint(agent, muted=False) != initial


def test_defensive_phase_parse_retains_native_identity_and_status(native):
    agent = live_agent()
    agent.status_snapshot = native()
    record = agent.to_dict()
    record["phase_snapshot"] = "corrupt"
    parsed = _agent_from_dict_defensive(record)
    assert parsed.agent_pid == 123
    assert parsed.status_snapshot == agent.status_snapshot


def test_shared_state_dot_escapes_backend_label_and_reason(native):
    agent = live_agent()
    agent.state = AgentState.WAITING
    agent.status_snapshot = native("waiting", label='Novel <label>', reason='"<script>')
    root = Path(__file__).parents[1] / "agent_hq/web/templates"
    env = Environment(loader=FileSystemLoader(root), autoescape=select_autoescape())
    rendered = env.get_template("partials/state_dot.html").render(agent=agent)
    assert "state-dot--waiting" in rendered
    assert "Novel &lt;label&gt;" in rendered
    assert "&lt;script&gt;" in rendered
    assert 'aria-label=' in rendered
    assert '<script>' not in rendered
