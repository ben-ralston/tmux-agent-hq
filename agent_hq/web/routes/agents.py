"""Agents routes — list view, kanban view, detail, per-card swap, preview.

Source of truth for "what agents exist right now" is the in-process
``Watcher`` snapshot. When the watcher hasn't been started (e.g. inside
pytest), we fall back to a fresh defensive read of ``cache.json`` so
tests can drop a cache fixture and exercise the route.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

from ansi2html import Ansi2HTMLConverter
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from agent_hq.capabilities import supports_harness
from agent_hq.dispatch import resolve_and_validate_harness
from agent_hq.enrichment import compute_content_fingerprint
from agent_hq.models import Agent, AgentState, DevPhase
from agent_hq.state import load_access_times, load_inactive_state
from agent_hq.transcript_history import HistoryPage, locate_transcript, read_history_page
from agent_hq.web.errors import ActionError, safe_kill, safe_rename, safe_set_mute
from agent_hq.web.events import Event
from agent_hq.web.watcher import CacheHealth, _load_agents_defensively, _read_cache

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
log = logging.getLogger(__name__)


def _slug_filter(pane_id: str) -> str:
    """``%5`` → ``5``. Avoids Starlette's percent-decode mangling URLs."""
    return pane_id[1:] if pane_id.startswith("%") else pane_id


templates.env.filters["pane_slug"] = _slug_filter


def pane_id_for(slug: str) -> str:
    """Inverse of ``_slug_filter``: ``5`` → ``%5``."""
    return f"%{slug}" if not slug.startswith("%") else slug


def _snapshot(app: FastAPI) -> list[Agent]:
    """Current agents from the watcher, or a fresh cache read fallback.

    The watcher's snapshot already has resolver-built ``phase_snapshot``
    on each agent. In the no-watcher fallback (tests / boot), the cache
    read returns agents whose snapshots were built by the daemon — no
    further override application needed.
    """
    watcher = getattr(app.state, "watcher", None)
    if watcher is not None:
        return watcher.snapshot()
    return _load_agents_defensively()


def _agents_and_health(app: FastAPI) -> tuple[list[Agent], CacheHealth]:
    """The view's agents and what the tab says about them (#338).

    One source for both: the watcher, or — with no watcher (tests / boot) —
    a single age-agnostic cache read, so the cards and the banner never
    judge freshness differently (an old cache shows its cards under the
    stale banner).
    """
    watcher = getattr(app.state, "watcher", None)
    if watcher is not None:
        return watcher.snapshot(), watcher.health()
    return _read_cache(max_age=None)


async def _publish_invalidate(app: FastAPI, pane_id: str, *, re_resolve: bool = False) -> None:
    """Publish ``agent.invalidated`` so connected SSE tabs re-fetch the card.

    ``re_resolve=True`` (phase routes) routes through
    ``watcher.refresh_pane`` so the resolver re-reads the just-written
    overrides.json. Plain action routes (mute / rename / kill / jump)
    don't change anything the resolver reads, so they publish a bare
    event directly — going through ``refresh_pane`` would burn a hook-file
    read plus git / ``gh pr view`` calls for nothing.

    Falls back to a direct broker publish when the watcher isn't running
    (tests / ``--no-daemon`` boot before first cache load).
    """
    watcher = getattr(app.state, "watcher", None)
    if re_resolve and watcher is not None:
        await watcher.refresh_pane(pane_id)
        return
    broker = getattr(app.state, "broker", None)
    if broker is not None:
        # Carry the renumber-stable key (#132) so the client can find the
        # agent's card by key. The action routes (mute / jump / rename /
        # kill) don't change the pane, so a slug lookup would also work —
        # but keying consistently keeps the client path uniform.
        agent = next((a for a in _snapshot(app) if a.pane_id == pane_id), None)
        data = {"pane_id": pane_id}
        if agent is not None:
            data["key"] = agent.stable_key
        await broker.publish(
            Event(
                topic="agent.invalidated",
                data=data,
                fingerprint=None,
            )
        )


def _muted_pane_ids() -> set[str]:
    return set(load_inactive_state().keys())


