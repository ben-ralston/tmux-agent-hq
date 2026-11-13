"""Regression guards for the agents-view scroll layout (issue #120).

CSS scroll behavior is a browser-render property pytest can't exercise
directly — the real acceptance check is manual browser verification. These
tests are a lightweight guard (mirroring ``test_web_pwa.py``'s static-asset
reads) that the desktop list/preview panes are wired as *independent* scroll
regions and that the old whole-page-scrolls-the-preview-away rule is gone.
"""

from agent_hq.web.app import STATIC_DIR


def _layout_css() -> str:
    return (STATIC_DIR / "layout.css").read_text()


def _rule_bodies(css: str, selector: str) -> list[str]:
    """Return the declaration bodies of every flat rule matching ``selector``.

    Rules in layout.css are flat (no nested braces), so a simple first-brace /
    first-close-brace slice is sufficient. Matching ``selector`` immediately
    followed by ``{`` (modulo a space) avoids matching descendant selectors
    that merely start with the same text (e.g. ``foo`` vs ``foo .bar``).
    """
    bodies = []
    for opener in (selector + " {", selector + "{"):
        start = 0
        while (idx := css.find(opener, start)) != -1:
            brace = css.find("{", idx)
            end = css.find("}", brace)
            bodies.append(css[brace + 1 : end])
            start = end + 1
    return bodies


def _rule_body(css: str, selector: str) -> str | None:
    bodies = _rule_bodies(css, selector)
    return bodies[0] if bodies else None


def test_shell_is_viewport_locked_for_list_and_phase_views():
    """The agent list/phase shell is capped to the viewport so the two panes
    own their own scroll — scoped via :has() so kanban + other pages are
    excluded and keep normal full-page scrolling."""
    css = _layout_css()
    body = _rule_body(css, ".app-shell:has(.agents-layout:not(.agents-layout--kanban))")
    assert body is not None, "missing the scoped viewport-lock rule for the agents shell"
    assert "height: 100vh" in body
    assert "overflow: hidden" in body


def test_agent_list_pane_is_its_own_scroll_container():
    """The left list pane (.app-main) scrolls independently of the preview."""
    css = _layout_css()
    body = _rule_body(css, ".agents-layout:not(.agents-layout--kanban) .app-main")
    assert body is not None, "missing the list-pane scroll rule"
    assert "overflow-y: auto" in body
    assert "min-height: 0" in body


def test_grid_stretches_its_columns_to_the_locked_height():
    """The list/phase grid must stretch both columns to fill the locked
    region so each can be its own scroller (replaces the old
    ``align-items: start``)."""
    css = _layout_css()
    body = _rule_body(css, ".agents-layout:not(.agents-layout--kanban)")
    assert body is not None, "missing the non-kanban agents-layout rule"
    assert "align-items: stretch" in body
    assert "min-height: 0" in body


def test_detail_pane_fills_its_cell_and_does_not_scroll_with_the_page():
    """The preview pane must fill its cell, not stick-then-release. The old
    ``position: sticky; top: 60px`` (which let the preview scroll out of view)
    is gone, and no ``.detail-pane`` rule is sticky."""
    css = _layout_css()
    # ``top: 60px`` was unique to the detail-pane sticky rule.
    assert "top: 60px" not in css, "detail-pane is still position:sticky (issue #120)"
    detail_bodies = _rule_bodies(css, ".detail-pane")
    assert detail_bodies, "missing .detail-pane rules"
    # No detail-pane rule may reintroduce sticky positioning.
    assert not any("position: sticky" in b for b in detail_bodies), (
        "a .detail-pane rule is position:sticky again (issue #120)"
    )
    # The desktop pane positively fills its stretched grid cell.
    assert any("height: 100%" in b for b in detail_bodies), (
        "desktop .detail-pane no longer fills its cell with height: 100%"
    )
