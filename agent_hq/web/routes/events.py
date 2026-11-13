"""SSE stream endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from agent_hq.web.events import Broker

# Idle streams emit a ``: ping`` comment at this cadence. The write is the
# disconnect probe: uvicorn only notices a vanished client when it next
# writes, so without a heartbeat a zombie subscriber (client navigated
# away without the socket closing cleanly) lingers until the next real
# event. Comment lines are invisible to EventSource clients.
HEARTBEAT_SECONDS = 15.0


async def sse_stream(
    broker: Broker,
    topics: list[str],
    after_id: int,
    is_disconnected: Callable[[], Awaitable[bool]],
):
    """Yield SSE-encoded bytes: buffered + live broker events, with a
    ``: ping`` heartbeat whenever ``HEARTBEAT_SECONDS`` passes idle.

    Extracted from the route so tests can drive it directly — an
    infinite stream can't be exercised through TestClient (the client
    teardown waits on the generator, which never ends).
    """
    yield b": connected\n\n"
    subscription = broker.subscribe(topics=topics, after_id=after_id)
    # The pending-anext task must survive heartbeat timeouts:
    # asyncio.wait (unlike wait_for) leaves it running, so a timeout
    # never cancels into the subscribe generator and kills the
    # subscription.
    next_event: asyncio.Task | None = None
    try:
        while True:
            if next_event is None:
                next_event = asyncio.ensure_future(anext(subscription))
            done, _pending = await asyncio.wait({next_event}, timeout=HEARTBEAT_SECONDS)
            if not done:
                yield b": ping\n\n"
                if await is_disconnected():
                    break
                continue
            try:
                event = next_event.result()
            except StopAsyncIteration:
                break
            next_event = None
            payload = (
                f"id: {event.event_id}\nevent: {event.topic}\ndata: {json.dumps(event.data)}\n\n"
            )
            yield payload.encode("utf-8")
            if await is_disconnected():
                break
    except asyncio.CancelledError:
        pass
    finally:
        if next_event is not None:
            # Await the cancellation: until the task processes it, the
            # subscribe generator is still suspended inside __anext__ and
            # aclose() would raise "already running".
            next_event.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await next_event
        await subscription.aclose()


def register(app: FastAPI) -> None:
    @app.get("/events")
    async def events(request: Request):
        broker: Broker = app.state.broker
        topics_param = request.query_params.get(
            "topics", "agent.*,issue.*,dispatch.*,toast.*"
        )
        topics = [p.strip() for p in topics_param.split(",") if p.strip()]
        try:
            after_id = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            after_id = 0

        return StreamingResponse(
            sse_stream(broker, topics, after_id, request.is_disconnected),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