def _pinned_keys(app: FastAPI) -> set[str]:
    """Stable keys pinned to the top, from the injected store. #141"""
    store = getattr(app.state, "pinned_store", None)
    return store.keys() if store is not None else set()


def _autopilot_branches(app: FastAPI) -> set[str]:
    """Branches the nightly autopilot launched (#302), for the ⏵ card badge.

    Read from the attempt records rather than guessed from the ``-ap…``
    suffix, so a hand-named branch is never badged. Never raises: a missing
    or unreadable state file just means no badges."""
    store = getattr(app.state, "autopilot_store", None)
    if store is None:
        return set()
    try:
        return {a.branch for a in store.snapshot().attempts.values()}
    except Exception:  # noqa: BLE001 — a badge must never break the agent list
        log.warning("autopilot branches unavailable", exc_info=True)
        return set()


async def _publish_pins_changed(app: FastAPI, pane_id: str, key: str) -> None:
    """Publish ``agent.pins_changed`` so every connected tab re-fetches its
    ``#agents-view`` and the pinned agent floats into / out of the top
    section without a full reload. Matches ``agent.*`` so it rides the
    existing SSE subscription (#141)."""
    await _publish_section_move(app, "agent.pins_changed", pane_id, key)


async def _publish_mute_changed(app: FastAPI, pane_id: str, key: str) -> None:
    """Publish ``agent.mute_changed`` so connected tabs re-fetch their view
    and the card sinks into / rises out of the bottom Muted section (#278).
    A per-card swap can't move a card between sections — same reason pins
    publish their own event."""
    await _publish_section_move(app, "agent.mute_changed", pane_id, key)


async def _publish_section_move(app: FastAPI, topic: str, pane_id: str, key: str) -> None:
    broker = getattr(app.state, "broker", None)
    if broker is not None:
        await broker.publish(
            Event(
                topic=topic,
                data={"pane_id": pane_id, "key": key},
                fingerprint=None,
            )
        )


_ansi = Ansi2HTMLConverter(inline=True, scheme="xterm")

# Strip OSC (Operating System Command) escape sequences before ANSI→HTML
# conversion. ansi2html turns OSC-8 hyperlinks into raw <a href=…> tags,
# which would be rendered as live HTML by the |safe filter — letting
# anything in tmux's pane (a malicious agent, a piped log) emit a
# javascript: link or an attribute injection. The preview only ever
# shows monospace text, so OSC sequences (window title, hyperlinks,
# clipboard, etc.) have nothing useful to contribute. Drop them.
# Pattern: ESC ] … (BEL | ESC \ | end-of-string). Including ``\Z`` covers
# unterminated OSC sequences (truncated capture, partial write) so an OSC-8
# hyperlink missing its terminator still gets stripped instead of leaking
# through to ansi2html.
_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\|\Z)", re.DOTALL)
# Every escape sequence left that is not CSI (``ESC [``) is dropped whole in
# one linear pass (#263 F1): charset designators (``ESC ( B``, which ``tput
# sgr0`` emits), two-byte escapes (``ESC 7``, ``ESC =``) and a trailing lone
# ESC. A match always starts at an ESC and never consumes the ``[`` after a
# kept one, so afterwards every surviving ESC opens a CSI sequence — no
# ``ESC ]``, and so no OSC-8 link, can exist however the input nests.
_NON_CSI_ESC_RE = re.compile(r"\x1b(?:[()*+\-./].?|[^\[]|\Z)", re.DOTALL)


def ansi_to_html(raw: str) -> str:
    """OSC-stripped ANSI→HTML, safe to render with the ``|safe`` filter.

    Shared by the agent pane preview and the automations log drill-down —
    both show untrusted monospace text, so both need the OSC strip above.
    Removing an inner sequence can splice its neighbours into a fresh
    ``ESC ]`` introducer, so after the OSC strip every ESC that does not
    start a CSI sequence goes too (#263 F1).
    """
    text = _NON_CSI_ESC_RE.sub("", _OSC_RE.sub("", raw))
    return _ansi.convert(text, full=False)


