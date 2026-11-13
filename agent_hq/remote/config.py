"""Remote-mode configuration (#396).

``RemoteConfig`` carries the attribute names the vendored ``auth.py`` reads
(the same as notes-graph's, minus its notes-only folder/write/reminders
settings). The non-secret fields come from the ``remote:`` block of
projects.yaml (``agent_hq.config.RemoteSettings``); the GitHub OAuth client
secret only ever comes from the login Keychain.
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from agent_hq.config import DEFAULT_REMOTE_PORT, Config

STATE_DIR = Path.home() / ".config" / "agent-hq" / "remote"
LOG_DIR = Path.home() / "Library" / "Logs" / "agent-hq"
REQUEST_LOG = LOG_DIR / "remote-requests.log"
KEYCHAIN_SERVICE = "agent-hq-mcp-remote"
KEYCHAIN_ACCOUNT = "github-client-secret"

BIND_HOST = "127.0.0.1"
CLAUDE_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)


class RemoteConfigError(Exception):
    """The remote: block or the Keychain secret is missing or invalid."""


@dataclass(frozen=True)
class RemoteConfig:
    public_url: str
    allowed_github_login: str
    github_client_id: str
    github_client_secret: Optional[str] = field(default=None, repr=False)
    allowed_github_id: Optional[int] = None
    host: str = BIND_HOST
    port: int = DEFAULT_REMOTE_PORT
    redirect_uri_allowlist: tuple[str, ...] = CLAUDE_REDIRECT_URIS
    state_dir: Path = STATE_DIR
    log_path: Path = REQUEST_LOG
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 30 * 24 * 3600
    github_authorize_url: str = "https://github.com/login/oauth/authorize"
    github_token_url: str = "https://github.com/login/oauth/access_token"
    github_user_url: str = "https://api.github.com/user"
    writes: bool = False  # #397: register the REMOTE_WRITE tools
    sessions: tuple[str, ...] = ()  # #429: names start_session may open (display only)

    @property
    def mcp_url(self) -> str:
        return self.public_url + "/mcp"

    @property
    def github_callback_url(self) -> str:
        return self.public_url + "/oauth/github/callback"


def keychain_secret() -> Optional[str]:
    """The GitHub client secret from the login Keychain, or None."""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    secret = result.stdout.strip()
    return secret if result.returncode == 0 and secret else None


def load_remote_config(
    cfg: Config | None,
    *,
    keychain_lookup: Callable[[], Optional[str]] | None = None,
    require_secret: bool = True,
) -> RemoteConfig:
    """Build the server's RemoteConfig; raise RemoteConfigError on any problem.

    ``keychain_lookup`` defaults to ``keychain_secret`` resolved at call time,
    so the test suite's guard on it applies.
    """
    settings = cfg.remote if cfg is not None else None
    if settings is None:
        raise RemoteConfigError(
            "no remote: block in ~/.config/agent-hq/projects.yaml (see docs/remote-connector.md)"
        )
    secret = None
    if require_secret:
        secret = (keychain_lookup or keychain_secret)()
        if not secret:
            raise RemoteConfigError(
                f"no GitHub client secret: add it to the login Keychain (service "
                f"{KEYCHAIN_SERVICE}, account {KEYCHAIN_ACCOUNT})"
            )
    return RemoteConfig(
        public_url=settings.public_url,
        allowed_github_login=settings.allowed_github_login,
        allowed_github_id=settings.allowed_github_id,
        github_client_id=settings.github_client_id,
        github_client_secret=secret,
        port=settings.port,
        writes=settings.writes,
        sessions=tuple(sorted(settings.sessions)),
        state_dir=STATE_DIR,
        log_path=REQUEST_LOG,
    )
