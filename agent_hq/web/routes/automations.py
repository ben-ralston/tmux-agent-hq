"""Automation health routes (#54).

Read-only surface for the launchd-scheduled automations configured under
``automations:`` in projects.yaml. Lives on its own ``/automations`` tab,
which loads the section partial via htmx; rows drill down into a 50-line
log tail rendered into the page's ``#detail-slot``.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from agent_hq.automations import collect_health, tail_log
from agent_hq.autopilot_store import (
    IN_FLIGHT_OUTCOMES,
    STALE_LAUNCH_S,
    Attempt,
    AutopilotStore,
    Outcome,
)
from agent_hq.config import AutomationConfig
from agent_hq.filters import format_relative_age
from agent_hq.triage_store import TriageRecord
from agent_hq.web.routes.agents import _slug_filter, _snapshot, ansi_to_html
from agent_hq.web.routes.issues import _publish

log = logging.getLogger(__name__)

# The card refreshes in-flight outcomes (gh + tmux probes) at most this often
# per web process; the page polls every 30 s. The throttle is an in-memory
# monotonic stamp (``app.state.autopilot_last_refresh``), NOT state.json's
# ``refreshed_at``: keying it on the file meant writing the file on every
# refresh, and the triage data repo commits + pushes every write.
REFRESH_MIN_INTERVAL_S = 300

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _relative_age(dt: datetime | None) -> str:
    return format_relative_age(dt.isoformat()) if dt else ""


templates.env.filters["relative_age"] = _relative_age


def _configured_automations(app: FastAPI) -> list[AutomationConfig]:
    config = getattr(app.state, "config", None)
    return config.automations if config is not None else []


def register(app: FastAPI) -> None:
    def _loader(configs: list[AutomationConfig]):
        def loader():
            return collect_health(configs, now=datetime.now(), uid=os.getuid())

        return loader

    async def _serve_stale(configs: list[AutomationConfig]):
        """Return ``(healths, refreshing)`` from the cache — even
        TTL-expired — kicking a background forced refresh when stale (#226);
        the page's 30s poll picks up the fresh data on its next tick, so no
        SSE plumbing is needed. Returns ``None`` on a cold cache (caller
        decides whether to block).

        ``get_cached`` runs in the threadpool (review fix): it takes the
        cache lock, which ``fetch`` holds across the whole launchctl sweep
        — acquiring it on the event loop could wedge every route.
        """
        cached = await run_in_threadpool(app.state.automations_cache.get_cached)
        if cached is None:
            return None
        healths, fresh = cached
        refreshing = False
        if not fresh:

            async def _refresh() -> None:
                await run_in_threadpool(app.state.automations_cache.fetch, _loader(configs), True)

            refreshing = app.state.automations_refresher.kick(_refresh)
        return healths, refreshing

    @app.get("/automations", response_class=HTMLResponse)
    async def automations_page(request: Request):
        configs = _configured_automations(app)
        # Inline first paint (#226) when the cache holds anything; a truly
        # cold cache keeps the shell (the launchctl sweep stays off the page
        # GET). NOTE: bare env render — no request-bound Jinja globals.
        initial_html = None
        if configs:
            served = await _serve_stale(configs)
            if served is not None:
                healths, refreshing = served
                initial_html = templates.get_template("partials/automations_section.html").render(
                    healths=healths, refreshing=refreshing
                )
        return templates.TemplateResponse(
            request,
            "automations.html",
            {"configured": bool(configs), "initial_html": initial_html},
        )

    @app.get("/automations/section", response_class=HTMLResponse)
    async def automations_section(request: Request):
        configs = _configured_automations(app)
        refreshing = False
        if not configs:
            healths = []
        else:
            served = await _serve_stale(configs)
            if served is None:
                healths = await run_in_threadpool(
                    app.state.automations_cache.fetch, _loader(configs)
                )
            else:
                healths, refreshing = served
        return templates.TemplateResponse(
            request,
            "partials/automations_section.html",
            {"healths": healths, "refreshing": refreshing},
        )

    # ---- Autopilot card (#302) -------------------------------------------
    # Registered before ``/automations/{name}/logs`` so the literal path wins.

    def _age_s(ts: str, now: datetime) -> float | None:
        try:
            return (now - datetime.fromisoformat(ts)).total_seconds()
        except (ValueError, TypeError):  # unparseable, or naive vs aware
            return None

    def _autopilot_ctx() -> dict | None:
        config = getattr(app.state, "config", None)
        ap = config.autopilot if config is not None else None
        store = app.state.autopilot_store
        if ap is None or store is None:
            return None
        try:
            # snapshot() degrades a corrupt file to EMPTY + unpaused — exactly
            # the card the owner must not see on their phone. Fail loud instead.
            store.assert_readable()
        except OSError as exc:
            return {"ap": ap, "unreadable": str(exc)}
        state = store.snapshot()
        now = datetime.now().astimezone()

        def stale_launch(a: Attempt) -> bool:
            # Mirrors AutopilotStore.retry: an unparseable dispatched_at is NOT
            # stale there, so offering Retry for it would only 409.
            if a.outcome is not Outcome.LAUNCHING:
                return False
            return (_age_s(a.dispatched_at, now) or 0) > STALE_LAUNCH_S

        # UNKNOWN still holds capacity, but it needs the owner: shown with Retry.
        # CLOSED (PR closed unmerged) blocks its issue until retried, and this
        # card is the only retry surface — so it must be listed here too.
        attention = [
            a
            for a in state.attempts.values()
            if a.outcome
            in (Outcome.LAUNCH_FAILED, Outcome.ENDED_WITHOUT_PR, Outcome.UNKNOWN, Outcome.CLOSED)
            or stale_launch(a)
        ]
        waiting = [
            a
            for a in state.attempts.values()
            if a.outcome in IN_FLIGHT_OUTCOMES and a not in attention
        ]
        # Tapping a waiting row opens the agent that runs its branch; with no
        # live agent the row opens the PR instead (hand-test, #302).
        agent_slugs = {
            agent.branch: _slug_filter(agent.pane_id) for agent in _snapshot(app) if agent.branch
        }
        return {
            "ap": ap,
            "unreadable": None,
            "state": state,
            "unparsed_count": len(state.unparsed),
            "last_run": state.runs[-1] if state.runs else None,
            "waiting": sorted(waiting, key=lambda a: a.dispatched_at),
            "attention": sorted(attention, key=lambda a: a.dispatched_at),
            "agent_slugs": agent_slugs,
        }

    def _maybe_refresh() -> None:
        config = app.state.config
        store = app.state.autopilot_store
        try:
            store.assert_readable()
        except OSError:
            return  # the card reports it; never refresh over a corrupt file
        last = getattr(app.state, "autopilot_last_refresh", None)
        if last is not None and time.monotonic() - last < REFRESH_MIN_INTERVAL_S:
            return
        state = store.snapshot()
        if not any(a.outcome in IN_FLIGHT_OUTCOMES for a in state.attempts.values()):
            return
        # Stamped before the probes run, so a failing refresh is not retried
        # on every 30 s poll either.
        app.state.autopilot_last_refresh = time.monotonic()
        now = datetime.now().astimezone()
        try:
            app.state.autopilot_refresh(store, config.autopilot, now.isoformat(timespec="seconds"))
        except Exception:  # noqa: BLE001 — stale outcomes beat a 500
            log.warning("autopilot outcome refresh failed", exc_info=True)

    def _render_autopilot(request: Request, ctx: dict | None):
        if ctx is None:
            return HTMLResponse("")
        return templates.TemplateResponse(request, "partials/autopilot_panel.html", ctx)

    def _writable_store() -> AutopilotStore:
        store = app.state.autopilot_store
        config = getattr(app.state, "config", None)
        if store is None or config is None or config.autopilot is None:
            raise HTTPException(status_code=404, detail="autopilot not configured")
        try:
            store.assert_readable()
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail=f"Autopilot state file unreadable — {exc}"
            ) from exc
        return store

    @app.get("/automations/autopilot", response_class=HTMLResponse)
    async def autopilot_panel(request: Request):
        config = getattr(app.state, "config", None)
        if (
            config is not None
            and config.autopilot is not None
            and config.autopilot.enabled
            and app.state.autopilot_store is not None
        ):
            await run_in_threadpool(_maybe_refresh)
        return _render_autopilot(request, await run_in_threadpool(_autopilot_ctx))

    @app.post("/automations/autopilot/pause", response_class=HTMLResponse)
    async def autopilot_pause(request: Request, paused: str = Form(...)):
        if paused not in ("0", "1"):
            raise HTTPException(status_code=400, detail="bad pause value")
        store = await run_in_threadpool(_writable_store)
        try:
            await run_in_threadpool(store.set_paused, paused == "1")
        except OSError as exc:  # the file went bad between check and write
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _render_autopilot(request, await run_in_threadpool(_autopilot_ctx))

    @app.post("/automations/autopilot/retry", response_class=HTMLResponse)
    async def autopilot_retry(request: Request, key: str = Form(...)):
        store = await run_in_threadpool(_writable_store)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            ok = await run_in_threadpool(lambda: store.retry(key, now=now))
        except OSError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=409, detail="attempt is not retryable")
        return _render_autopilot(request, await run_in_threadpool(_autopilot_ctx))

    @app.post("/automations/autopilot/discard", response_class=HTMLResponse)
    async def autopilot_discard(request: Request, key: str = Form(...)):
        """Give up on an issue (hand-test, #302): forget its finished attempt
        — freeing the slot like Retry — AND clear its ⏵ flag, so the nightly
        run never picks it again. The flag goes first: if forgetting the
        attempt then fails, the issue is merely parked, never relaunched."""
        store = await run_in_threadpool(_writable_store)
        triage = getattr(app.state, "triage_store", None)
        if triage is None:
            raise HTTPException(status_code=503, detail="triage store unavailable")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        if not await run_in_threadpool(lambda: store.can_retry(key, now=now)):
            raise HTTPException(status_code=409, detail="attempt is not discardable")
        prior = await run_in_threadpool(triage.get, key)
        try:
            # upsert swallows an unreadable triage file and returns None; the
            # attempt must then stay, or its slot frees with the flag still on.
            stored = await run_in_threadpool(
                triage.upsert,
                TriageRecord(issue_key=key, autopilot=False, updated_at=now),
                actor="human",
            )
            if stored is None:
                raise HTTPException(status_code=503, detail="could not clear the autopilot flag")
            ok = await run_in_threadpool(lambda: store.retry(key, now=now))
            if not ok and key not in (await run_in_threadpool(store.snapshot)).attempts:
                ok = True  # another tab's Discard forgot it first — done
            elif not ok and prior is not None and prior.autopilot:
                # The attempt went live between can_retry and retry's locked
                # re-check: put the flag back rather than half-apply. (A
                # retry that RAISES leaves the flag off on purpose: the issue
                # is parked, never relaunched, and Discard can be re-tapped.)
                await run_in_threadpool(
                    triage.upsert,
                    TriageRecord(issue_key=key, autopilot=True, updated_at=now),
                    actor="human",
                )
        except OSError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        await _publish(getattr(app.state, "broker", None), "issue.invalidated", {"key": key})
        if not ok:
            raise HTTPException(status_code=409, detail="attempt is not discardable")
        return _render_autopilot(request, await run_in_threadpool(_autopilot_ctx))

    @app.get("/automations/{name}/logs", response_class=HTMLResponse)
    async def automation_logs(request: Request, name: str):
        auto = next((a for a in _configured_automations(app) if a.name == name), None)
        if auto is None:
            raise HTTPException(status_code=404, detail="automation not found")
        raw = await run_in_threadpool(tail_log, auto.log_file, 50)
        return templates.TemplateResponse(
            request,
            "partials/automations_logs.html",
            {"name": name, "ansi_html": ansi_to_html(raw)},
        )
