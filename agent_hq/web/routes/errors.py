"""Errors tab routes (#148) — read side of the unified logger.

Mirrors the Automations tab shape: page GET with inline first paint from
the cache, an htmx section partial, and a drill-down into ``#detail-slot``.
NOT related to ``agent_hq/web/errors.py`` (action-error wrappers).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from agent_hq import logreader
from agent_hq.filters import format_relative_age
from agent_hq.logreader import DEFAULT_LEVELS, DEFAULT_WINDOW, LogPage

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["relative_age"] = format_relative_age


def _default_loader() -> LogPage:
    return logreader.fetch_logs(levels=DEFAULT_LEVELS, since=DEFAULT_WINDOW, limit=500)


async def _serve_stale(app: FastAPI):
    """(page, refreshing) from cache even if stale; None on cold cache."""
    cached = await run_in_threadpool(app.state.errors_cache.get_cached)
    if cached is None:
        return None
    page, fresh = cached
    refreshing = False
    if not fresh:

        async def _refresh() -> None:
            await run_in_threadpool(app.state.errors_cache.fetch, _default_loader, True)

        refreshing = app.state.errors_refresher.kick(_refresh)
    return page, refreshing


def _render_section(page: LogPage, refreshing: bool) -> str:
    groups = logreader.group_rows_by_app(page.rows)
    return templates.get_template("partials/errors_section.html").render(
        groups=groups,
        refreshing=refreshing,
        levels=list(DEFAULT_LEVELS),
        window=DEFAULT_WINDOW,
        app_filter=None,
        app_chips=[(name, len(rows)) for name, rows in groups],
        error=None,
    )


def _parse_levels(raw: str | None) -> list[str]:
    if not raw:
        return list(DEFAULT_LEVELS)
    levels = [v for v in raw.split(",") if v in logreader.VALID_LEVELS]
    return levels or list(DEFAULT_LEVELS)


def register(app: FastAPI) -> None:
    @app.get("/errors", response_class=HTMLResponse)
    async def errors_page(request: Request):
        configured = app.state.logs_configured()
        initial_html = None
        if configured:
            served = await _serve_stale(app)
            if served is not None:
                page, refreshing = served
                initial_html = _render_section(page, refreshing)
        return templates.TemplateResponse(
            request,
            "errors.html",
            {"configured": configured, "initial_html": initial_html},
        )

    @app.get("/errors/section", response_class=HTMLResponse)
    async def errors_section(
        request: Request,
        level: str | None = None,
        window: str | None = None,
        app_filter: str | None = Query(None, alias="app"),
        refresh: int = 0,
    ):
        levels = _parse_levels(level)
        window_v = window if window in logreader.WINDOWS else DEFAULT_WINDOW
        is_default = set(levels) == set(DEFAULT_LEVELS) and window_v == DEFAULT_WINDOW
        error = None
        refreshing = False
        page = LogPage(rows=[], count=0, max_id=None)
        try:
            if is_default and not refresh:
                served = await _serve_stale(app)
                if served is not None:
                    page, refreshing = served
                else:
                    page = await run_in_threadpool(app.state.errors_cache.fetch, _default_loader)
            elif is_default:
                page = await run_in_threadpool(app.state.errors_cache.fetch, _default_loader, True)
            else:
                page = await run_in_threadpool(
                    lambda: logreader.fetch_logs(levels=levels, since=window_v, limit=500)
                )
        except logreader.LogReadError as exc:
            error = str(exc)
        groups = logreader.group_rows_by_app(page.rows)
        app_chips = [(name, len(rows)) for name, rows in groups]
        if app_filter:
            groups = [(n, r) for n, r in groups if n == app_filter]
        return templates.TemplateResponse(
            request,
            "partials/errors_section.html",
            {
                "groups": groups,
                "refreshing": refreshing,
                "levels": levels,
                "window": window_v,
                "app_filter": app_filter or None,
                "app_chips": app_chips,
                "error": error,
            },
        )

    @app.get("/errors/row/{row_id}/context", response_class=HTMLResponse)
    async def error_context(request: Request, row_id: int, app_name: str = Query(..., alias="app")):
        error = None
        focus = None
        context_rows: list = []
        try:
            # Two concurrent windows: the since_id-anchored fetch finds the
            # focus row + following context at ANY depth (the desc window
            # only reaches the newest 200 same-app rows — hours, for a
            # chatty app); the desc fetch supplies the preceding rows when
            # the row is recent enough to appear in it.
            recent, anchored = await asyncio.gather(
                run_in_threadpool(
                    lambda: logreader.fetch_logs(app=app_name, limit=200, order="desc")
                ),
                run_in_threadpool(
                    lambda: logreader.fetch_logs(
                        app=app_name, since_id=row_id - 1, limit=11, order="asc"
                    )
                ),
            )
            focus = next((r for r in anchored.rows if r.id == row_id), None)
            if focus is None:
                focus = next((r for r in recent.rows if r.id == row_id), None)
            if focus is not None:
                after = sorted((r for r in anchored.rows if r.id > row_id), key=lambda r: r.id)
                before = [r for r in recent.rows if r.id < row_id][:10]
                context_rows = list(reversed(after[:10])) + [focus] + before
        except logreader.LogReadError as exc:
            error = str(exc)
        extra_pretty = None
        if focus is not None and focus.json_extra is not None:
            extra_pretty = (
                json.dumps(focus.json_extra, indent=2, ensure_ascii=False)
                if not isinstance(focus.json_extra, str)
                else focus.json_extra
            )
        return templates.TemplateResponse(
            request,
            "partials/errors_context.html",
            {
                "focus": focus,
                "rows": context_rows,
                "app_name": app_name,
                "error": error,
                "extra_pretty": extra_pretty,
                "row_id": row_id,
            },
        )