def capture_pane(pane_id: str, lines: int = 200) -> str:
    """Shell out to ``tmux capture-pane -e`` with ANSI escapes preserved.

    ``-e`` keeps colors, ``-S -<lines>`` walks ``lines`` rows up from the
    current bottom — same recipe used elsewhere in the codebase. Order
    matters: ``-e`` must follow ``-t pane_id``.
    """
    cmd = ["tmux", "capture-pane", "-e", "-p", "-t", pane_id, "-S", f"-{lines}"]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _locate_transcript(agent: Agent) -> str | None:
    """History-availability gate for ``agent`` (#171). See ``locate_transcript``."""
    return locate_transcript(agent.cwd, agent.claude_transcript_session_id)


def _ordered_for_list(agents: list[Agent], muted: set[str]) -> list[Agent]:
    """Sort: WAITING first, then by recent access, muted to bottom.

    Mirrors the TUI's ``sort_agents_by_priority`` so the list view here
    matches what the user sees in ``agent-hq --dashboard``.
    """
    access_times = load_access_times()

    def priority(agent: Agent) -> tuple[int, int, float]:
        is_muted = 1 if agent.pane_id in muted else 0
        is_waiting = 1 if agent.state == AgentState.WAITING else 0
        last_access = access_times.get(agent.pane_id, 0.0)
        return (is_muted, -is_waiting, -last_access)

    return sorted(agents, key=priority)


async def _agents_view_context(app: FastAPI, view: str) -> tuple[str, dict]:
    """Build ``(template_name, context)`` for the list/phase/kanban view.

    Shared by the partial routes and the page GET's inline first paint
    (#226) — the data is an in-memory watcher snapshot plus two small
    threadpooled file reads, so rendering it inline costs ~nothing and
    saves the 'Discovering agents…' second round trip.
    """
    if getattr(app.state, "watcher", None) is not None:
        agents, health = _agents_and_health(app)
    else:
        agents, health = await run_in_threadpool(_agents_and_health, app)
    muted = await run_in_threadpool(_muted_pane_ids)
    pinned = await run_in_threadpool(_pinned_keys, app)
    ordered = _ordered_for_list(agents, muted)
    # Pinned agents float into a top section regardless of mute/sort, and
    # muted agents sink into a bottom section below everything else — Done
    # included (#278). Pinned wins: a pinned-and-muted agent shows once, in
    # the pinned section. Both are removed from the body below (no
    # duplication).
    pinned_agents = [a for a in ordered if a.is_pinned_in(pinned)]
    rest = [a for a in ordered if not a.is_pinned_in(pinned)]
    muted_agents = [a for a in rest if a.pane_id in muted]
    rest = [a for a in rest if a.pane_id not in muted]
    notice = health.notice(time.time())
    sections = {
        # (kind, text) or None — rendered by partials/agents_status.html (#338).
        "agents_notice": notice,
        "muted": muted,
        "muted_agents": muted_agents,
        "pinned_agents": pinned_agents,
        "pinned": pinned,
        "autopilot_branches": await run_in_threadpool(_autopilot_branches, app),
    }
    if view == "list":
        return ("partials/agents_list.html", {"active": rest, **sections})
    buckets: dict[DevPhase, list[Agent]] = {phase: [] for phase in DevPhase}
    for agent in rest:
        buckets[agent.phase if agent.phase else DevPhase.ADHOC].append(agent)
    if view == "kanban":
        # Pinned agents render in a full-width band above the columns,
        # muted agents in one below; both removed from their phase column.
        return (
            "partials/agents_kanban.html",
            {
                "columns": [(phase, bucket) for phase, bucket in buckets.items() if bucket],
                **sections,
            },
        )
    return (
        "partials/agents_phase.html",
        {
            "groups": [(phase, bucket) for phase, bucket in buckets.items() if bucket],
            **sections,
        },
    )


