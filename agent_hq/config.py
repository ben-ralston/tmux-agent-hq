"""Load and validate ~/.config/agent-hq/projects.yaml."""

import re
import shlex
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import yaml

CONFIG_PATH = Path.home() / ".config" / "agent-hq" / "projects.yaml"

# Matches `claude --permission-mode` choices; kept in sync with the Claude CLI.
VALID_PERMISSION_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}
)

# Harnesses dispatch knows how to launch, and the reasoning-effort levels
# accepted across them (not every harness supports every level; the harness
# adapter is responsible for translating/rejecting at dispatch time).
VALID_HARNESSES = frozenset({"claude", "codex"})
# Semantic (weakest -> strongest) order, for UI selects that must not desync
# from the allowlist (#205 Stage 1B Task 15 review). ``VALID_EFFORTS`` stays a
# frozenset for membership checks; error messages that sort it alphabetically
# are unaffected.
EFFORT_ORDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
VALID_EFFORTS = frozenset(EFFORT_ORDER)


class ConfigError(Exception):
    """Invalid or incomplete configuration."""


@dataclass
class RepoConfig:
    """A configured repository.

    `project` and `description` are Idea-Inbox-only fields. `project` names a
    `ProjectConfig.name` and is what makes this repo a candidate for the
    classifier; without it, the repo is ignored by Idea Inbox routing.
    `description` is an optional one-liner the classifier sees as context.
    """

    owner_name: str  # "owner/name"
    local_path: Path
    project: str | None = None
    description: str | None = None
    # Branch new autopilot worktrees are cut from (#312). None = ask GitHub
    # for the repo's default branch.
    base_branch: str | None = None


@dataclass
class ProjectConfig:
    """A GitHub Project to fetch issues from."""

    name: str
    number: int
    owner: str  # "@me" or org name


@dataclass(frozen=True)
class AutomationConfig:
    """A scheduled launchd automation to surface on the web UI (#54).

    Health is read from what already exists: the launchd job (last exit
    code), its plist (schedule), and the wrapper's text log (last run /
    drill-down). ``label`` and ``log_file`` default from ``name`` to match
    the ``com.claude.<name>`` / ``~/.claude/logs/<name>.log`` conventions.

    ``failure_pattern`` / ``success_pattern`` are optional regexes judged
    against the wrapper's LAST log line: a failure match — or a success
    miss — marks the run ERROR even when launchd says exit 0 (wrappers
    that log a failure verdict but exit clean) or has no record at all
    (a reboot wipes launchd's last exit code).

    ``stale_after`` (#298) marks a long-running KeepAlive daemon: health
    then comes from launchd's running state plus log freshness — the
    daemon is expected to write a line (e.g. a heartbeat) more often than
    this — instead of the calendar schedule.
    """

    name: str
    label: str
    log_file: Path
    failure_pattern: str | None = None
    success_pattern: str | None = None
    stale_after: timedelta | None = None


@dataclass(frozen=True)
class HarnessDefaults:
    """Per-harness dispatch defaults (#205 Stage 1B).

    ``model`` is a free pass-through string — the harness adapter owns
    interpreting it, so config does not validate its contents beyond type.
    ``effort`` is validated against ``VALID_EFFORTS`` since it is a shared
    vocabulary across harnesses.
    """

    model: str | None = None
    effort: str | None = None


@dataclass
class DispatchConfig:
    """Settings that shape how dispatched Claude sessions are launched."""

    # No reader since #345; still validated so existing configs keep loading.
    permission_mode: str = "acceptEdits"
    default_harness: str = "claude"
    harnesses: dict[str, HarnessDefaults] = field(default_factory=dict)
    # Where the "scratch" tmux session (the + New session picker, MCP
    # open_chat_session) is created.
    scratch_cwd: Path = field(default_factory=Path.home)


