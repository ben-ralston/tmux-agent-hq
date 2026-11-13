"""Assemble the remote ASGI app (#396).

A second FastMCP (``RemoteFastMCP``) carrying annotated, threaded copies of
the REMOTE_READ and REMOTE_ONLY_READ tools (plus the REMOTE_WRITE tools when
``remote.writes`` is on, #397), with the MCP SDK's OAuth authorization-server routes
backed by the vendored ``GitHubOAuthProvider``. Mirrors notes-graph's
``remote/app.py`` minus its notes-only folder scope, Reminders and write queue.
"""

from typing import Callable, Optional

import httpx
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .asgi import RegistrationGuard, RemoteFastMCP, threaded
from .auth import GitHubOAuthProvider, TokenStore
from .config import RemoteConfig
from .events import EventingRequestLog
from .request_log import RequestLogMiddleware
from .tools import (
    REMOTE_ONLY_READ,
    REMOTE_ONLY_WRITE,
    REMOTE_READ,
    REMOTE_WRITE,
    build_tool_source,
    check_categorized,
)

_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# Local writes never delete anything. Triage and mute are set-to-a-value (repeating
# one changes nothing); dispatch starts a session on the Mac each time.
_LOCAL_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_GITHUB_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
_WRITE_ANNOTATIONS = {
    "dispatch_issues": ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    ),
    # #429: opens a Claude Code session on the Mac each time; what that
    # session then does is open-ended. Never deletes anything.
    "start_session": ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    ),
    "set_triage": _LOCAL_WRITE,
    "mute_agent": _LOCAL_WRITE,
    "unmute_agent": _LOCAL_WRITE,
    # #414: public GitHub writes; each call writes again. Closing is the one
    # the MCP spec calls destructive (it ends the issue; reopening is manual).
    "create_issue": _GITHUB_WRITE,
    "comment_issue": _GITHUB_WRITE,
    "close_issue": ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
    ),
}


def _stdio_tool_names() -> list[str]:
    from agent_hq import mcp_server

    return [t.name for t in mcp_server.mcp._tool_manager.list_tools()]


def remote_tool_copies(source: FastMCP, *, writes: bool = False) -> list:
    """Annotated, threaded copies of ``source``'s remote tools.

    Refuses to build when a stdio tool is uncategorized, or the source offers
    anything outside REMOTE_READ and REMOTE_ONLY_READ (plus REMOTE_WRITE and
    REMOTE_ONLY_WRITE when ``writes``)."""
    check_categorized(_stdio_tool_names())
    allowed = REMOTE_READ | REMOTE_ONLY_READ
    if writes:
        allowed |= REMOTE_WRITE | REMOTE_ONLY_WRITE
    tools = source._tool_manager.list_tools()
    extra = sorted(t.name for t in tools if t.name not in allowed)
    if extra:
        raise ValueError(f"remote tool source offers non-remote tools: {', '.join(extra)}")
    return [
        threaded(
            t.model_copy(
                update={"annotations": _WRITE_ANNOTATIONS.get(t.name, _READ_ANNOTATIONS)}
            )
        )
        for t in tools
    ]


def build_remote_app(
    config: RemoteConfig,
    *,
    source: Optional[FastMCP] = None,
    http_client_factory: Optional[Callable[[], httpx.AsyncClient]] = None,
):
    """The remote ASGI app: OAuth routes, /mcp, request log, /register guard."""
    if source is None:
        source = build_tool_source(writes=config.writes)
    log = EventingRequestLog(config.log_path)
    provider = GitHubOAuthProvider(config, TokenStore(config.state_dir), log, http_client_factory)
    public_host = httpx.URL(config.public_url).host
    remote = RemoteFastMCP(
        source.name,
        instructions=source.instructions,
        request_log=log,
        tools=remote_tool_copies(source, writes=config.writes),
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=config.public_url,
            resource_server_url=config.mcp_url,
            # We are the only resource server this AS issues for.
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
        host=config.host,
        port=config.port,
        streamable_http_path="/mcp",
        # Stateless + JSON: every request stands alone (survives restarts, no SSE).
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public_host, f"{public_host}:*", "127.0.0.1:*", "localhost:*"],
            allowed_origins=[
                config.public_url,
                "https://claude.ai",
                "https://claude.com",
                "http://127.0.0.1:*",
                "http://localhost:*",
            ],
        ),
    )
    remote.custom_route("/oauth/not-allowed", methods=["GET"])(provider.not_allowed_page)
    remote.custom_route("/oauth/consent", methods=["GET"])(provider.consent_page)
    remote.custom_route("/oauth/consent", methods=["POST"])(provider.consent_submit)
    remote.custom_route("/oauth/github/callback", methods=["GET"])(provider.github_callback)
    return RequestLogMiddleware(RegistrationGuard(remote.streamable_http_app()), log)
