"""Source pins for the /issues SSE refetches in agent_hq.js (#269).

htmx keeps one sync queue per request *source*. The refetches fire one
``htmx.ajax`` per surface (container, Triage badge, Hygiene badge); with no
``source`` all of them shared ``document.body``'s queue, whose default
``queue last`` strategy evicted every call but the final one — the Triage
badge refetch vanished and its count went stale after a Confirm.

The swapped surface can't be its own source either (review round 1): an
outerHTML swap wipes the old element's queue and htmx drops a request whose
source has left the DOM, so a refetch queued behind an in-flight one — a
burst of events, the skeleton's ``load`` GET, a chip tap — was lost. Each
surface therefore gets a hidden, never-swapped source with
``hx-sync="this:replace"``: a newer refetch aborts the in-flight one and goes
out at once against the current target.

We can't execute the handlers in pytest, so this pins the source. The
behavioural proof is the real-browser pass described in the PR.
"""

from __future__ import annotations

import pathlib
import re

JS_FILE = pathlib.Path(__file__).parent.parent / "agent_hq/web/static/agent_hq.js"


def _function_body(js: str, name: str) -> str:
    # The body ends at the closing brace on the function line's own indent.
    match = re.search(rf"(?m)^([ \t]*)function {name}\([^)]*\) \{{(.*?)\n\1\}}", js, re.S)
    assert match, f"{name} not found"
    return match.group(2)


def _ajax_calls(body: str) -> list[str]:
    return re.findall(r"window\.htmx\.ajax\((.*?)\);", body, re.S)


def test_refetch_sources_are_persistent_and_replace_in_flight_requests():
    body = _function_body(JS_FILE.read_text(), "refetchSource")
    assert "document.body.appendChild" in body, "source must live outside every swapped surface"
    assert re.search(r'setAttribute\(\s*"hx-sync",\s*"this:replace"\s*\)', body), (
        "a queued request would swap into the target captured at call time "
        "(stale once the in-flight response swaps it); replace re-issues now"
    )
    assert "document.body.contains" in body, "a detached source must be recreated"


def test_every_issues_surface_refetch_uses_its_own_persistent_source():
    js = JS_FILE.read_text()
    calls = _ajax_calls(_function_body(js, "refetchIssuesSurfaces")) + _ajax_calls(
        _function_body(js, "refetchDispatchContainer")
    )
    assert len(calls) == 3, f"expected container + two badges, found {len(calls)}"
    names = []
    for call in calls:
        source = re.search(r'source:\s*refetchSource\("([a-z-]+)"\)', call)
        assert source, f"refetch without a persistent source (#269): {call.strip()[:80]}"
        names.append(source.group(1))
    assert len(set(names)) == 3, f"surfaces must not share a sync queue: {names}"


def test_issues_listeners_share_the_container_refetch():
    js = JS_FILE.read_text()
    assert "refetchDispatchContainer()" in _function_body(js, "refetchIssuesSurfaces")
    assert re.search(
        r'es\.addEventListener\(\s*"dispatch\.refreshed",\s*refetchDispatchContainer\s*\)', js
    ), "dispatch.refreshed must reuse refetchDispatchContainer, not a body-sourced copy"
