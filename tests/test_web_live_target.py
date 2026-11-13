"""Source pins for the ``live-target`` htmx extension (#268).

Two ``outerHTML`` swaps of ``#dispatch-container`` can be in flight at once:
the skeleton's ``hx-trigger="load"`` fetch racing an SSE-driven ``htmx.ajax``
refetch (``dispatch.refreshed`` / ``issue.invalidated``), two JS refetches
(htmx queues the second behind the first but keeps the target element it
captured at issue time), or a chip tap while the ↻ refresh is still pending.
The second response then targets an element the first swap already replaced.
htmx 2.0.3's ``swap()`` runs
``handlePreservedElements`` — which moves the live ``hx-preserve`` search input
out of the document — BEFORE ``swapOuterHTML`` discovers the target has no
parent and silently returns, and ``restorePreservedElements`` then finds only
the pantry copy and removes it. The Browse search field is gone until the next
successful container swap re-creates it (the "intermittently missing search
field").

``htmx:beforeSwap`` is dispatched on the (detached) target, so a document-
level listener never sees it; an extension named in ``hx-ext`` on the
container itself does. The extension retargets the swap to the live element
carrying the same id — but only when the late response is not OLDER than the
request that produced the live element and is not a read landing over a
write (review rounds 1-3: a slow ↻ landing after a chip tap, or a refetch
rendered from the pre-tap cookie, must not repaint pre-tap state) — and
drops the swap otherwise. The guard governs the DOM only: a dropped
response's Set-Cookie still lands (round 4), a pre-existing server-side race.

We can't execute htmx in pytest, so this pins the source; the behavioural
proof is the browser repro in the PR (two concurrent container refetches,
field present afterwards).
"""

from __future__ import annotations

import pathlib
import re

JS_FILE = pathlib.Path(__file__).parent.parent / "agent_hq/web/static/agent_hq.js"


def _js() -> str:
    return JS_FILE.read_text()


def _extension_body() -> str:
    match = re.search(
        r'defineExtension\("live-target",\s*\{(.*?)\n {2,4}\}\);',
        _js(),
        re.S,
    )
    assert match, "live-target htmx extension not defined in agent_hq.js"
    return match.group(1)


def test_extension_hooks_before_swap_only():
    body = _extension_body()
    assert '"htmx:beforeSwap"' in body
    # Every other event must pass straight through.
    assert re.search(r'if \(name !== "htmx:beforeSwap"\) return true;', body)


def test_extension_retargets_detached_container_to_live_element_by_id():
    body = _extension_body()
    # Attached targets are left alone — the ordinary swap path is untouched.
    assert "document.body.contains(target)" in body
    # A detached target is re-resolved by id to the element that replaced it…
    assert "document.getElementById(target.id)" in body
    assert "detail.target = live" in body
    # …and the swap is dropped (not mangled) when nothing live carries the id.
    assert "detail.shouldSwap = false" in body


def test_extension_never_lets_an_older_response_overwrite_a_newer_swap():
    """Review finding: retargeting must be ordered. Each request against a
    live-target container is stamped with a monotonically increasing sequence
    at issue time; the element a swap produces carries that stamp; a late
    response whose stamp is older than the live element's is dropped."""
    js = _js()
    body = _extension_body()
    # Issue-time stamp on the XHR, for every request targeting an opted-in container.
    stamp = re.search(
        r'addEventListener\("htmx:beforeRequest", function \(evt\) \{(.*?)\n  \}\);',
        js[js.index('defineExtension("live-target"') :],
        re.S,
    )
    assert stamp, "no htmx:beforeRequest stamping listener after the extension"
    assert '[hx-ext~="live-target"]' in stamp.group(1)
    assert "ahqSwapSeq" in stamp.group(1)
    # The freshly swapped-in element records the stamp of the request that produced it.
    carry = re.search(
        r'addEventListener\("htmx:afterSwap", function \(evt\) \{(.*?)\n  \}\);',
        js[js.index('defineExtension("live-target"') :],
        re.S,
    )
    assert carry, "no htmx:afterSwap stamp-carrying listener after the extension"
    assert "data-swap-seq" in carry.group(1)
    # And the extension compares the two before retargeting.
    assert "ahqSwapSeq" in body and "data-swap-seq" in body
    assert re.search(r"liveSeq > mine", body), "older-than-live responses must be dropped"


def test_extension_only_retargets_a_read_over_a_read():
    """Review round 2: issue-time order is not state order. A container
    refetch (GET) issued while a chip's POST is still in flight was rendered
    from the pre-tap cookie; if the POST lands first, retargeting the younger
    GET would repaint that pre-tap state over the tap's result. So the verb
    rides the stamp too, and a late response is retargeted only when it AND
    the request that produced the live element were both reads. Every case
    involving a write drops, exactly as before this extension existed."""
    js = _js()
    body = _extension_body()
    after = js[js.index('defineExtension("live-target"') :]
    listener = r'addEventListener\("htmx:%s", function \(evt\) \{(.*?)\n  \}\);'
    stamp = re.search(listener % "beforeRequest", after, re.S)
    carry = re.search(listener % "afterSwap", after, re.S)
    assert stamp and "ahqSwapVerb" in stamp.group(1)
    assert carry and "data-swap-verb" in carry.group(1)
    assert 'ahqSwapVerb === "get"' in body
    assert 'getAttribute("data-swap-verb") === "get"' in body


def test_extension_lets_a_younger_write_land_over_an_older_read():
    """Review round 3: the verb gate only has to block a READ over a write.
    A write issued after the live read (↻ tapped while a slow skeleton load
    was in flight) carries the post-write truth, so it applies; dropping it
    made ↻ look like a no-op."""
    body = _extension_body()
    assert "(!read || liveRead)" in body
