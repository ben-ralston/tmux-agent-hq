"""PWA manifest expectations (issue #100).

The home-screen app installs from a single shared manifest, so it must open
to the dashboard and carry the whole-app identity, not the Idea Inbox.
"""

from __future__ import annotations

import json
from pathlib import Path

import agent_hq.web

STATIC_DIR = Path(agent_hq.web.__file__).resolve().parent / "static"
MANIFEST = STATIC_DIR / "manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def test_start_url_is_agents():
    assert _manifest()["start_url"] == "/agents"


def test_name_is_agent_hq():
    data = _manifest()
    assert data["name"] == "Agent HQ"
    assert data["short_name"] == "Agent HQ"
