"""Regression pins for #275: the agent preview wraps instead of scrolling
horizontally.

The Live preview is a ``<pre>`` of tmux capture output; ``white-space: pre``
plus ``overflow-x: auto`` gave it its own horizontal scrollbar on any line
wider than the detail column. The History view already wraps its turns; the
host gets an ``overflow-x`` backstop so neither view can pan sideways. CSS has
no unit-testable runtime here, so these pin the load-bearing rules the same
way ``test_mobile_overflow.py`` does; the behavioral check is the browser pass
in ``verify-changes``.
"""

from agent_hq.web.app import STATIC_DIR

from .test_web_layout import _rule_bodies


def test_live_preview_wraps_instead_of_scrolling():
    css = (STATIC_DIR / "ansi.css").read_text()
    bodies = _rule_bodies(css, ".pane-preview")
    assert bodies, "no .pane-preview rule"
    assert any("white-space: pre-wrap" in b for b in bodies)
    assert all("white-space: pre;" not in b for b in bodies)
    # A single token wider than the column must break inside itself.
    assert any("overflow-wrap: anywhere" in b for b in bodies)
    assert all("overflow-x: auto" not in b and "overflow-x: scroll" not in b for b in bodies)


def test_history_turns_wrap_consistently_with_live():
    css = (STATIC_DIR / "ansi.css").read_text()
    bodies = _rule_bodies(css, ".history-turn")
    assert bodies, "no .history-turn rule"
    assert any("white-space: pre-wrap" in b for b in bodies)
    assert any("overflow-wrap: anywhere" in b for b in bodies)


def test_preview_host_never_scrolls_horizontally():
    css = (STATIC_DIR / "layout.css").read_text()
    bodies = _rule_bodies(css, ".pane-preview-host")
    assert bodies, "no .pane-preview-host rule"
    assert any("overflow-x: hidden" in b for b in bodies)
