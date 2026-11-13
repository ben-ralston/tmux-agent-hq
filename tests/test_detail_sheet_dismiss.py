"""Test for detail sheet tap-outside dismiss on mobile (#187).

The mobile detail-sheet uses ``position: fixed``, for which
``offsetParent`` is always ``null`` even when the element is visible. The
old tap-outside-dismiss guard used ``offsetParent !== null`` as its
visibility check, so on mobile it always concluded the sheet was hidden
and returned early — making the dismiss a silent no-op.

We can't execute the handler's JS in pytest, so this test pins the source
to the fixed pattern (``getComputedStyle`` visibility check, no lingering
``offsetParent`` guard). The behavioral proof lives in the 390px browser
pass described in the PR.
"""

from __future__ import annotations

import pathlib

JS_FILE = pathlib.Path(__file__).parent.parent / "agent_hq/web/static/agent_hq.js"


def test_detail_sheet_dismiss_uses_fixed_safe_visibility_check():
    """The dismiss guard must use a fixed-position-safe visibility check."""
    js_code = JS_FILE.read_text()

    assert "getComputedStyle(sheet).display" in js_code, (
        "The detail-sheet dismiss handler should gate on "
        "getComputedStyle(sheet).display, which works for position:fixed "
        "elements (offsetParent does not)."
    )
    assert "sheet.offsetParent" not in js_code, (
        "The broken offsetParent visibility guard should be gone — it "
        "always reads null for the position:fixed mobile sheet (#187)."
    )
