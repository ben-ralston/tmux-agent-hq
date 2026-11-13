"""Source pins for the client-side dispatch-state machinery (#59).

/issues cards are found by ``data-issue-key``, dispatch events name them
``issue_keys``, and the Dispatch button posts to ``/issues/dispatch``. Miss
any one and the surface silently misbehaves — most visibly, the re-dispatch
purge fails to drop a still-live "Started ✓" entry and the afterSettle replay
re-stamps it over a card that is booting right now. (The Personal tab used
``data-item-key`` / ``item_keys`` / ``/personal/dispatch`` until #345.)

We can't execute the handlers in pytest, so this pins the source. The
behavioural proof lives in the 390px browser pass described in the PR.
"""

from __future__ import annotations

import pathlib
import re

JS_FILE = pathlib.Path(__file__).parent.parent / "agent_hq/web/static/agent_hq.js"


def _js() -> str:
    return JS_FILE.read_text()


def test_card_selector_matches_issue_cards():
    js = _js()
    match = re.search(r"function selectorForKey\(key\) \{(.*?)\n    \}", js, re.S)
    assert match, "selectorForKey not found"
    assert "data-issue-key" in match.group(1), "must match /issues cards"


def test_events_are_read_through_the_key_helper():
    js = _js()
    match = re.search(r"function dispatchKeys\(payload\) \{(.*?)\n    \}", js, re.S)
    assert match, "dispatchKeys helper not found"
    body = match.group(1)
    assert "issue_keys" in body

    # No listener may reach for a raw key field and thereby ignore the other.
    for listener in re.findall(r'es\.addEventListener\("dispatch\.[a-z]+".*?\n    \}\);', js, re.S):
        assert "payload.issue_keys" not in listener, listener[:120]


def test_redispatch_purge_covers_the_issues_endpoint():
    js = _js()
    match = re.search(
        r"var DISPATCH_POSTS = \{(.*?)\};.*?"
        r'addEventListener\("htmx:beforeRequest", function \(evt\) \{(.*?)\n    \}\);',
        js,
        re.S,
    )
    assert match, "DISPATCH_POSTS map + htmx:beforeRequest purge handler not found"
    endpoints, handler = match.group(1), match.group(2)
    assert "/issues/dispatch" in endpoints
    assert "data-issue-key" in endpoints, "the purge must find the cards to clear"
    assert "DISPATCH_POSTS[" in handler, "the handler must consult the endpoint map"
    # #247 review: the dispatch buttons carry ?mode=… — the lookup must
    # strip the query string or the purge never fires for issue dispatches
    # (regressing the #59 fix).
    assert 'split("?")[0]' in handler, "purge lookup must strip the hx-post query string"


def test_afterSettle_replay_scans_for_issue_cards():
    js = _js()
    assert 'querySelector("[data-issue-key]")' in js, (
        "the afterSettle pre-check must scan for issue cards, or the replay never runs"
    )


def test_container_refetches_carry_the_rendered_mode():
    """#247: the SSE-driven container refetches must re-request the mode
    the container was rendered under (data-mode) — otherwise an
    issue.invalidated event would flip a Browse tab back to Queue."""
    js = _js()
    # Both listeners route through the one container refetch (#269), which
    # is where the mode has to be carried.
    helper = re.search(r"function refetchDispatchContainer\(\) \{(.*?)\n    \}", js, re.S)
    assert helper, "refetchDispatchContainer not found"
    assert "/issues/container?mode=" in helper.group(1)
    assert "dataset.mode" in helper.group(1)
    assert re.search(
        r"function refetchIssuesSurfaces\(\) \{[^}]*refetchDispatchContainer\(\)", js
    ), "issue.invalidated's helper must reuse the mode-carrying container refetch"
    for topic, handler in (
        ("issue.invalidated", "refetchIssuesSurfaces"),
        ("dispatch.refreshed", "refetchDispatchContainer"),
    ):
        assert f'es.addEventListener("{topic}", {handler});' in js, topic