@dataclass(frozen=True)
class IdeaInboxConfig:
    """Settings for the Idea Inbox classifier.

    All fields have defaults so the feature is usable out of the box once
    `ANTHROPIC_API_KEY` is set and at least one repo has `project:` configured.
    """

    model: str = "claude-sonnet-4-6"
    confidence_threshold: float = 0.75
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    # Optional argv to fetch the API key on demand when the env var is unset
    # (e.g. under the launchd service). Lets the key come from a secrets
    # manager — e.g. ["dcli", "n", "anthropic-agent-hq-key"] — instead of disk.
    anthropic_api_key_command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class WebConfig:
    """Settings for the web dashboard.

    ``ios_jump_shortcut`` names the iOS Shortcut the "Jump to Terminal"
    button opens on iPhone/iPad (via ``shortcuts://run-shortcut``). Set it
    to ``null`` (or empty) to disable the iOS branch entirely. #137
    """

    ios_jump_shortcut: str | None = "Agent HQ Terminal"


@dataclass(frozen=True)
class TriageConfig:
    """The `triage:` section — local triage store + data-repo sync (#246) and
    the AI triage worker knobs (#248)."""

    data_dir: Path
    sync_debounce_s: int = 60
    sync_enabled: bool = True
    ai_enabled: bool = True
    ai_model: str = "opus"  # passed to `claude -p --model`; alias or full id. opus: 2026-09 eval
    ai_batch_cap: int = 15  # max issues per repo per worker run
    ai_timeout_s: int = 300  # subprocess timeout per claude -p call
    hygiene_enabled: bool = True  # #249 use-triggered sweep + Run now
    hygiene_model: str | None = None  # None → ai_model
    hygiene_timeout_s: int = 600  # one cross-repo call, larger than a triage batch


@dataclass(frozen=True)
class AutopilotConfig:
    """The `autopilot:` section (#302) — nightly dispatch of opted-in issues.

    ``enabled`` is the hard kill switch (the web Pause toggle is the soft
    one). ``repos`` is an allowlist; empty means nothing is eligible.
    """

    enabled: bool = False
    per_night: int = 2
    max_open: int = 3
    turn_bound: int = 40
    model: str = "opus"
    repos: tuple[str, ...] = ()


DEFAULT_REMOTE_PORT = 9894


# What ``claude --effort`` accepts (pinned to ClaudeAdapter.chat_efforts by a
# test; adapters can't be imported here).
REMOTE_SESSION_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
# The launch line is typed into the new pane's tty, which keeps about 1024
# bytes of a line while the shell is still starting; the rest, closing quote
# included, would be dropped (review round 1). Bytes of the shell-quoted
# prompt (an apostrophe is five once quoted, round 2); the flags before it add
# at most ~130, so the whole line stays under the limit.
MAX_REMOTE_SESSION_PROMPT_BYTES = 800
REMOTE_SESSION_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}", re.ASCII)


@dataclass(frozen=True)
class RemoteSession:
    """One ``remote.sessions`` entry (#429): a Claude Code session Duet may
    start by name. The prompt, folder, model and effort come only from here,
    never from the remote caller."""

    name: str
    prompt: str
    cwd: Path | None = None  # None: dispatch.scratch_cwd
    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class RemoteSettings:
    """The `remote:` section (#396) — remote MCP for claude.ai/Duet (writes opt-in, #397).

    Absent means the feature is off. The bind host is fixed at 127.0.0.1 and
    the GitHub client secret lives in the Keychain, never here.
    """

    public_url: str
    allowed_github_login: str
    allowed_github_id: int
    github_client_id: str
    port: int = DEFAULT_REMOTE_PORT
    # #397: expose the write tools (dispatch, triage, mute). Off by default,
    # so the connector stays read-only until the owner opts in.
    writes: bool = False
    # #429: the sessions the remote ``start_session`` tool may open, by name.
    # Empty means it refuses everything.
    sessions: dict[str, RemoteSession] = field(default_factory=dict)


@dataclass
class GithubConfig:
    """The `github:` section — GitHub API budget policy (#255).

    ``graphql_floor`` is the GraphQL headroom below which agent-hq stops
    making *background* refreshes. Explicit refreshes and writes are never
    gated: the user is present and deliberately spending budget.
    """

    graphql_floor: int = 500


@dataclass
class Config:
    """Agent HQ dispatch configuration."""

    repos: dict[str, RepoConfig]  # keyed by "owner/name"
    projects: list[ProjectConfig]
    worktree_root: Path
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    idea_inbox: IdeaInboxConfig = field(default_factory=IdeaInboxConfig)
    web: WebConfig = field(default_factory=WebConfig)
    automations: list[AutomationConfig] = field(default_factory=list)
    triage: TriageConfig | None = None
    github: GithubConfig = field(default_factory=GithubConfig)
    autopilot: AutopilotConfig | None = None
    remote: RemoteSettings | None = None
    # Top-level blocks of removed features this load ignored, e.g. "companion"
    # (#399). The loader never logs (short-lived processes load the config
    # too); the web app warns once at startup.
    retired_keys: tuple[str, ...] = ()


