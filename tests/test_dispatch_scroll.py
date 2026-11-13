"""Regression pins for the #253 dispatch scroll-jump fix.

Every control on /dispatch — the issue checkbox, Group/Ungroup, Dispatch,
the header ``↻``, the sort select, the filter chips — is wired
``hx-target="#dispatch-container" hx-swap="outerHTML"``, so a single tap
tears down and rebuilds the whole scrollable page content. The window is
the scroll container there (``.dispatch-list`` has no ``overflow`` of its
own), and Chrome's scroll anchoring, having lost its anchor node,
re-anchors against the freshly inserted list and throws the viewport to the
bottom of the page.

The cure is the same save/restore shape ``agent_hq.js`` already uses for the
chip row's horizontal ``scrollLeft``, one axis up, plus an
``overflow-anchor`` opt-out so anchoring can't re-shift the page after the
restore. Neither has a runtime the test suite can drive, so these pin the
load-bearing source the way ``test_mobile_overflow.py`` and
``test_dispatch_state_js.py`` do; the behavioral proof is the 390px browser
pass in ``verify-changes`` Phase E.
"""

from __future__ import annotations

import re

from agent_hq.web.app import STATIC_DIR

from .test_web_layout import _rule_bodies


def _js() -> str:
    return (STATIC_DIR / "agent_hq.js").read_text()


def _listener_bodies(js: str, event: str) -> list[str]:
    """Bodies of every ``addEventListener(<event>, function (evt) {…})``.

    Listeners in agent_hq.js sit at one indent level inside the file's IIFE,
    so they close on a line of exactly ``  });``.
    """
    pattern = r'addEventListener\("' + re.escape(event) + r'", function \(evt\) \{(.*?)\n  \}\);'
    return re.findall(pattern, js, re.S)


def test_dispatch_layout_opts_out_of_scroll_anchoring():
    """Chrome must not re-anchor the page against the rebuilt issue list."""
    css = (STATIC_DIR / "layout.css").read_text()
    bodies = _rule_bodies(css, ".dispatch-layout")
    assert bodies, "no .dispatch-layout rule"
    assert any("overflow-anchor: none" in b for b in bodies)


def test_dispatch_container_is_covered():
    """The JS save/restore names the container it preserves explicitly."""
    match = re.search(r"var SCROLL_PRESERVING_CONTAINERS = (.*?);", _js(), re.S)
    assert match, "SCROLL_PRESERVING_CONTAINERS selector not found"
    assert "#dispatch-container" in match.group(1)


def test_container_is_matched_exactly_never_via_an_ancestor():
    """Both htmx events hand us the container itself, so an exact match is
    the whole job. An ancestor branch (``node.querySelector(...)``) must NOT
    come back: it has no caller here, and under a future ``hx-boost`` on the
    nav it would match the old page's ``<body>`` on beforeSwap and the new
    page's on afterSettle — carrying /dispatch's offset onto the next page
    instead of landing at the top (PR #256 review)."""
    match = re.search(r"function isScrollPreservingContainer\(node\) \{(.*?)\n  \}", _js(), re.S)
    assert match, "isScrollPreservingContainer helper not found"
    body = match.group(1)
    assert "node.matches(SCROLL_PRESERVING_CONTAINERS)" in body
    assert "querySelector" not in body


def test_saved_offset_is_cleared_when_the_swap_never_happens():
    """A 4xx/5xx fires beforeSwap but never settles. Without an explicit
    clear the offset lingers until some later swap happens to consume it."""
    match = re.search(r"\[([^\]]*htmx:responseError[^\]]*)\]\.forEach", _js(), re.S)
    assert match, "no error-event list clears the saved offset"
    events = match.group(1)
    for name in ("htmx:responseError", "htmx:sendError", "htmx:swapError"):
        assert name in events
    # htmx:afterRequest fires BEFORE afterSettle (default 20ms settle delay),
    # so clearing there would defeat the restore on the success path.
    assert "htmx:afterRequest" not in events


def test_restore_is_re_applied_on_the_next_frame():
    """scrollTo clamps against the document height as it stands at settle.
    If the rebuilt container grows a frame later (web fonts resolving, the
    --header-h ResizeObserver re-laying the sticky filter bar) a single
    synchronous pass lands short. The preview-scroll code above does the
    same double pass for the same reason."""
    bodies = [b for b in _listener_bodies(_js(), "htmx:afterSettle") if "savedPageScroll" in b]
    assert bodies, "no htmx:afterSettle listener restores the page scroll"
    body = bodies[0]
    assert "requestAnimationFrame" in body
    assert body.count("window.scrollTo") == 2


def test_page_scroll_is_saved_before_the_container_is_torn_down():
    js = _js()
    bodies = [
        b for b in _listener_bodies(js, "htmx:beforeSwap") if "isScrollPreservingContainer" in b
    ]
    assert bodies, "no htmx:beforeSwap listener saves the page scroll"
    body = bodies[0]
    assert "window.scrollY" in body
    assert "savedPageScroll" in body


def test_page_scroll_is_restored_after_the_new_container_settles():
    bodies = [b for b in _listener_bodies(_js(), "htmx:afterSettle") if "savedPageScroll" in b]
    assert bodies, "no htmx:afterSettle listener restores the page scroll"
    body = bodies[0]
    assert "window.scrollTo" in body
    # Restoring on an unrelated settle (the detail modal, an agent card)
    # would yank the viewport of pages this fix has no business touching.
    assert "isScrollPreservingContainer" in body
