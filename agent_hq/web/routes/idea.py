"""Idea Inbox HTTP routes — POST /api/idea, POST .../reroute, GET /inbox + recent.

The handler is built once at app startup and passed in via `register`. Routes
are thin: parse JSON, call handler, map exceptions to HTTP status codes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from agent_hq import eventlog
from agent_hq.config import Config
from agent_hq.github import GHError, detect_active_worktrees
from agent_hq.idea.classifier import ClassifierAuthError, ClassifierError
from agent_hq.idea.handler import (
    EmptyTextError,
    IdeaError,
    IdeaHandler,
    NoCandidatesError,
)
from agent_hq.models import Issue, Priority
from agent_hq.web.auth import COOKIE_NAME, _extract_bearer
from agent_hq.web.routes.issues import _group_batches_by_repo, _run_dispatch_bg

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Ceiling for /inbox/recent?limit=N — matches the depth a full-cap per-repo
# fetch can actually serve and keeps a hostile param from rendering huge pages.
_MAX_RECENT_LIMIT = 100


def _token_hash(request: Request) -> str:
    """Hash whatever auth value the caller used, so idempotency keys are scoped per-token."""
    cookie = request.cookies.get(COOKIE_NAME, "")
    if cookie:
        return hashlib.sha256(("c:" + cookie).encode()).hexdigest()
    bearer = _extract_bearer(request.headers.get("authorization", ""))
    if bearer:
        return hashlib.sha256(("b:" + bearer).encode()).hexdigest()
    return ""


def _candidates_for_template(config: Config) -> list[dict]:
    return [
        {"repo": r.owner_name, "project": r.project, "description": r.description}
        for r in config.repos.values()
        if r.project
    ]


def register(app: FastAPI, *, handler: IdeaHandler, config: Config) -> None:
    @app.post("/api/idea")
    async def api_idea(request: Request) -> Any:
        body = await _read_json(request)
        text = body.get("text")
        if not isinstance(text, str):
            raise HTTPException(status_code=422, detail="`text` field is required")

        try:
            # handler.handle does blocking I/O (Anthropic calls + several `gh`
            # shellouts) — keep it off the event loop, matching routes/agents.py.
            result = await run_in_threadpool(
                handler.handle,
                text,
                token_hash=_token_hash(request),
                idempotency_key=body.get("idempotency_key"),
            )
        except EmptyTextError:
            raise HTTPException(status_code=422, detail="text must not be empty")
        except ClassifierAuthError as e:
            eventlog.emit("warn", "idea classification failed", {"kind": "auth", "error": str(e)})
            return JSONResponse({"error": str(e)}, status_code=503)
        except NoCandidatesError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except ClassifierError as e:
            eventlog.emit(
                "warn", "idea classification failed", {"kind": "classifier", "error": str(e)}
            )
            return JSONResponse({"error": f"classifier upstream: {e}"}, status_code=502)
        except GHError as e:
            eventlog.emit("warn", "idea classification failed", {"kind": "github", "error": str(e)})
            return JSONResponse({"error": f"github: {e}"}, status_code=502)
        except IdeaError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(result.to_dict())

    @app.post("/api/idea/sent/{sent_id}/dismiss")
    async def api_idea_sent_dismiss(sent_id: str) -> Any:
        ok = await run_in_threadpool(handler.dismiss_sent, sent_id)
        if not ok:
            raise HTTPException(status_code=404, detail="unknown sent id")
        return JSONResponse({"ok": True})

    @app.post("/api/idea/{number:int}/reroute")
    async def api_idea_reroute(number: int, request: Request) -> Any:
        body = await _read_json(request)
        try:
            src_repo = body["src_repo"]
            dst_repo = body["dst_repo"]
            dst_project = body["dst_project"]
        except KeyError as e:
            raise HTTPException(status_code=422, detail=f"missing field: {e.args[0]}")
        try:
            result = await run_in_threadpool(
                handler.reroute,
                issue_number=number,
                src_repo=src_repo,
                dst_repo=dst_repo,
                dst_project=dst_project,
            )
        except NoCandidatesError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except GHError as e:
            return JSONResponse({"error": f"github: {e}"}, status_code=502)
        except IdeaError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"issue_url": result.issue_url, "warnings": result.warnings})

    @app.get("/inbox", response_class=HTMLResponse)
    async def inbox_page(request: Request):
        return templates.TemplateResponse(
            request,
            "inbox.html",
            {"candidates": _candidates_for_template(config)},
        )

    @app.get("/inbox/recent", response_class=HTMLResponse)
    async def inbox_recent(request: Request, limit: int = 10):
        # Clamp hostile/garbage limits; over-fetch by one so the template
        # knows whether a Show-more button is warranted. #128 follow-up
        limit = max(1, min(limit, _MAX_RECENT_LIMIT))
        recent = (
            await run_in_threadpool(handler.list_recent, limit=limit + 1)
            if hasattr(handler, "list_recent")
            else []
        )
        # Sent rows (failed/interrupted sends, #140) are guaranteed a slot —
        # they neither count toward the page size nor get dropped. Only
        # github rows participate in the over-fetch-by-one check.
        non_sent_count = sum(1 for r in recent if r.destination != "sent")
        has_more = non_sent_count > limit
        if has_more:
            # Drop the sentinel: the oldest non-sent row (list is newest-first).
            for i in range(len(recent) - 1, -1, -1):
                if recent[i].destination != "sent":
                    del recent[i]
                    break
        if config is not None and config.repos:
            try:
                active_worktrees = await run_in_threadpool(detect_active_worktrees, config.repos)
            except Exception:
                active_worktrees = set()
        else:
            active_worktrees = set()
        return templates.TemplateResponse(
            request,
            "partials/inbox_recent.html",
            {
                "recent": recent,
                "candidates": _candidates_for_template(config),
                "active_worktrees": active_worktrees,
                "has_more": has_more,
            },
        )

    @app.post("/inbox/dispatch", response_class=HTMLResponse)
    async def inbox_dispatch(
        request: Request,
        background_tasks: BackgroundTasks,
        kind: str = Form(...),
        repo: str = Form(""),
        number: int = Form(0),
        title: str = Form(""),
    ):
        cfg = app.state.config
        if kind == "github":
            # Config.repos is dict[str, RepoConfig] keyed by "owner/name".
            if cfg is None or repo not in cfg.repos:
                raise HTTPException(status_code=400, detail="repo not configured")
            if number <= 0:
                raise HTTPException(status_code=400, detail="invalid issue number")
            issue = Issue(
                number=number,
                title=title or f"#{number}",
                repo=repo,
                status=None,
                priority=Priority.NO_PRI,
            )
            per_repo = _group_batches_by_repo([[issue]])
            background_tasks.add_task(
                _run_dispatch_bg,
                per_repo,
                cfg,
                app.state.broker,
                capabilities_probe=app.state.capabilities_probe,
            )
            return templates.TemplateResponse(request, "partials/inbox_dispatch_button.html", {})
        raise HTTPException(status_code=400, detail="unknown kind")


async def _read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON body must be an object")
    return body