# Top-level sections of removed features: ignored, listed in Config.retired_keys,
# each with the hint the web app's one-time warning carries.
RETIRED_KEYS = {
    "companion": "the companion was removed (#399); delete this block",
}


def load_config(path: Path | None = None) -> Config | None:
    """Load config from YAML. Returns None if file doesn't exist.

    Raises ConfigError on invalid YAML. All top-level sections are optional.
    A retired feature's block (``RETIRED_KEYS``) is ignored and listed in
    ``Config.retired_keys``; any other unknown key is ignored silently.
    """
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        return None

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML: {e}") from e

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("Config must be a YAML mapping")

    # Parse repos (optional). Malformed entries must surface as ConfigError —
    # callers (TUI mount, web create_app) catch only ConfigError to degrade
    # gracefully; a raw KeyError/TypeError would crash them instead.
    repos: dict[str, RepoConfig] = {}
    try:
        repo_items = (data.get("repos") or {}).items()
    except AttributeError as e:
        raise ConfigError("repos must be a mapping of owner/name -> settings") from e
    for owner_name, repo_data in repo_items:
        try:
            local_path = Path(repo_data["local_path"]).expanduser()
            base_branch = repo_data.get("base_branch")
            if isinstance(base_branch, str):
                base_branch = base_branch.strip()
            repos[owner_name] = RepoConfig(
                owner_name=owner_name,
                local_path=local_path,
                project=repo_data.get("project"),
                description=repo_data.get("description"),
                base_branch=base_branch,
            )
        except (KeyError, TypeError, AttributeError) as e:
            raise ConfigError(
                f"repo {owner_name!r}: entry must be a mapping with a local_path ({e!r})"
            ) from e
        blank = not isinstance(base_branch, str) or not base_branch
        if base_branch is not None and blank:
            raise ConfigError(f"repo {owner_name!r}: base_branch must be a non-empty string")

    # Parse projects (optional)
    projects: list[ProjectConfig] = []
    projects_data = data.get("projects") or []
    if not isinstance(projects_data, list):
        raise ConfigError("projects must be a list of {name, number, owner} mappings")
    for proj in projects_data:
        try:
            projects.append(
                ProjectConfig(
                    name=proj["name"],
                    number=proj["number"],
                    owner=proj["owner"],
                )
            )
        except (KeyError, TypeError, AttributeError) as e:
            raise ConfigError(
                f"project entry {proj!r}: must be a mapping with name/number/owner ({e!r})"
            ) from e

    # Cross-validate: every repo.project must name a known ProjectConfig.
    project_names = {p.name for p in projects}
    for repo in repos.values():
        if repo.project is not None and repo.project not in project_names:
            raise ConfigError(
                f"repo {repo.owner_name!r} references unknown project {repo.project!r}; "
                f"known projects: {sorted(project_names) or '[]'}"
            )

    # Parse worktree root
    worktree_root = Path(data.get("worktree_root", "~/.claude-worktrees")).expanduser()

    # Parse dispatch settings
    dispatch_data = data.get("dispatch") or {}
    permission_mode = dispatch_data.get("permission_mode", "acceptEdits")
    if permission_mode not in VALID_PERMISSION_MODES:
        valid = ", ".join(sorted(VALID_PERMISSION_MODES))
        raise ConfigError(
            f"Invalid dispatch.permission_mode: {permission_mode!r}. Must be one of: {valid}"
        )

    default_harness = dispatch_data.get("default_harness", "claude")
    if default_harness not in VALID_HARNESSES:
        raise ConfigError(
            f"Invalid dispatch.default_harness: {default_harness!r}. "
            f"Must be one of: {', '.join(sorted(VALID_HARNESSES))}"
        )
    harnesses: dict[str, HarnessDefaults] = {}
    harness_data = dispatch_data.get("harnesses") or {}
    if not isinstance(harness_data, dict):
        raise ConfigError("dispatch.harnesses must be a mapping of harness -> settings")
    for name, hd in harness_data.items():
        if name not in VALID_HARNESSES:
            raise ConfigError(f"Invalid dispatch.harnesses key (unknown harness): {name!r}")
        hd = hd or {}
        if not isinstance(hd, dict):
            raise ConfigError(f"dispatch.harnesses.{name} must be a mapping")
        model = hd.get("model")
        if model is not None and not isinstance(model, str):
            raise ConfigError(f"dispatch.harnesses.{name}.model must be a string; got {model!r}")
        if model is not None and (not model or any(c.isspace() for c in model)):
            # Mirrors resolve_dispatch_selection's rule (dispatch.py): an
            # empty or whitespace-containing model rides /worktree-create's
            # argv as a single token, so reject it at load time rather than
            # let it silently persist and blow up (or splice phantom
            # arguments) at dispatch time.
            raise ConfigError(
                f"dispatch.harnesses.{name}.model must be a non-empty, "
                f"whitespace-free string; got {model!r}"
            )
        effort = hd.get("effort")
        if effort is not None and effort not in VALID_EFFORTS:
            raise ConfigError(
                f"Invalid dispatch.harnesses.{name}.effort: {effort!r}. "
                f"Must be one of: {', '.join(sorted(VALID_EFFORTS))}"
            )
        harnesses[name] = HarnessDefaults(model=model, effort=effort)

    scratch_raw = dispatch_data.get("scratch_cwd")
    if scratch_raw is not None and (not isinstance(scratch_raw, str) or not scratch_raw.strip()):
        raise ConfigError(f"dispatch.scratch_cwd must be a non-empty path; got {scratch_raw!r}")
    if scratch_raw is not None:
        scratch_cwd = Path(scratch_raw).expanduser()
    else:
        scratch_cwd = Path.home()

    dispatch = DispatchConfig(
        permission_mode=permission_mode,
        default_harness=default_harness,
        harnesses=harnesses,
        scratch_cwd=scratch_cwd,
    )

    # Parse idea_inbox (optional; defaults applied when block missing)
    idea_inbox = _parse_idea_inbox(data.get("idea_inbox"))

    # Parse web (optional; defaults applied when block missing)
    web = _parse_web(data.get("web"))

    # Parse automations (optional)
    automations = _parse_automations(data.get("automations"))

    return Config(
        repos=repos,
        projects=projects,
        worktree_root=worktree_root,
        dispatch=dispatch,
        idea_inbox=idea_inbox,
        web=web,
        automations=automations,
        triage=_parse_triage(data.get("triage")),
        github=_parse_github(data.get("github")),
        autopilot=_parse_autopilot(data.get("autopilot"), set(repos)),
        remote=_parse_remote(data.get("remote")),
        retired_keys=tuple(k for k in RETIRED_KEYS if k in data),
    )


