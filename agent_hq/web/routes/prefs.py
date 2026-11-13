"""UI-preference mutation routes.

``POST /prefs/theme`` persists the theme picker's choice to
``ui-prefs.json`` (``UiPrefsStore.set_web_theme``) and redirects back to
the page the picker was on, so the resulting full page load links the
new theme stylesheet — no client JS involved. ``base.html`` reads the
stored value on every page render; a whitelisted ``?theme=`` query param
still overrides it for screenshots.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

log = logging.getLogger(__name__)


def _local_referer_path(request: Request) -> str:
    """The referer's path component, or ``/agents`` when absent/unusable.

    Only the path is ever used — scheme and host are discarded — so a
    forged or protocol-relative referer can't turn the redirect into an
    open redirect. ``urlsplit`` itself raises on a malformed authority
    (e.g. an unclosed IPv6 literal), which must not surface as a 500.
    """
    try:
        parts = urlsplit(request.headers.get("referer", ""))
    except ValueError:
        return "/agents"
    path = parts.path
    if not path.startswith("/") or path.startswith("//"):
        return "/agents"
    return path


def register(app: FastAPI) -> None:
    @app.post("/prefs/theme")
    async def set_theme(request: Request, theme: str = Form(...)):
        store = app.state.ui_prefs_store
        try:
            await run_in_threadpool(store.set_web_theme, theme)
        except ValueError:
            # The only caller is a native <form> POST, so raising here would
            # replace the app with a raw JSON error document. Leave the theme
            # unchanged and redirect back instead: that reloads the page,
            # which self-heals the case that produces an unknown theme in the
            # first place (a stale tab still offering a retired option).
            log.warning("ignoring unknown theme %r", theme)
        # 303 turns the POST into a GET of the originating page, whose
        # render picks up the just-persisted theme.
        return RedirectResponse(_local_referer_path(request), status_code=303)
