"""Login / logout routes.

The GET handler accepts an optional ``?token=...`` query parameter for the
phone QR-scan flow: scanning the startup QR code opens
``https://hostname/login?token=<token>`` and the page's bootstrap JS reads
the param, fills the hidden input, and auto-submits the form via POST.
This keeps the cookie set on the same origin as the redirect target — a
GET-with-Set-Cookie would dirty the URL bar with the token on history.
"""

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agent_hq.web.auth import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    TokenManager,
    derive_cookie_value,
    is_secure_request,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def register(app: FastAPI, tm: TokenManager) -> None:
    """Attach login/logout routes to the app with the shared token manager."""

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        # ``?token=<value>`` lands here from the QR-scan flow. Pass it
        # through as ``prefill_token`` so the page can populate + auto-
        # submit the form on load. Empty string when absent → page
        # behaves like the manual paste flow.
        prefill = request.query_params.get("token", "")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": None, "prefill_token": prefill},
        )

    @app.post("/login")
    async def login_submit(request: Request, token: str = Form(default="")):
        if not tm.verify_user_token(token):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Invalid token.", "prefill_token": ""},
                status_code=200,
            )
        response = RedirectResponse(url="/agents", status_code=302)
        response.set_cookie(
            key=COOKIE_NAME,
            value=derive_cookie_value(tm),
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=is_secure_request(request),
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie(key=COOKIE_NAME)
        return response