def _parse_github(raw: object | None) -> GithubConfig:
    if raw is None:
        return GithubConfig()
    if not isinstance(raw, dict):
        raise ConfigError("github: must be a mapping")
    floor = raw.get("graphql_floor", GithubConfig.graphql_floor)
    if isinstance(floor, bool) or not isinstance(floor, int):
        raise ConfigError(f"github.graphql_floor must be an integer; got {floor!r}")
    if floor < 0:
        raise ConfigError(f"github.graphql_floor must not be negative; got {floor!r}")
    return GithubConfig(graphql_floor=floor)


def _positive_int(raw: dict, key: str, default: int, section: str = "autopilot") -> int:
    val = raw.get(key, default)
    if isinstance(val, bool) or not isinstance(val, int) or val < 1:
        raise ConfigError(f"{section}.{key} must be a positive integer; got {val!r}")
    return val


_REMOTE_KEYS = frozenset(
    {
        "public_url",
        "allowed_github_login",
        "allowed_github_id",
        "github_client_id",
        "port",
        "writes",
        "sessions",
    }
)
_REMOTE_SESSION_KEYS = frozenset({"prompt", "cwd", "model", "effort"})
_REMOTE_SESSION_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}", re.ASCII)


def _parse_remote_sessions(raw: object | None) -> dict[str, RemoteSession]:
    """The ``remote.sessions`` allowlist (#429). Every value is checked here
    because the launch line is typed into a shell on the Mac."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("remote.sessions must be a mapping of name to session")
    sessions: dict[str, RemoteSession] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not REMOTE_SESSION_NAME_RE.fullmatch(name):
            raise ConfigError(
                f"remote.sessions name {name!r} must be lowercase letters, digits, '-' or '_' "
                "(up to 40, starting with a letter or digit)"
            )
        where = f"remote.sessions.{name}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a mapping with a prompt")
        unknown = sorted((str(k) for k in set(entry) - _REMOTE_SESSION_KEYS))
        if unknown:
            raise ConfigError(
                f"unknown key {where}.{unknown[0]} "
                f"(known: {', '.join(sorted(_REMOTE_SESSION_KEYS))})"
            )
        prompt = entry.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ConfigError(f"{where}.prompt is required and must be a non-empty string")
        prompt = prompt.strip()
        if len(shlex.quote(prompt).encode("utf-8")) > MAX_REMOTE_SESSION_PROMPT_BYTES:
            raise ConfigError(
                f"{where}.prompt must be at most {MAX_REMOTE_SESSION_PROMPT_BYTES} bytes once "
                "shell-quoted (put longer instructions in a skill and name it here)"
            )
        if any((ord(c) < 32 or ord(c) == 127) and c != "\n" for c in prompt):
            raise ConfigError(
                f"{where}.prompt: control characters (other than newlines) are not allowed"
            )
        cwd_raw = entry.get("cwd")
        if cwd_raw is not None and (not isinstance(cwd_raw, str) or not cwd_raw.strip()):
            raise ConfigError(f"{where}.cwd must be a non-empty path; got {cwd_raw!r}")
        cwd = Path(cwd_raw.strip()).expanduser() if cwd_raw is not None else None
        if cwd is not None and not cwd.is_absolute():
            # A relative folder would resolve against the server's own cwd.
            raise ConfigError(f"{where}.cwd must be an absolute path (or ~/...); got {cwd_raw!r}")
        model = entry.get("model")
        if model is not None and (
            not isinstance(model, str) or not _REMOTE_SESSION_MODEL_RE.fullmatch(model)
        ):
            raise ConfigError(f"{where}.model must be a plain model id; got {model!r}")
        effort = entry.get("effort")
        if effort is not None and (
            not isinstance(effort, str) or effort not in REMOTE_SESSION_EFFORTS
        ):
            raise ConfigError(
                f"{where}.effort must be one of {', '.join(sorted(REMOTE_SESSION_EFFORTS))}; "
                f"got {effort!r}"
            )
        sessions[name] = RemoteSession(
            name=name,
            prompt=prompt,
            cwd=cwd,
            model=model,
            effort=effort,
        )
    return sessions


def _parse_remote(raw: object | None) -> RemoteSettings | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("remote: must be a mapping")
    if "host" in raw:
        raise ConfigError("remote.host is not allowed: host is fixed at 127.0.0.1")
    if "github_client_secret" in raw:
        raise ConfigError(
            "remote.github_client_secret is not allowed: keep the GitHub client secret "
            "in the Keychain (service agent-hq-mcp-remote, account github-client-secret)"
        )
    unknown = sorted(set(raw) - _REMOTE_KEYS)
    if unknown:
        raise ConfigError(
            f"unknown key remote.{unknown[0]} (known: {', '.join(sorted(_REMOTE_KEYS))})"
        )
    for key in ("public_url", "allowed_github_login", "github_client_id"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ConfigError(f"remote.{key} is required and must be a non-empty string")
    url = raw["public_url"].strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ConfigError(
            "remote.public_url must be an https:// URL (Claude only connects over HTTPS)"
        )
    if not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        raise ConfigError(
            "remote.public_url must be scheme + host only, e.g. https://agents.example.com"
        )
    gh_id = raw.get("allowed_github_id")
    if isinstance(gh_id, bool) or not isinstance(gh_id, int) or gh_id < 1:
        raise ConfigError(
            f"remote.allowed_github_id must be the numeric GitHub user id; got {gh_id!r}"
        )
    port = raw.get("port", DEFAULT_REMOTE_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ConfigError(f"remote.port must be a number between 1 and 65535; got {port!r}")
    writes = raw.get("writes", False)
    if not isinstance(writes, bool):
        raise ConfigError(f"remote.writes must be true or false; got {writes!r}")
    return RemoteSettings(
        public_url=url,
        allowed_github_login=raw["allowed_github_login"].strip(),
        allowed_github_id=gh_id,
        github_client_id=raw["github_client_id"].strip(),
        port=port,
        writes=writes,
        sessions=_parse_remote_sessions(raw.get("sessions")),
    )


def _parse_autopilot(raw: object | None, known_repos: set[str]) -> AutopilotConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("autopilot: must be a mapping")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"autopilot.enabled must be a boolean; got {enabled!r}")
    model = raw.get("model", "opus")
    if not isinstance(model, str) or not model or any(c.isspace() for c in model):
        raise ConfigError(
            f"autopilot.model must be a non-empty, whitespace-free string; got {model!r}"
        )
    repos = raw.get("repos", [])
    if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
        raise ConfigError(f"autopilot.repos must be a list of owner/name strings; got {repos!r}")
    for r in repos:
        if r not in known_repos:
            raise ConfigError(f"autopilot.repos: {r!r} is not a configured repo")
    return AutopilotConfig(
        enabled=enabled,
        per_night=_positive_int(raw, "per_night", 2),
        max_open=_positive_int(raw, "max_open", 3),
        turn_bound=_positive_int(raw, "turn_bound", 40),
        model=model,
        repos=tuple(repos),
    )


def _parse_triage(raw: object | None) -> TriageConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("triage: must be a mapping")
    data_dir = raw.get("data_dir")
    if not data_dir or not isinstance(data_dir, str):
        raise ConfigError("triage.data_dir is required")
    debounce = raw.get("sync_debounce_s", 60)
    if isinstance(debounce, bool) or not isinstance(debounce, int):
        raise ConfigError(f"triage.sync_debounce_s must be an integer; got {debounce!r}")
    enabled = raw.get("sync_enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"triage.sync_enabled must be a boolean; got {enabled!r}")
    ai_enabled = raw.get("ai_enabled", True)
    if not isinstance(ai_enabled, bool):
        raise ConfigError(f"triage.ai_enabled must be a boolean; got {ai_enabled!r}")
    ai_model = raw.get("ai_model", "opus")
    if not isinstance(ai_model, str) or not ai_model or any(c.isspace() for c in ai_model):
        raise ConfigError(
            f"triage.ai_model must be a non-empty, whitespace-free string; got {ai_model!r}"
        )
    ai_batch_cap = raw.get("ai_batch_cap", 15)
    if isinstance(ai_batch_cap, bool) or not isinstance(ai_batch_cap, int) or ai_batch_cap < 1:
        raise ConfigError(f"triage.ai_batch_cap must be a positive integer; got {ai_batch_cap!r}")
    ai_timeout_s = raw.get("ai_timeout_s", 300)
    if isinstance(ai_timeout_s, bool) or not isinstance(ai_timeout_s, int) or ai_timeout_s < 1:
        raise ConfigError(f"triage.ai_timeout_s must be a positive integer; got {ai_timeout_s!r}")
    hygiene_enabled = raw.get("hygiene_enabled", True)
    if not isinstance(hygiene_enabled, bool):
        raise ConfigError(f"triage.hygiene_enabled must be a boolean; got {hygiene_enabled!r}")
    hygiene_model = raw.get("hygiene_model")
    if hygiene_model is not None and (
        not isinstance(hygiene_model, str)
        or not hygiene_model
        or any(c.isspace() for c in hygiene_model)
    ):
        raise ConfigError(
            "triage.hygiene_model must be a non-empty, whitespace-free string; "
            f"got {hygiene_model!r}"
        )
    hygiene_timeout_s = raw.get("hygiene_timeout_s", 600)
    if (
        isinstance(hygiene_timeout_s, bool)
        or not isinstance(hygiene_timeout_s, int)
        or hygiene_timeout_s < 1
    ):
        raise ConfigError(
            f"triage.hygiene_timeout_s must be a positive integer; got {hygiene_timeout_s!r}"
        )
    return TriageConfig(
        data_dir=Path(data_dir).expanduser(),
        sync_debounce_s=debounce,
        sync_enabled=enabled,
        ai_enabled=ai_enabled,
        ai_model=ai_model,
        ai_batch_cap=ai_batch_cap,
        ai_timeout_s=ai_timeout_s,
        hygiene_enabled=hygiene_enabled,
        hygiene_model=hygiene_model,
        hygiene_timeout_s=hygiene_timeout_s,
    )


def _parse_web(raw: object | None) -> WebConfig:
    if raw is None:
        return WebConfig()
    if not isinstance(raw, dict):
        raise ConfigError("web section must be a mapping")
    name_raw = raw.get("ios_jump_shortcut", WebConfig.ios_jump_shortcut)
    if name_raw is None:
        name = None
    elif isinstance(name_raw, str):
        # Empty / whitespace-only means "disable", same as null.
        name = name_raw.strip() or None
    else:
        raise ConfigError(f"web.ios_jump_shortcut must be a string or null; got {name_raw!r}")
    return WebConfig(ios_jump_shortcut=name)


_DURATION_RE = re.compile(r"^(\d+)(s|m|h)$")
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours"}


def _parse_duration(name: str, key: str, value: object) -> timedelta:
    """Parse a positive ``<int><s|m|h>`` duration such as ``90s`` or ``3m``."""
    match = _DURATION_RE.match(value) if isinstance(value, str) else None
    if not match or int(match.group(1)) == 0:
        raise ConfigError(
            f"automations[{name}].{key} must be a positive duration like '90s', '3m' or '2h'"
        )
    return timedelta(**{_DURATION_UNITS[match.group(2)]: int(match.group(1))})


def _parse_automations(raw: object | None) -> list[AutomationConfig]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("automations section must be a list")
    automations: list[AutomationConfig] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigError("automations entries must be mappings with a 'name' key")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError("automations entries require a non-empty string 'name'")
        label = entry.get("label", f"com.claude.{name}")
        if not isinstance(label, str) or not label:
            raise ConfigError(f"automations[{name}].label must be a non-empty string")
        log_file_raw = entry.get("log_file", f"~/.claude/logs/{name}.log")
        if not isinstance(log_file_raw, str) or not log_file_raw:
            raise ConfigError(f"automations[{name}].log_file must be a non-empty string")
        patterns = {}
        for key in ("failure_pattern", "success_pattern"):
            pattern = entry.get(key)
            if pattern is not None:
                if not isinstance(pattern, str) or not pattern:
                    raise ConfigError(f"automations[{name}].{key} must be a non-empty string")
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ConfigError(f"automations[{name}].{key} is not a valid regex: {exc}")
            patterns[key] = pattern
        stale_after_raw = entry.get("stale_after")
        stale_after = (
            None
            if stale_after_raw is None
            else _parse_duration(name, "stale_after", stale_after_raw)
        )
        automations.append(
            AutomationConfig(
                name=name,
                label=label,
                log_file=Path(log_file_raw).expanduser(),
                stale_after=stale_after,
                **patterns,
            )
        )
    return automations


def _parse_idea_inbox(raw: object | None) -> IdeaInboxConfig:
    if raw is None:
        return IdeaInboxConfig()
    if not isinstance(raw, dict):
        raise ConfigError("idea_inbox section must be a mapping")
    threshold = raw.get("confidence_threshold", IdeaInboxConfig.confidence_threshold)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError) as e:
        raise ConfigError(
            f"idea_inbox.confidence_threshold must be a number: {raw.get('confidence_threshold')!r}"
        ) from e
    if not 0.0 <= threshold <= 1.0:
        raise ConfigError(f"idea_inbox.confidence_threshold must be in [0, 1]; got {threshold}")
    key_command_raw = raw.get("anthropic_api_key_command")
    if key_command_raw is None:
        key_command = None
    elif (
        isinstance(key_command_raw, list)
        and key_command_raw
        and all(isinstance(x, str) and x for x in key_command_raw)
    ):
        key_command = tuple(key_command_raw)
    else:
        raise ConfigError(
            "idea_inbox.anthropic_api_key_command must be a non-empty list of "
            "non-empty strings (argv), e.g. [dcli, n, anthropic-agent-hq-key]"
        )

    return IdeaInboxConfig(
        model=raw.get("model", IdeaInboxConfig.model),
        confidence_threshold=threshold,
        anthropic_api_key_env=raw.get(
            "anthropic_api_key_env", IdeaInboxConfig.anthropic_api_key_env
        ),
        anthropic_api_key_command=key_command,
    )
