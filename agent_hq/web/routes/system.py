"""System-level actions (#215): the settings-menu Restart button.

``POST /system/restart`` replicates the runtime part of
``./run install-global`` (see agent_hq/restart.py's module docstring for
the restart-time vs install-only breakdown): daemon stop → snapshot-cache
clear → daemon start, then — only when this very process is the
launchd-managed web service — a ``launchctl kickstart -k`` of itself.

The kick kills this server, so it runs in a Starlette background task
AFTER the response is flushed; the rendered interstitial polls /healthz
until the boot id changes, then redirects back to /agents.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from agent_hq import eventlog, restart

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _report_failed_steps(steps) -> None:
    for step in steps:
        if not step.ok:
            eventlog.emit(
                "error",
                "web service restart step failed",
                {"step": step.name, "detail": step.detail},
            )


def _kick_after_flush() -> None:
    # Give the client a beat to finish receiving the response body before
    # launchd kills this process mid-write.
    time.sleep(0.5)
    restart.kick_web_service()


def register(app: FastAPI) -> None:
    # One restart at a time per server process: overlapping POSTs (a double
    # submit, two tabs) must not interleave their stop/start sequences.
    restart_lock = asyncio.Lock()

    @app.post("/system/restart", response_class=HTMLResponse)
    async def system_restart(request: Request):
        async with restart_lock:
            steps = await run_in_threadpool(restart.perform_daemon_restart)
            _report_failed_steps(steps)
            will_restart_web = await run_in_threadpool(
                restart.web_service_owns_process, os.getpid()
            )
        # A wedged daemon must not block the web restart: failed steps are
        # rendered, and the kick still happens when eligible.
        background = BackgroundTask(_kick_after_flush) if will_restart_web else None
        return templates.TemplateResponse(
            request,
            "restarting.html",
            {
                "steps": steps,
                "will_restart_web": will_restart_web,
                "boot_id": app.state.boot_id,
            },
            background=background,
        )
