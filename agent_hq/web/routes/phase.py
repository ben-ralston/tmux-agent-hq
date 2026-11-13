"""Phase override / confirm / note / clear routes.

All four actions go through ``agent_hq.overrides`` directly — no fork,
no parallel implementation. After the disk write, the route asks the
watcher to publish ``agent.invalidated`` (via ``refresh_pane``) so
connected tabs swap their card immediately rather than waiting for the
next cache poll.

The watcher's ``refresh_pane`` re-runs the resolver, which reads the
just-updated overrides.json under flock — no separate "fallback_phase"
plumbing needed. The override write bumps overrides.json's revision,
the resolver picks it up, and the new snapshot wins comparison
because its ``inputs_observed.overrides_revision`` is strictly newer
than the daemon's most recent tick.

Phase overlay semantics (matching the existing daemon):

| Action  | overrides.json entry                                              |
|---------|-------------------------------------------------------------------|
| override| {"action":"override", "phase":<new>, "auto_phase":<auto>, ...}    |
| confirm | {"action":"confirm",  "phase": null, "auto_phase":<auto>, ...}    |
| note    | (no entry) — only appends to phase-feedback.jsonl                 |
| clear   | (entry removed)                                                   |
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from agent_hq.models import DevPhase, normalize_phase_value
from agent_hq.overrides import (
    append_feedback,
    delete_override,
    make_feedback_event,
    write_override,
)
from agent_hq.web.routes.agents import _publish_invalidate, _snapshot, pane_id_for

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Shown when an overrides.json / feedback-log write fails (disk full,
# read-only filesystem, permission flip). Without this the route 500s
# with an empty body and the user has no idea their action didn't stick.
_DISK_ERROR_TOAST = "Couldn't save that — disk error writing the phase store. Try again."


def _toast(request: Request, message: str, level: str = "success") -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/toast.html",
        {"message": message, "level": level},
    )


def _resolve_agent(app: FastAPI, slug: str):
    pane_id = pane_id_for(slug)
    agents = _snapshot(app)
    agent = next((a for a in agents if a.pane_id == pane_id), None)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    return pane_id, agent


def _auto_phase_for(agent) -> str | None:
    """Resolve the daemon-detected phase, looking past any active override.

    Reads ``agent.phase_snapshot.detected_phase`` — the resolver
    captures both ``detected_phase`` (what auto-detection produced)
    and ``effective_phase`` (post-feedback application) in the
    snapshot, so we don't need to peek at overrides.json here.
    """
    snap = agent.phase_snapshot
    if snap is None or snap.detected_phase is None:
        return None
    return snap.detected_phase.value


def register(app: FastAPI) -> None:
    @app.get("/agents/{slug}/phase/menu", response_class=HTMLResponse)
    async def phase_menu(request: Request, slug: str):
        # Resolve to ensure pane exists (404 otherwise) but we don't
        # need any agent details — the menu is purely the action set.
        _resolve_agent(app, slug)
        return templates.TemplateResponse(
            request,
            "partials/phase_menu.html",
            {"slug": slug, "phases": list(DevPhase)},
        )

    @app.post("/agents/{slug}/phase/override", response_class=HTMLResponse)
    async def phase_override(
        request: Request,
        slug: str,
        phase: str = Form(...),
        note: str = Form(default=""),
    ):
        pane_id, agent = _resolve_agent(app, slug)
        try:
            # normalize_phase_value maps legacy values (brainstorm/plan →
            # design, merged → done) so an older client / stale form resolves
            # instead of 400ing. Unknown values still raise → handled below.
            new_phase = DevPhase(normalize_phase_value(phase))
        except ValueError:
            return templates.TemplateResponse(
                request,
                "partials/toast.html",
                {"message": f"Unknown phase: {phase}", "level": "error"},
                status_code=400,
            )

        auto_phase = _auto_phase_for(agent)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            await run_in_threadpool(
                lambda: write_override(
                    pane_id,
                    {
                        "action": "override",
                        "phase": new_phase.value,
                        "auto_phase": auto_phase,
                        "set_at": now,
                        "note": note or None,
                    },
                    session_id=agent.override_session_id,
                )
            )
            await run_in_threadpool(
                append_feedback,
                make_feedback_event(
                    pane_id=pane_id,
                    repo=agent.repo_root,
                    branch=agent.branch,
                    auto_phase=auto_phase,
                    user_phase=new_phase.value,
                    action="override",
                    note=note or None,
                ),
            )
        except OSError as exc:
            log.warning("phase override write failed for %s: %s", pane_id, exc)
            return _toast(request, _DISK_ERROR_TOAST, level="error")
        await _publish_invalidate(app, pane_id, re_resolve=True)
        return _toast(request, f"Phase set to {new_phase.label}.", level="success")

    @app.delete("/agents/{slug}/phase/override", response_class=HTMLResponse)
    async def phase_override_clear(request: Request, slug: str):
        pane_id, agent = _resolve_agent(app, slug)
        auto_phase = _auto_phase_for(agent)
        try:
            removed = await run_in_threadpool(delete_override, pane_id)
            if not removed:
                raise HTTPException(status_code=404, detail="no override")
            await run_in_threadpool(
                append_feedback,
                make_feedback_event(
                    pane_id=pane_id,
                    repo=agent.repo_root,
                    branch=agent.branch,
                    auto_phase=auto_phase,
                    user_phase=None,
                    action="undo",
                    note=None,
                ),
            )
        except OSError as exc:
            log.warning("phase override clear failed for %s: %s", pane_id, exc)
            return _toast(request, _DISK_ERROR_TOAST, level="error")
        # delete_override bumps overrides.json's revision; the watcher's
        # refresh_pane re-runs the resolver which sees the new revision
        # and the now-absent entry. effective_phase reverts to detected
        # immediately, no fallback_phase plumbing needed.
        await _publish_invalidate(app, pane_id, re_resolve=True)
        return _toast(request, "Override cleared.", level="info")

    @app.post("/agents/{slug}/phase/confirm", response_class=HTMLResponse)
    async def phase_confirm(
        request: Request,
        slug: str,
        note: str = Form(default=""),
    ):
        pane_id, agent = _resolve_agent(app, slug)
        auto_phase = _auto_phase_for(agent)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            await run_in_threadpool(
                lambda: write_override(
                    pane_id,
                    {
                        "action": "confirm",
                        "phase": None,
                        "auto_phase": auto_phase,
                        "set_at": now,
                        "note": note or None,
                    },
                    session_id=agent.override_session_id,
                )
            )
            await run_in_threadpool(
                append_feedback,
                make_feedback_event(
                    pane_id=pane_id,
                    repo=agent.repo_root,
                    branch=agent.branch,
                    auto_phase=auto_phase,
                    user_phase=None,
                    action="confirm",
                    note=note or None,
                ),
            )
        except OSError as exc:
            log.warning("phase confirm write failed for %s: %s", pane_id, exc)
            return _toast(request, _DISK_ERROR_TOAST, level="error")
        await _publish_invalidate(app, pane_id, re_resolve=True)
        return _toast(request, "Confirmed.", level="success")

    @app.post("/agents/{slug}/note", response_class=HTMLResponse)
    async def phase_note(
        request: Request,
        slug: str,
        note: str = Form(default=""),
    ):
        pane_id, agent = _resolve_agent(app, slug)
        auto_phase = _auto_phase_for(agent)
        try:
            await run_in_threadpool(
                append_feedback,
                make_feedback_event(
                    pane_id=pane_id,
                    repo=agent.repo_root,
                    branch=agent.branch,
                    auto_phase=auto_phase,
                    user_phase=None,
                    action="note",
                    note=note or None,
                ),
            )
        except OSError as exc:
            log.warning("phase note write failed for %s: %s", pane_id, exc)
            return _toast(request, _DISK_ERROR_TOAST, level="error")
        return _toast(request, "Note saved.", level="success")