def register(app: FastAPI) -> None:
    @app.get("/agents", response_class=HTMLResponse)
    async def agents_page(request: Request, view: str | None = None):
        # ``view`` is "list", "phase", or "kanban". List and phase are
        # reachable from the toolbar; kanban is still a valid value (so
        # ``?view=kanban`` works — and persists — by URL) but its toolbar
        # button is hidden (#127). An explicit valid ``?view=`` is a
        # deliberate toolbar click (verified: no nav link or htmx partial
        # carries the param), so it's saved to the shared prefs store; a
        # bare or unrecognized request restores the stored view, falling
        # back to phase (#176).
        store = getattr(app.state, "ui_prefs_store", None)
        if view in ("list", "phase", "kanban"):
            if store is not None:
                await run_in_threadpool(store.set_web_agents_view, view)
        else:
            stored = (
                await run_in_threadpool(store.get_web_agents_view) if store is not None else None
            )
            view = stored or "phase"
        # Inline first paint (#226): render the view partial into the page
        # itself. NOTE: this is a bare env render — no request-bound Jinja
        # globals (url_for etc.); the agents partials use none today.
        template_name, ctx = await _agents_view_context(app, view)
        initial_html = templates.get_template(template_name).render(**ctx)
        return templates.TemplateResponse(
            request,
            "agents.html",
            {"view": view, "initial_html": initial_html},
        )

    @app.get("/agents/list", response_class=HTMLResponse)
    async def agents_list(request: Request):
        template_name, ctx = await _agents_view_context(app, "list")
        return templates.TemplateResponse(request, template_name, ctx)

    @app.get("/agents/phase", response_class=HTMLResponse)
    async def agents_phase(request: Request):
        """Phase-grouped list (third toggle option). Mirrors the TUI's
        ``V`` mode: cards stacked under phase chips, populated phases
        only. Unlike kanban (which keeps zero-count columns visible
        for at-a-glance scan) the phase list omits empty groups.
        Inside each group, sort uses the same priority as the flat
        list — waiting first, then by recent access, muted to bottom —
        so the most-actionable agent is always at the top of its
        section."""
        template_name, ctx = await _agents_view_context(app, "phase")
        return templates.TemplateResponse(request, template_name, ctx)

    @app.get("/agents/kanban", response_class=HTMLResponse)
    async def agents_kanban(request: Request):
        # DevPhase enum order matches the kanban left-to-right: adhoc
        # → design → implement → qa → review → ready → done.
        # ``None``-phase agents collapse into adhoc — same convention as
        # the TUI dashboard. Empty phases are omitted entirely (rather
        # than rendered as thin strips); populated columns share the
        # full row so each gets readable width.
        template_name, ctx = await _agents_view_context(app, "kanban")
        return templates.TemplateResponse(request, template_name, ctx)

    @app.get("/agents/new", response_class=HTMLResponse)
    async def new_session_picker(request: Request):
        """Target picker for the "+ New session" header button (#169).

        Rendered into ``#overlay-slot``. Offers a Scratch session plus one
        entry per configured repo. Registered before the ``/agents/{slug}``
        routes so the ``{slug}`` catch-all doesn't swallow ``new``.
        """
        cfg = getattr(app.state, "config", None)
        repos = list(cfg.repos.values()) if cfg is not None else []
        # run_in_threadpool (Fix 7, multi-review wave): the probe shells out
        # to launch.sh; a hung/slow launcher must not stall the event loop.
        caps = await run_in_threadpool(app.state.capabilities_probe)
        codex_available = supports_harness(caps, "codex")
        default_harness = cfg.dispatch.default_harness if cfg is not None else "claude"
        return templates.TemplateResponse(
            request,
            "partials/new_session_picker.html",
            {
                "repos": repos,
                "codex_available": codex_available,
                "default_harness": default_harness,
            },
        )

    @app.post("/agents/new", response_class=HTMLResponse)
    async def new_session_create(
        request: Request,
        target: str = Form(...),
        focus: str = Form(default="1"),
        harness: str = Form(default=""),
    ):
        """Create a chat window for ``target`` and jump to it (#169).

        ``target`` is ``scratch`` (find-or-create the ``scratch`` session,
        cwd from ``dispatch.scratch_cwd``) or ``repo:<owner/name>`` (the
        repo's main session via the same find-or-create dispatch uses).
        The new pane runs a bare interactive harness REPL; there's no agent
        card yet — discovery surfaces it once the process is seen — so the
        jump happens here rather than through a card. ``focus`` matches the
        jump route's iOS semantics (#137). ``harness`` (#205 Stage 1B Task
        15): a blank field resolves to the config default; no model/effort
        here — a bare chat REPL has no such concept, unlike a dispatched
        worktree — so ``resolve_model_effort=False`` (Fix 6, multi-review
        wave) skips resolving/validating a configured model/effort default
        this flow never uses; a bad
        ``config.dispatch.harnesses[...].model`` elsewhere must not 400 a
        request that has no model field to begin with.
        """
        import asyncio

        from agent_hq import dispatch
        from agent_hq.web import jump

        cfg = getattr(app.state, "config", None)

        harness_val = harness or None
        # run_in_threadpool (Fix 7, multi-review wave): the probe shells out
        # to launch.sh; a hung/slow launcher must not stall the event loop.
        caps = await run_in_threadpool(app.state.capabilities_probe)
        try:
            resolved_harness, _, _ = resolve_and_validate_harness(
                cfg,
                caps,
                harness_val,
                resolve_model_effort=False,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        if target == "scratch":
            cwd = cfg.dispatch.scratch_cwd if cfg is not None else Path.home()

            def resolve_and_open() -> tuple[str | None, str]:
                existing = dispatch._find_session_by_name("scratch")
                pane = dispatch.open_chat_window(
                    existing_session=existing,
                    new_session_name="scratch",
                    cwd=cwd,
                    harness=resolved_harness,
                )
                return pane, "scratch"

        elif target.startswith("repo:"):
            repo = (cfg.repos if cfg is not None else {}).get(target[len("repo:") :])
            if repo is None:
                raise HTTPException(status_code=404, detail="unknown target")

            def resolve_and_open() -> tuple[str | None, str]:
                existing = dispatch._find_repo_session(repo.local_path)
                name = existing or dispatch._repo_session_name(repo.local_path)
                pane = dispatch.open_chat_window(
                    existing_session=existing,
                    new_session_name=name,
                    cwd=repo.local_path,
                    harness=resolved_harness,
                )
                return pane, name

        else:
            raise HTTPException(status_code=404, detail="unknown target")

        try:
            pane_id, session_label = await asyncio.wait_for(
                run_in_threadpool(resolve_and_open), timeout=5.0
            )
        except asyncio.TimeoutError:
            return templates.TemplateResponse(
                request,
                "partials/toast.html",
                {"message": "New session timed out.", "level": "error"},
            )
        if pane_id is None:
            return templates.TemplateResponse(
                request,
                "partials/toast.html",
                {"message": "Could not create the session window.", "level": "error"},
            )
        try:
            # 4s, not the jump route's 2s: the Ghostty osascript alone can
            # take ~2.1s, which would false-negative the toast even though
            # the tmux switch already happened.
            ok = await asyncio.wait_for(
                run_in_threadpool(jump.perform_jump, pane_id, focus != "0"),
                timeout=4.0,
            )
        except asyncio.TimeoutError:
            ok = False
        await _publish_invalidate(app, pane_id)
        if not ok:
            return templates.TemplateResponse(
                request,
                "partials/toast.html",
                {
                    "message": f"Opened claude in {session_label}, but could not jump.",
                    "level": "error",
                },
            )
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {"message": f"Opened claude in {session_label}.", "level": "success"},
        )

    @app.get("/agents/{slug}/preview", response_class=HTMLResponse)
    async def agent_preview(request: Request, slug: str, lines: int = 200):
        pane_id = pane_id_for(slug)
        agents = _snapshot(app)
        if not any(a.pane_id == pane_id for a in agents):
            raise HTTPException(status_code=404, detail="agent not found")
        # Use the module-level capture_pane symbol so tests can patch it.
        from agent_hq.web.routes import agents as self_mod

        raw = await run_in_threadpool(self_mod.capture_pane, pane_id, lines)
        ansi_html = ansi_to_html(raw)
        return templates.TemplateResponse(
            request,
            "partials/preview.html",
            {"slug": slug, "ansi_html": ansi_html},
        )

    async def _persist_preview_mode(mode: str) -> None:
        """Record the sticky Live|History choice (#171 follow-up).

        Only explicit toggle clicks pass ``persist=1``; the initial-load
        fetch and the pagination sentinel never do, so browsing history
        pages doesn't rewrite the pref.
        """
        store = getattr(app.state, "ui_prefs_store", None)
        if store is not None:
            await run_in_threadpool(store.set_web_preview_mode, mode)

    async def _preview_mode_for(has_history: bool) -> str:
        """The mode the detail body should open in: the stored preference,
        downgraded to live when this agent has no transcript to render."""
        store = getattr(app.state, "ui_prefs_store", None)
        if not has_history or store is None:
            return "live"
        pref = await run_in_threadpool(store.get_web_preview_mode)
        return "history" if pref == "history" else "live"

    @app.get("/agents/{slug}/preview/live", response_class=HTMLResponse)
    async def agent_preview_live(request: Request, slug: str, persist: bool = False):
        """Live-mode inner element for the preview host (#171).

        Swapped into the host when the user toggles back from History;
        carries the every-3s poll, so inserting it re-establishes live
        updates (and removing it — the History swap — stops them).
        """
        pane_id = pane_id_for(slug)
        agents = _snapshot(app)
        if not any(a.pane_id == pane_id for a in agents):
            raise HTTPException(status_code=404, detail="agent not found")
        if persist:
            await _persist_preview_mode("live")
        return templates.TemplateResponse(
            request,
            "partials/preview_live.html",
            {"slug": slug},
        )

    @app.get("/agents/{slug}/history", response_class=HTMLResponse)
    async def agent_history(
        request: Request, slug: str, before: int | None = None, persist: bool = False
    ):
        """One page of transcript-local history (#171).

        Renders turns parsed from the agent's Claude transcript JSONL —
        zero pane interaction (the whole point: Claude's fullscreen
        renderer keeps scrollback off-tmux, and remote-controlling the
        pane was rejected hands-on in PR #152). ``before`` is the byte
        cursor from the previous page's top sentinel; the sentinel's
        ``hx-trigger="intersect once"`` makes scrolling to the top load
        the earlier page (infinite upward scroll).
        """
        pane_id = pane_id_for(slug)
        agents = _snapshot(app)
        agent = next((a for a in agents if a.pane_id == pane_id), None)
        if not agent:
            raise HTTPException(status_code=404, detail="agent not found")
        if persist:
            await _persist_preview_mode("history")
        path = await run_in_threadpool(_locate_transcript, agent)
        if path is None:
            page: HistoryPage | None = None
        else:
            page = await run_in_threadpool(read_history_page, path, before)
        return templates.TemplateResponse(
            request,
            "partials/history_page.html",
            {"slug": slug, "page": page},
        )

    @app.get("/agents/{slug}/card", response_class=HTMLResponse)
    async def agent_card(request: Request, slug: str, variant: str = "comfortable"):
        """Single-card swap target for the SSE listener.

        Returns the card alone (no surrounding wrapper / no separator)
        so the front-end can replace ``outerHTML`` of the previous card
        in place.

        ``variant`` selects the template — ``comfortable`` (list/phase
        views, swap target ``#detail-slot``) or ``kanban`` (swap target
        ``#overlay-slot``). The SSE listener reads ``card--kanban`` off
        the existing card to pick the right variant; without it, every
        kanban card invalidation would replace the kanban card with a
        comfortable list card pointed at the wrong slot.
        """
        pane_id = pane_id_for(slug)
        agents = _snapshot(app)
        agent = next((a for a in agents if a.pane_id == pane_id), None)
        if not agent:
            raise HTTPException(status_code=404, detail="agent not found")
        muted = await run_in_threadpool(_muted_pane_ids)
        pinned = await run_in_threadpool(_pinned_keys, app)
        template = (
            "partials/card_kanban.html" if variant == "kanban" else "partials/card_comfortable.html"
        )
        return templates.TemplateResponse(
            request,
            template,
            {
                "agent": agent,
                "muted": muted,
                "pinned": pinned,
                "autopilot_branches": await run_in_threadpool(_autopilot_branches, app),
            },
        )

    @app.post("/agents/{slug}/jump", response_class=HTMLResponse)
    async def agent_jump(request: Request, slug: str, focus: str = Form(default="1")):
        """tmux switch-client + AppleScript focus.

        The iOS button posts ``focus=0`` (#137): the pane switch still
        runs (a phone terminal attaching over SSH sees the right agent)
        but the Ghostty raise on the Mac is skipped. Both subprocess
        calls block; we use a 2s ceiling via ``asyncio.wait_for`` so a
        stalled focus script doesn't pin the request handler
        indefinitely.
        """
        import asyncio

        from agent_hq.web import jump

        pane_id = pane_id_for(slug)
        agents = _snapshot(app)
        if not any(a.pane_id == pane_id for a in agents):
            raise HTTPException(status_code=404, detail="agent not found")
        try:
            # 4s, matching /agents/new: the Ghostty osascript inside
            # perform_jump alone can take ~2.1s, so a 2s outer ceiling
            # reported successful jumps as timeouts.
            ok = await asyncio.wait_for(
                run_in_threadpool(jump.perform_jump, pane_id, focus != "0"),
                timeout=4.0,
            )
        except asyncio.TimeoutError:
            return templates.TemplateResponse(
                request,
                "partials/toast.html",
                {"message": "Jump timed out.", "level": "error"},
            )
        if not ok:
            return templates.TemplateResponse(
                request,
                "partials/toast.html",
                {"message": "Could not jump.", "level": "error"},
            )
        # Jump records access time, which affects sort order — publish so
        # connected tabs re-render their list.
        await _publish_invalidate(app, pane_id)
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {"message": "Jumped.", "level": "success"},
        )

    @app.post("/agents/{slug}/mute", response_class=HTMLResponse)
    async def agent_mute(request: Request, slug: str, state: str = Form(...)):
        pane_id = pane_id_for(slug)
        agents = _snapshot(app)
        if not any(a.pane_id == pane_id for a in agents):
            raise HTTPException(status_code=404, detail="agent not found")
        fp = await run_in_threadpool(compute_content_fingerprint, pane_id)
        muted = state.lower() == "true"
        await run_in_threadpool(safe_set_mute, pane_id, muted=muted, fingerprint=fp)
        # Mute moves the card between sections (#278) — a card swap alone
        # can't express that, so tabs re-fetch the whole view (which also
        # refreshes the open detail). No separate ``agent.invalidated`` here:
        # the whole-view refetch re-renders the card, and a card swap landing
        # before it would see a muted card in the wrong section and trigger a
        # second refetch (code-review round 2).
        agent = next(a for a in agents if a.pane_id == pane_id)
        await _publish_mute_changed(app, pane_id, agent.stable_key)
        msg = "Muted." if muted else "Unmuted."
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {"message": msg, "level": "info"},
        )

    @app.post("/agents/{slug}/pin", response_class=HTMLResponse)
    async def agent_pin(request: Request, slug: str, state: str = Form(...)):
        """Toggle pin on an agent. ``state=true`` pins, ``state=false`` unpins.

        Pins are keyed on ``Agent.stable_key`` (#132/#141) so they survive a
        tmux pane renumber. Publishes ``agent.pins_changed`` so connected tabs
        re-fetch their view and the card floats to / from the top section
        without a full reload. #141

        Pinning writes ONLY the current ``stable_key`` (#205 Codex review) —
        also writing the ``legacy_stable_key`` form left a ``tool:cwd:pane_id``
        entry behind that a future unrelated session in the same pane and cwd
        would collide with. A pre-capture codex pin still lands in legacy form
        naturally (that IS its ``stable_key`` then) and
        ``migrate_legacy_pins`` re-keys it on the UUID once captured. The
        tradeoff: a codex UUID flap (dedupe / 3-day rollout cutoff) hides the
        pin until the UUID returns, instead of transferring it to strangers.
        Unpinning clears both ``stable_key`` and ``legacy_stable_key`` (#205
        Task 7) — a codex agent may have been pinned before its thread UUID
        was captured, under the pre-upgrade legacy form; clearing only the
        current key would leave that old entry pinned forever."""
        pane_id = pane_id_for(slug)
        agents = _snapshot(app)
        agent = next((a for a in agents if a.pane_id == pane_id), None)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        key = agent.stable_key
        legacy_key = agent.legacy_stable_key
        pinned = state.lower() == "true"
        store = app.state.pinned_store
        if pinned:
            await run_in_threadpool(store.add, key)
        else:
            await run_in_threadpool(store.remove, key)
            await run_in_threadpool(store.remove, legacy_key)
        await _publish_pins_changed(app, pane_id, key)
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {"message": "Pinned." if pinned else "Unpinned.", "level": "info"},
        )

    @app.post("/agents/{slug}/rename", response_class=HTMLResponse)
    async def agent_rename(request: Request, slug: str, name: str = Form(default="")):
        pane_id = pane_id_for(slug)
        agents = _snapshot(app)
        if not any(a.pane_id == pane_id for a in agents):
            raise HTTPException(status_code=404, detail="agent not found")
        try:
            # ``None`` triggers the auto-rename path; explicit name
            # overrides it.
            await run_in_threadpool(safe_rename, pane_id, name or None)
        except ActionError as exc:
            return templates.TemplateResponse(
                request,
                "partials/toast.html",
                {"message": str(exc), "level": "error"},
            )
        await _publish_invalidate(app, pane_id)
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {
                "message": f"Renamed to {name}." if name else "Rename requested.",
                "level": "success",
            },
        )

    @app.get("/agents/{slug}/overlay", response_class=HTMLResponse)
    async def agent_overlay(request: Request, slug: str):
        """Floating overlay panel — kanban click target.

        Returns the agent detail wrapped in a fixed-positioned overlay
        with a close button. Same body content as the side pane / sheet
        (shared via ``partials/detail_body.html``); only the chrome
        differs. Closing the overlay is JS-only (clear ``#overlay-slot``)
        — there's no DELETE endpoint."""
        pane_id = pane_id_for(slug)
        agents = _snapshot(app)
        agent = next((a for a in agents if a.pane_id == pane_id), None)
        if not agent:
            raise HTTPException(status_code=404, detail="agent not found")
        muted = pane_id in await run_in_threadpool(_muted_pane_ids)
        pinned = await run_in_threadpool(_pinned_keys, app)
        has_history = await run_in_threadpool(_locate_transcript, agent) is not None
        return templates.TemplateResponse(
            request,
            "partials/detail_overlay.html",
            {
                "agent": agent,
                "slug": slug,
                "muted": muted,
                "pinned": pinned,
                "has_history": has_history,
                "preview_mode": await _preview_mode_for(has_history),
            },
        )

    @app.post("/agents/{slug}/kill", response_class=HTMLResponse)
    async def agent_kill(request: Request, slug: str):
        pane_id = pane_id_for(slug)
        agents = _snapshot(app)
        if not any(a.pane_id == pane_id for a in agents):
            raise HTTPException(status_code=404, detail="agent not found")
        try:
            await run_in_threadpool(safe_kill, pane_id)
        except ActionError as exc:
            return templates.TemplateResponse(
                request,
                "partials/toast.html",
                {"message": str(exc), "level": "error"},
            )
        await _publish_invalidate(app, pane_id)
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {"message": "Killed.", "level": "success"},
        )

    # NOTE: keep this dynamic route LAST among ``/agents/...`` routes so
    # the literal paths (``/agents/list``, ``/agents/kanban``) win.
    @app.get("/agents/{slug}", response_class=HTMLResponse)
    async def agent_detail(request: Request, slug: str):
        pane_id = pane_id_for(slug)
        agents = _snapshot(app)
        agent = next((a for a in agents if a.pane_id == pane_id), None)
        if not agent:
            raise HTTPException(status_code=404, detail="agent not found")
        muted = pane_id in await run_in_threadpool(_muted_pane_ids)
        pinned = await run_in_threadpool(_pinned_keys, app)
        has_history = await run_in_threadpool(_locate_transcript, agent) is not None
        return templates.TemplateResponse(
            request,
            "partials/detail.html",
            {
                "agent": agent,
                "slug": slug,
                "muted": muted,
                "pinned": pinned,
                "has_history": has_history,
                "preview_mode": await _preview_mode_for(has_history),
            },
        )
