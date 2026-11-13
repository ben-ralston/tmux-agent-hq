"""Source pin for the ``agent.removed`` SSE handler in agent_hq.js (#338).

Removing a card in place leaves its section behind: the phase header and
its count (or a kanban column) are server-rendered around the card, so the
last card of a group going away left a bare header until the next 10 s
poll. The handler now asks for the whole view after a removal — the same
body event pins and mutes use — so the server re-renders the groups.

We can't execute the handler in pytest; the behavioural proof is the
browser pass described in the PR.
"""

from __future__ import annotations

import pathlib
import re

JS_FILE = pathlib.Path(__file__).parent.parent / "agent_hq/web/static/agent_hq.js"


def _removed_handler() -> str:
    js = JS_FILE.read_text()
    match = re.search(
        r'es\.addEventListener\("agent\.removed", function \(e\) \{(.*?)\n    \}\);', js, re.S
    )
    assert match, "agent.removed handler not found"
    return match.group(1)


def test_removed_card_refetches_the_view():
    body = _removed_handler()
    removal = body.find("target.remove()")
    assert removal != -1
    refetch = body.find('new CustomEvent("agenthq:pins-changed")', removal)
    assert refetch != -1, "a removal must re-render the groups, or a bare header remains"
