"""Tests for the in-process SSE broker."""

import asyncio

from agent_hq.web.events import Broker, Event


async def test_broker_delivers_to_matching_subscriber():
    broker = Broker(buffer_size=10)
    received = []

    async def consume():
        async for event in broker.subscribe(topics=["agent.*"]):
            received.append(event)
            if len(received) == 2:
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    await broker.publish(Event(topic="agent.invalidated", data={"pane_id": "%1"}))
    await broker.publish(Event(topic="issue.invalidated", data={"key": "x#1"}))  # filtered
    await broker.publish(Event(topic="agent.removed", data={"pane_id": "%1"}))

    await asyncio.wait_for(task, timeout=1.0)
    assert [e.topic for e in received] == ["agent.invalidated", "agent.removed"]


async def test_broker_dedupes_by_fingerprint():
    broker = Broker(buffer_size=10)
    await broker.publish(Event(topic="agent.invalidated", data={"pane_id": "%1"}, fingerprint="f1"))
    await broker.publish(Event(topic="agent.invalidated", data={"pane_id": "%1"}, fingerprint="f1"))
    await broker.publish(Event(topic="agent.invalidated", data={"pane_id": "%1"}, fingerprint="f2"))

    buffered = broker.replay_since(0)
    assert len(buffered) == 2
    assert [e.fingerprint for e in buffered] == ["f1", "f2"]


async def test_broker_replay_since_event_id():
    broker = Broker(buffer_size=10)
    for i in range(5):
        await broker.publish(
            Event(
                topic="agent.invalidated",
                data={"pane_id": f"%{i}"},
                fingerprint=f"fp{i}",
            )
        )
    replayed = broker.replay_since(after_id=2)
    assert [e.event_id for e in replayed] == [3, 4, 5]


async def test_topic_glob_exact_match():
    broker = Broker(buffer_size=10)
    received = []

    async def consume():
        async for e in broker.subscribe(topics=["toast.error"]):
            received.append(e.topic)
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await broker.publish(Event(topic="dispatch.progress", data={}))
    await broker.publish(Event(topic="toast.error", data={"message": "hi"}))

    await asyncio.wait_for(task, timeout=1.0)
    assert received == ["toast.error"]


async def test_subscriber_receives_replay_on_connect():
    broker = Broker(buffer_size=10)
    await broker.publish(
        Event(topic="agent.invalidated", data={"pane_id": "%1"}, fingerprint="fp1")
    )
    await broker.publish(
        Event(topic="agent.invalidated", data={"pane_id": "%2"}, fingerprint="fp2")
    )

    received = []

    async def consume():
        async for e in broker.subscribe(topics=["agent.*"], after_id=0):
            received.append(e.event_id)
            if len(received) == 2:
                break

    await asyncio.wait_for(asyncio.create_task(consume()), timeout=1.0)
    assert received == [1, 2]


async def _never_disconnected() -> bool:
    return False


async def test_sse_stream_emits_heartbeat_when_idle(monkeypatch):
    """An idle /events stream must emit ``: ping`` comment lines so dead
    sockets are detected (the write fails) and proxies keep the stream
    alive. Rapid tab navigation piles up zombie SSE connections without
    this — the browser's per-origin pool fills and page loads stall
    (#226 follow-up)."""
    from agent_hq.web.routes import events as events_routes

    monkeypatch.setattr(events_routes, "HEARTBEAT_SECONDS", 0.05)
    broker = Broker(buffer_size=10)
    gen = events_routes.sse_stream(broker, ["agent.*"], 0, _never_disconnected)
    try:
        first = await asyncio.wait_for(anext(gen), timeout=1.0)
        assert b"connected" in first
        ping = await asyncio.wait_for(anext(gen), timeout=1.0)
        assert ping == b": ping\n\n"
    finally:
        await gen.aclose()


async def test_sse_stream_delivers_events_with_heartbeat_active(monkeypatch):
    """The heartbeat rewrite must not break event delivery: buffered and
    live events both still reach the stream."""
    from agent_hq.web.routes import events as events_routes

    monkeypatch.setattr(events_routes, "HEARTBEAT_SECONDS", 0.05)
    broker = Broker(buffer_size=10)
    await broker.publish(Event(topic="agent.invalidated", data={"pane_id": "%1"}))

    gen = events_routes.sse_stream(broker, ["agent.*"], 0, _never_disconnected)
    try:
        await asyncio.wait_for(anext(gen), timeout=1.0)  # ": connected"
        buffered = await asyncio.wait_for(anext(gen), timeout=1.0)
        assert b"event: agent.invalidated" in buffered
        assert b'"pane_id": "%1"' in buffered

        await broker.publish(Event(topic="agent.removed", data={"pane_id": "%1"}))
        chunks = []
        for _ in range(5):  # skip any interleaved pings
            chunk = await asyncio.wait_for(anext(gen), timeout=1.0)
            chunks.append(chunk)
            if b"event: agent.removed" in chunk:
                break
        assert any(b"event: agent.removed" in c for c in chunks)
    finally:
        await gen.aclose()


async def test_sse_stream_close_releases_broker_subscription(monkeypatch):
    """Closing the stream generator must drop the broker subscriber —
    zombie subscriptions otherwise accumulate queue memory per navigation."""
    from agent_hq.web.routes import events as events_routes

    monkeypatch.setattr(events_routes, "HEARTBEAT_SECONDS", 0.05)
    broker = Broker(buffer_size=10)
    gen = events_routes.sse_stream(broker, ["agent.*"], 0, _never_disconnected)
    await asyncio.wait_for(anext(gen), timeout=1.0)  # ": connected"
    await asyncio.wait_for(anext(gen), timeout=1.0)  # first ping — subscription live
    assert len(broker._subscribers) == 1
    await gen.aclose()
    assert len(broker._subscribers) == 0


def test_default_sse_topics_drop_personal():
    """The Personal tab is gone (#345): no default topic for it."""
    import inspect

    from agent_hq.web.routes import events as events_routes

    src = inspect.getsource(events_routes)
    assert "personal.*" not in src
