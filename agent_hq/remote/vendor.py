"""Provenance of the vendored notes-graph OAuth stack (#396).

``auth.py`` and ``request_log.py`` in this package are copies of notes-graph's
``src/apple_notes_mcp/remote/`` files at ``UPSTREAM_COMMIT`` (PR #168, which
went through four security review rounds plus a hardening round). They differ
from upstream ONLY by ``SUBSTITUTIONS``: branding, cookie names and CLI hints.

Never hand-edit them. ``tests/test_remote_vendor.py`` reverses the
substitutions and checks the upstream sha256, so any other change fails CI.
To pick up an upstream fix: run ``scripts/check-remote-vendor.sh``, copy the
new files, re-apply the substitutions, and bump the commit and hashes here.
notes-graph pins the same hashes on its side, so a fix there fails its own
suite until this copy is updated.

The upstream repository may not be public. If you cannot read it, the hash
test still runs (it needs no upstream checkout), but you cannot re-vendor:
describe the change you need in an issue or pull request and leave these two
files and the hashes alone, so the maintainer can land it upstream first.
"""

UPSTREAM_REPO = "ben-ralston/notes-graph"
UPSTREAM_COMMIT = "29ab497"

# vendored file name -> (upstream path, sha256 of the upstream file)
FILES: dict[str, tuple[str, str]] = {
    "auth.py": (
        "src/apple_notes_mcp/remote/auth.py",
        "ff4f7073e7666c0d47bdb326af3205317b4037634ca9d3d3d26c15dffd7eda43",
    ),
    "request_log.py": (
        "src/apple_notes_mcp/remote/request_log.py",
        "abe00bed5a65c48bc79d9886eadd234c8d45748d62d24c392847661d97371840",
    ),
}

# Ordered (upstream text, our text) pairs; each occurs exactly once.
SUBSTITUTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "auth.py": (
        (
            "``apple-notes-mcp-remote allow-client <id>``",
            "``agent-hq remote allow-client <id>``",
        ),
        ('CONSENT_COOKIE = "ng_consent"', 'CONSENT_COOKIE = "ahq_consent"'),
        ('FLOW_COOKIE = "ng_flow"', 'FLOW_COOKIE = "ahq_flow"'),
        (
            "<code>./run remote-allow-client {cid}</code>",
            "<code>agent-hq remote allow-client {cid}</code>",
        ),
        (
            "<code>./run remote-revoke-tokens</code>",
            "<code>agent-hq remote revoke-all</code>",
        ),
        (
            '"Allow access to your Apple Notes?"',
            '"Allow access to your agent-hq sessions and backlog?"',
        ),
        (
            "wants to read your notes through ",
            "wants to read your agent-hq sessions and backlog through ",
        ),
    ),
    "request_log.py": (
        (
            "uvicorn, the shared apple_notes library)",
            "uvicorn, the agent_hq library)",
        ),
        ("can carry note titles.", "can carry session names and issue titles."),
        (
            'record.name.startswith("apple_notes_mcp.remote")',
            'record.name.startswith("agent_hq.remote")',
        ),
        (
            '        "apple_notes",\n        "apple_notes_mcp",\n',
            '        "agent_hq",\n        "httpx",\n',
        ),
    ),
}


def apply_substitutions(name: str, upstream_text: str) -> str:
    """Upstream text -> our vendored text (used when re-vendoring)."""
    text = upstream_text
    for upstream, ours in SUBSTITUTIONS[name]:
        if text.count(upstream) != 1:
            raise ValueError(f"{name}: {upstream!r} must occur exactly once upstream")
        text = text.replace(upstream, ours)
    return text


def restore_upstream(name: str, text: str) -> str:
    """Our vendored text -> the upstream text it was made from."""
    for upstream, ours in reversed(SUBSTITUTIONS[name]):
        text = text.replace(ours, upstream)
    return text
