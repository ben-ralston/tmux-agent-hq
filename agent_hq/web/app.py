"""FastAPI application factory.

Single-process only — do NOT run with workers>1 or reload. The SSE broker
and watcher state both live in process memory; multiple workers would split
this state and break SSE delivery.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from agent_hq import eventlog, gh_budget, logreader
from agent_hq.automations import AutomationsCache
from agent_hq.autopilot import sync_outcomes
from agent_hq.autopilot_store import AutopilotStore
from agent_hq.capabilities import LauncherCapabilities, probe_launcher_capabilities
from agent_hq.config import RETIRED_KEYS, Config, ConfigError, WebConfig, load_config
from agent_hq.github import GitHubCache
from agent_hq.hygiene_store import HygieneStore
from agent_hq.idea.sent_store import SentMessageStore
from agent_hq.pinned_store import PinnedSessionsStore
from agent_hq.triage_store import TriageStore
from agent_hq.ui_prefs import UiPrefsStore
from agent_hq.web.auth import (
    COOKIE_NAME,
    TokenManager,
    install_auth_middleware,
    verify_cookie_value,
)
from agent_hq.web.events import Broker
from agent_hq.web.refresh import BackgroundRefresher
from agent_hq.web.routes import agents as agents_routes
from agent_hq.web.routes import auth as auth_routes
from agent_hq.web.routes import automations as automations_routes
from agent_hq.web.routes import errors as errors_routes
from agent_hq.web.routes import events as events_routes
from agent_hq.web.routes import idea as idea_routes
from agent_hq.web.routes import issues as issues_routes
from agent_hq.web.routes import phase as phase_routes
from agent_hq.web.routes import prefs as prefs_routes
from agent_hq.web.routes import pwa as pwa_routes
from agent_hq.web.routes import system as system_routes

STATIC_DIR = Path(__file__).resolve().parent / "static"
log = logging.getLogger(__name__)


def create_app(
    token_file: Path,
    broker_buffer_size: int = 100,
    enable_watcher: bool | None = None,
    *,
    config: Config | None | object = ...,
    github_cache: GitHubCache | None = None,
    sent_store: SentMessageStore | None = None,
    automations_cache: AutomationsCache | None = None,
    pinned_store: PinnedSessionsStore | None = None,
    ui_prefs_store: UiPrefsStore | None = None,
    capabilities_probe: Callable[[], LauncherCapabilities] | None = None,
    errors_cache: logreader.ErrorsCache | None = None,
    logs_configured: Callable[[], bool] | None = None,
    triage_store: TriageStore | None = None,
    hygiene_store: HygieneStore | None = None,
    autopilot_store: AutopilotStore | None = None,
    autopilot_refresh: Callable[..., object] | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    ``config`` and ``github_cache`` are normally lazy-loaded from disk
    (``~/.config/agent-hq/projects.yaml`` + the on-disk GitHub cache).
    Tests inject explicit overrides via the keyword arguments — passing
    ``config=None`` is a valid override (means "no projects configured"),
    so we use ``Ellipsis`` as the not-supplied sentinel. ``Config`` is
    intentionally typed loosely on the parameter to match that sentinel.

    ``capabilities_probe`` (#205 Stage 1B Task 15) is the injectable seam
    for "which harnesses does the installed launch.sh advertise" — defaults
    to ``probe_launcher_capabilities`` (a real subprocess call, cached).
    Tests inject a stub returning a fixed ``LauncherCapabilities`` so no
    test ever shells out to the real launcher.
    """
    tm = TokenManager(token_file)
    tm.ensure()
    broker = Broker(buffer_size=broker_buffer_size)

    if config is ...:
        try:
            config = load_config()
        except ConfigError as e:
            log.warning("Config load failed: %s", e)
            eventlog.emit("warn", "config load failed", {"error": str(e)})
            config = None
    for key in config.retired_keys if isinstance(config, Config) else ():
        # Once per web process (#399): the long-lived place to say it, since
        # short-lived CLIs and MCP servers load the same config. The message
        # is per key ("companion config block ignored"), from a fixed table.
        eventlog.emit("warn", f"{key} config block ignored", {"hint": RETIRED_KEYS[key]})
    if github_cache is None:
        github_cache = GitHubCache(graphql_floor=config.github.graphql_floor if config else None)
    if sent_store is None:
        sent_store = SentMessageStore()
    if automations_cache is None:
        automations_cache = AutomationsCache()
    if pinned_store is None:
        pinned_store = PinnedSessionsStore()
    if ui_prefs_store is None:
        ui_prefs_store = UiPrefsStore()
    if capabilities_probe is None:
        capabilities_probe = probe_launcher_capabilities
    if errors_cache is None:
        errors_cache = logreader.ErrorsCache()
    if logs_configured is None:
        logs_configured = logreader.is_configured
    if triage_store is None and config is not None and config.triage is not None:
        # No triage: section in projects.yaml → stays None; the hub's Queue
        # renders a "not configured" hint instead (#247).
        triage_store = TriageStore(config.triage.data_dir)
    if hygiene_store is None and config is not None and config.triage is not None:
        hygiene_store = HygieneStore(config.triage.data_dir)
    if (
        autopilot_store is None
        and config is not None
        and config.triage is not None
        and config.autopilot is not None
    ):
        # Same file the nightly `agent-hq autopilot run` writes (#302).
        autopilot_store = AutopilotStore(config.triage.data_dir)

    if enable_watcher is None:
        # Default off during pytest so tests don't spawn watchdog observers
        # or filesystem polls in the background.
        enable_watcher = "pytest" not in sys.modules

    watcher = None
    if enable_watcher:
        from agent_hq.web.watcher import Watcher

        watcher = Watcher(broker=broker)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if watcher is not None:
            await watcher.start()
        try:
            yield
        finally:
            # Cancel in-flight SWR refreshes (#226) so shutdown doesn't log
            # "Task was destroyed but it is pending!".
            for refresher in (
                app.state.issues_refresher,
                app.state.automations_refresher,
                app.state.errors_refresher,
                app.state.triage_refresher,
                app.state.hygiene_refresher,
            ):
                refresher.cancel()
            if watcher is not None:
                await watcher.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.broker = broker
    app.state.watcher = watcher
    app.state.token_manager = tm
    app.state.config = config
    # iOS "Jump to Terminal" deep link (#137). Defaults apply even without
    # a projects.yaml; a null/empty ``web.ios_jump_shortcut`` disables it.
    web_cfg = config.web if config is not None else WebConfig()
    app.state.ios_jump_url = (
        f"shortcuts://run-shortcut?name={quote(web_cfg.ios_jump_shortcut)}"
        if web_cfg.ios_jump_shortcut
        else None
    )
    app.state.github_cache = github_cache
    app.state.sent_store = sent_store
    app.state.automations_cache = automations_cache
    app.state.pinned_store = pinned_store
    app.state.ui_prefs_store = ui_prefs_store
    # A callable, not a value, and specifically the NON-BLOCKING one: the
    # settings menu asks during template rendering, which happens on the
    # event loop inside async routes. `snapshot` would run up to _SAMPLES
    # `gh api rate_limit` subprocesses there once per memo expiry and stall
    # the whole service; `snapshot_cached` answers from the memo and probes
    # on a background thread (#255 review).
    app.state.gh_budget_snapshot = gh_budget.snapshot_cached
    # The SAME floor the gate enforces (threaded into GitHubCache above), not
    # the module default — otherwise a configured floor of 2000 leaves the ⚙
    # row calm down to 500 while background refreshes have already stood down,
    # so the indicator contradicts the behaviour it exists to explain (#255
    # review).
    app.state.gh_budget_floor = (
        config.github.graphql_floor if config else gh_budget.DEFAULT_GRAPHQL_FLOOR
    )
    app.state.capabilities_probe = capabilities_probe
    app.state.errors_cache = errors_cache
    app.state.logs_configured = logs_configured
    app.state.triage_store = triage_store
    app.state.hygiene_store = hygiene_store
    # Autopilot card (#302). ``autopilot_refresh(store, cfg, now)`` is the
    # outcome-refresh seam (real gh/tmux probes by default); tests inject a stub.
    app.state.autopilot_store = autopilot_store
    app.state.autopilot_refresh = autopilot_refresh or (
        lambda store, cfg, now: sync_outcomes(store, now=now)
    )
    # Per-process refresh throttle stamp (time.monotonic()); None = never.
    app.state.autopilot_last_refresh = None
    # Stale-while-revalidate singleflight handles (#226): routes that serve
    # expired cache data kick exactly one background refresh through these.
    app.state.issues_refresher = BackgroundRefresher("issues")
    app.state.automations_refresher = BackgroundRefresher("automations")
    app.state.errors_refresher = BackgroundRefresher("errors")
    # The AI triage worker (#248) shares the singleflight machinery: one run
    # at a time, and a 600 s cooldown after a failing run so a broken
    # ``claude -p`` can't be re-invoked on every /issues load.
    app.state.triage_refresher = BackgroundRefresher("triage-worker", cooldown=600.0)
    # Hygiene sweep (#249): same singleflight + 600 s cooldown; the 24 h
    # ``last_swept_at`` stamp is what keeps the auto-trigger cheap.
    app.state.hygiene_refresher = BackgroundRefresher("hygiene-sweep", cooldown=600.0)
    # Per-process boot id: the restarting interstitial polls /healthz until
    # this changes, which proves the launchd kickstart produced a new server
    # process (#215).
    app.state.boot_id = uuid.uuid4().hex

    @app.middleware("http")
    async def _request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        try:
            response = await call_next(request)
        except Exception as exc:
            # HTTPException never reaches here (handled by the inner
            # ExceptionMiddleware) — only genuine 500-class bugs do.
            eventlog.emit(
                "error",
                "web unhandled exception",
                {
                    "path": request.url.path,
                    "method": request.method,
                    "request_id": rid,
                    "error": repr(exc),
                },
            )
            raise
        response.headers["x-request-id"] = rid
        return response

    install_auth_middleware(app, tm)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # Cache-buster appended to ``/static/*`` URLs in base.html so a
    # server restart (the natural action after editing CSS / JS)
    # invalidates the browser cache. Without this, an edit to
    # ``components.css`` or ``agent_hq.js`` may not show up until the
    # user does a manual hard-reload.
    app.state.static_version = str(int(time.time()))

    auth_routes.register(app, tm)
    events_routes.register(app)
    agents_routes.register(app)
    automations_routes.register(app)
    errors_routes.register(app)
    phase_routes.register(app)
    issues_routes.register(app)
    prefs_routes.register(app)
    pwa_routes.register(app)
    system_routes.register(app)

    # Idea Inbox routes — only registered when projects.yaml is present
    # (i.e. ``config`` resolved to a real Config above). The web app still
    # boots cleanly on machines without a config file.
    if config is not None:
        from agent_hq.idea.handler import IdeaHandler

        idea_handler = IdeaHandler(config, sent_store=sent_store)
        idea_routes.register(app, handler=idea_handler, config=config)
        app.state.idea_handler = idea_handler

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "boot_id": app.state.boot_id}

    @app.get("/")
    async def root(request: Request):
        cookie = request.cookies.get(COOKIE_NAME, "")
        if verify_cookie_value(cookie, tm):
            return RedirectResponse(url="/agents")
        return RedirectResponse(url="/login")

    return app
