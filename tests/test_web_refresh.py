"""Tests for BackgroundRefresher (singleflight background refresh, #226)."""

from __future__ import annotations

import asyncio

import pytest

from agent_hq.web import refresh as refresh_mod
from agent_hq.web.refresh import BackgroundRefresher


async def test_kick_runs_factory_once() -> None:
    refresher = BackgroundRefresher("t")
    calls = []

    async def work() -> None:
        calls.append(1)

    assert refresher.kick(lambda: work()) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == [1]


async def test_second_kick_while_in_flight_coalesces() -> None:
    refresher = BackgroundRefresher("t")
    started = 0
    release = asyncio.Event()

    async def work() -> None:
        nonlocal started
        started += 1
        await release.wait()

    assert refresher.kick(lambda: work()) is True
    await asyncio.sleep(0)
    assert refresher.kick(lambda: work()) is True
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started == 1


async def test_failure_sets_cooldown_and_kick_returns_false() -> None:
    refresher = BackgroundRefresher("t", cooldown=30.0)

    async def boom() -> None:
        raise RuntimeError("refresh failed")

    assert refresher.kick(lambda: boom()) is True
    for _ in range(3):
        await asyncio.sleep(0)
    assert refresher.in_flight is False
    assert refresher.kick(lambda: boom()) is False


async def test_kick_allowed_after_cooldown_elapses(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(refresh_mod.time, "monotonic", lambda: clock[0])
    refresher = BackgroundRefresher("t", cooldown=30.0)

    async def boom() -> None:
        raise RuntimeError("refresh failed")

    refresher.kick(lambda: boom())
    for _ in range(3):
        await asyncio.sleep(0)
    assert refresher.kick(lambda: boom()) is False
    clock[0] += 31
    calls = []

    async def work() -> None:
        calls.append(1)

    assert refresher.kick(lambda: work()) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == [1]


async def test_force_kick_bypasses_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """#248: a deliberate human retry ("Run now") must not be a silent no-op
    inside the post-failure cooldown. force=True clears the block; the
    cooldown is genuinely gone afterwards, not skipped for one call."""
    clock = [1000.0]
    monkeypatch.setattr(refresh_mod.time, "monotonic", lambda: clock[0])
    refresher = BackgroundRefresher("t", cooldown=600.0)

    async def boom() -> None:
        raise RuntimeError("refresh failed")

    refresher.kick(lambda: boom())
    for _ in range(3):
        await asyncio.sleep(0)
    assert refresher.kick(lambda: boom()) is False  # cooling down

    calls = []

    async def work() -> None:
        calls.append(1)

    assert refresher.kick(lambda: work(), force=True) is True
    for _ in range(3):
        await asyncio.sleep(0)
    assert calls == [1]
    assert refresher.kick(lambda: work()) is True  # block was cleared, not bypassed


async def test_force_kick_still_coalesces_onto_an_in_flight_run() -> None:
    """force bypasses the cooldown, never the singleflight — a second tap
    must not start a second paid worker run."""
    refresher = BackgroundRefresher("t")
    started = 0
    release = asyncio.Event()

    async def work() -> None:
        nonlocal started
        started += 1
        await release.wait()

    assert refresher.kick(lambda: work()) is True
    await asyncio.sleep(0)
    assert refresher.kick(lambda: work(), force=True) is True
    release.set()
    for _ in range(3):
        await asyncio.sleep(0)
    assert started == 1


async def test_success_allows_immediate_rekick() -> None:
    refresher = BackgroundRefresher("t")
    calls = []

    async def work() -> None:
        calls.append(1)

    refresher.kick(lambda: work())
    for _ in range(3):
        await asyncio.sleep(0)
    assert refresher.in_flight is False
    assert refresher.kick(lambda: work()) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == [1, 1]


async def test_cancel_stops_in_flight_task() -> None:
    """Review fix (#226): app shutdown cancels in-flight refreshes so a
    server stop doesn't log 'Task was destroyed but it is pending!'."""
    refresher = BackgroundRefresher("t")
    release = asyncio.Event()
    cancelled = []

    async def work() -> None:
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    refresher.kick(lambda: work())
    await asyncio.sleep(0)
    assert refresher.in_flight
    refresher.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert cancelled == [1]
    assert not refresher.in_flight


async def test_cancel_noop_when_idle() -> None:
    refresher = BackgroundRefresher("t")
    refresher.cancel()  # must not raise
    assert not refresher.in_flight
