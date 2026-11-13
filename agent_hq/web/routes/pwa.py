"""PWA service worker + offline shell routes.

Both endpoints are exempt from auth (see ``auth.EXEMPT_PREFIXES``):

* ``/sw.js`` — the browser may fetch the SW script without cookies, and
  the SW itself leaks no data.
* ``/offline`` — must be precacheable from the SW without depending on
  auth state, and must render usefully when the phone has no network at
  all (e.g. off-tailnet).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def register(app: FastAPI) -> None:
    @app.get("/sw.js")
    async def service_worker():
        # ``Cache-Control: no-cache`` so browsers re-check the SW script
        # on every navigation — without this, an updated SW could be
        # pinned for up to 24h by HTTP cache.
        return FileResponse(
            STATIC_DIR / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/offline", response_class=HTMLResponse)
    async def offline(request: Request):
        return templates.TemplateResponse(request, "offline.html", {})
