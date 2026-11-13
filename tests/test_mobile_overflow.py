"""Regression pins for the #242 mobile horizontal-overflow fix.

At phone width the page must never scroll horizontally: card meta rows wrap
instead of pushing past the card border, and the document carries an
``overflow-x: clip`` backstop so any one over-wide child can't pan the whole
page. CSS has no unit-testable runtime here, so these pin the load-bearing
rules the same way ``test_web_layout.py`` pins the scroll layout (whose
``_rule_bodies`` helper — every matching flat rule, not just the first — is
reused so a later overriding rule can't hide from the pin); the behavioral
check is the 390px browser pass in ``verify-changes`` Phase E.
"""

from agent_hq.web.app import STATIC_DIR

from .test_web_layout import _rule_bodies


def test_issue_meta_wraps_and_body_shrinks():
    css = (STATIC_DIR / "components.css").read_text()
    meta_bodies = _rule_bodies(css, ".issue-meta")
    assert meta_bodies, "no .issue-meta rule"
    assert all("flex-wrap: wrap" in b for b in meta_bodies)
    # The shrink constraint that lets the card's grid track narrow lives on
    # .issue-body (the flex/grid item), not .issue-meta — pin the one that
    # is actually load-bearing.
    body_bodies = _rule_bodies(css, ".issue-body")
    assert body_bodies and all("min-width: 0" in b for b in body_bodies)
    # A single token wider than the card must break inside itself rather
    # than hit the page clip and truncate unreadably.
    repo_bodies = _rule_bodies(css, ".issue-meta__repo")
    assert repo_bodies and any("overflow-wrap: anywhere" in b for b in repo_bodies)


def test_page_level_horizontal_overflow_backstop():
    css = (STATIC_DIR / "layout.css").read_text()
    bodies = _rule_bodies(css, "html, body")
    assert bodies, "no html, body rule"
    # ``clip`` specifically: ``hidden`` would make body a scroll container
    # and break the sticky header/action bars — the regression this pin
    # exists to prevent.
    assert any("overflow-x: clip" in b for b in bodies)
    assert all("overflow-x: hidden" not in b for b in bodies)
