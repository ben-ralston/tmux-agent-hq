"""Agent HQ MCP server -- expose agent management tools to Claude Code."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from agent_hq import eventlog

from . import tools
from .config import ConfigError, load_config
from .discovery import discover_agents
from .enrichment import enrich_all_agents
from .github import GitHubCache
from .issues_hub import merge_issue_lists
from .state import load_inactive_state
from .tools import ToolContext
from .triage_store import TriageStore

if TYPE_CHECKING:
    from .config import Config

mcp = FastMCP("agent-hq")

# ---------------------------------------------------------------------------
# Cached state (lazy-initialized)
# ---------------------------------------------------------------------------

_config: Config | None = None
_config_loaded: bool = False
_github_cache: GitHubCache | None = None
_triage_store: TriageStore | None = None
_triage_store_warned: bool = False


def _get_config() -> Config | None:
    """Return cached config, loading once on first call."""
    global _config, _config_loaded
    if not _config_loaded:
        _config = load_config()
        _config_loaded = True
    return _config


def _get_github_cache() -> GitHubCache:
    """Return cached GitHubCache, creating once on first call."""
    global _github_cache
    if _github_cache is None:
        config = _get_config()
        _github_cache = GitHubCache(graphql_floor=config.github.graphql_floor if config else None)
    return _github_cache


def _get_triage_store() -> TriageStore | None:
    """Return the configured store without creating a missing data directory."""
    global _triage_store, _triage_store_warned
    if _triage_store is None:
        config = _get_config()
        if config is not None and config.triage is not None:
            if not config.triage.data_dir.is_dir():
                if not _triage_store_warned:
                    eventlog.emit("warn", "mcp triage store unavailable", {})
                    _triage_store_warned = True
                return None
            _triage_store = TriageStore(config.triage.data_dir)
    return _triage_store


def _guarded(fn):
    """Per-tool error boundary (#231): the module previously had ZERO
    error handling — an exception propagated raw into FastMCP. Failures
    come back as the same "Error: ..." strings the tools already use,
    and land in the unified logger."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ConfigError as e:
            if not eventlog.was_reported(e):
                eventlog.emit("warn", "config load failed", {"error": str(e)})
            return f"Error: config failed to load: {e}"
        except Exception as e:
            # Skip an exception whose origin already emitted its own error
            # event (dispatch crash), or one incident files two issues (#237).
            if not eventlog.was_reported(e):
                eventlog.emit(
                    "error",
                    "mcp server tool failed",
                    {"tool": fn.__name__, "error": repr(e)},
                )
            return f"Error: {fn.__name__} failed: {e}"

    return wrapper


def _get_all_issues(*, force: bool = False) -> list:
    """Fetch shared open issues; explicit refresh bypasses both cache TTLs."""
    config = _get_config()
    cache = _get_github_cache()
    issues = (
        merge_issue_lists(
            cache.get_issues(proj.number, proj.owner, force=force) for proj in config.projects
        )
        if config
        else []
    )
    repos = {issue.repo for issue in issues}
    closed = cache.get_closed_keys(repos, force=force) if repos else set()
    return [issue for issue in issues if issue.key not in closed]


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


def _discover_agents_context() -> ToolContext:
    """Build context for agent-related tools (list, kill, jump, mute, unmute).

    Runs agent discovery and enrichment. Loads inactive state.
    """
    agents = enrich_all_agents(discover_agents())
    inactive = load_inactive_state()
    inactive_pane_ids = set(inactive.keys())
    return ToolContext(
        config=_get_config(),
        github_cache=_get_github_cache(),
        issues=[],
        agents=agents,
        active_screen="agents",
        selected_keys=set(),
        inactive_pane_ids=inactive_pane_ids,
    )


def _issues_context() -> ToolContext:
    """Build context for issue-related tools (list, dispatch, triage, set_status).

    Fetches issues from configured GitHub Projects. No agent discovery.
    """
    return ToolContext(
        config=_get_config(),
        github_cache=_get_github_cache(),
        issues=_get_all_issues(),
        triage_store=_get_triage_store(),
        agents=[],
        active_screen="dispatch",
        selected_keys=set(),
    )


# ---------------------------------------------------------------------------
# Tool wrappers
# ---------------------------------------------------------------------------


@mcp.tool()
@_guarded
def list_agents(
    repo: str | None = None,
    state: str | None = None,
    agent_type: str | None = None,
    format: str = "text",
) -> str:
    """List running AI coding agents across all tmux sessions.

    Can filter by repo name, state (idle/running/waiting), or agent type
    (claude/codex/aider). Claude agents show their native session name as
    `@name` — the handle to pass to SendMessage / an @-mention — plus the
    live native status (busy/waiting/idle, with what it is waiting for). A
    waiting session of either harness says how long: `waiting 3h12m`.
    "(derived)" marks an auto-generated name that changes on /rename.
    format="json" returns one record per agent: pane_id, agent_type, state,
    repo, branch, worktree, native_name, native_name_source
    (derived/user/auto), native_status, waiting_for, waiting_since (ISO 8601,
    null unless waiting; set for Codex too, where the two before it are null),
    and session_id — the transcript sessionId the name belongs to (names are
    mutable and can collide, so match on session_id when it matters). Read-only.
    """
    ctx = _discover_agents_context()
    result = tools.list_agents(ctx, repo=repo, state=state, agent_type=agent_type, format=format)
    return result.preview if result.success else f"Error: {result.error}"


@mcp.tool()
@_guarded
def kill_agent(identifier: str, confirm: bool = False) -> str:
    """Kill (stop) a running agent. Two-step handshake.

    Call without confirm first: the identifier (pane ID like %5, repo name,
    or description like 'idle claude in agent-hq') is resolved and a preview
    of the exact target is returned -- nothing is killed. To execute, call
    again with identifier="%<pane>" (exact pane id required) and confirm=true;
    the kill is then verified and the actual outcome reported.
    """
    ctx = _discover_agents_context()
    result = tools.kill_agent(ctx, identifier=identifier, confirm=confirm)
    if not result.success:
        return f"Error: {result.error}"
    if not confirm:
        return result.preview
    ok = result.execute()
    if result.outcome:
        return result.outcome
    return result.preview if ok else f"Failed: {result.preview}"


@mcp.tool()
@_guarded
def jump_to_agent(identifier: str) -> str:
    """Jump to (focus) an agent's tmux pane.

    Use pane ID (%5), repo name, or description like 'idle agent in agent-hq'.
    """
    ctx = _discover_agents_context()
    result = tools.jump_to_agent(ctx, identifier=identifier)
    if not result.success:
        return f"Error: {result.error}"
    ok = result.execute()
    return result.preview if ok else f"Failed: {result.preview}"


@mcp.tool()
@_guarded
def mute_agent(identifier: str) -> str:
    """Mute an agent so it is hidden from the active agents list.

    Use pane ID (%5), repo name, or description.
    """
    ctx = _discover_agents_context()
    result = tools.mute_agent(ctx, identifier=identifier)
    if not result.success:
        return f"Error: {result.error}"
    ok = result.execute()
    return result.preview if ok else f"Failed: {result.preview}"


@mcp.tool()
@_guarded
def unmute_agent(identifier: str) -> str:
    """Unmute a previously muted agent so it reappears in the list.

    Use pane ID (%5), repo name, or description.
    """
    ctx = _discover_agents_context()
    result = tools.unmute_agent(ctx, identifier=identifier)
    if not result.success:
        return f"Error: {result.error}"
    ok = result.execute()
    return result.preview if ok else f"Failed: {result.preview}"


@mcp.tool()
@_guarded
def list_issues(
    repo: str | None = None,
    status: str | None = None,
    effort: str | None = None,
    fitness: str | None = None,
    state: str | None = None,
    format: str = "text",
) -> str:
    """List open issues in Queue order: pinned, confirmed ranked, then untriaged.

    Text shows score, breakdown, flags and triage state. format="json" returns
    full structured records including dimensions and reasoning. Filter by repo
    substring, GitHub status, effort (S/M/L/none), fitness
    (solo/pair/needs_you/none), or state (untriaged/proposed/confirmed).
    Dimension/state filters require triage.data_dir. Read-only; never seeds.
    """
    ctx = _issues_context()
    result = tools.list_issues(
        ctx, repo=repo, status=status, effort=effort, fitness=fitness, state=state, format=format
    )
    return result.preview if result.success else f"Error: {result.error}"


@mcp.tool()
@_guarded
def dispatch_issues(
    issue_keys: list[str] | None = None,
    harness: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> str:
    """Dispatch one or more GitHub issues to a new agent worktree.

    Provide issue keys in 'owner/repo#number' format.

    ``harness`` selects the agent CLI the worktree launches (default:
    claude). A non-claude harness such as "codex" requires the installed
    launcher to advertise it (via its ``--capabilities`` contract) -- an
    older launcher, or one that doesn't support the harness, is rejected
    with an error before anything is dispatched. ``effort`` must be one of
    the recognized reasoning-effort levels (e.g. low/medium/high); ``model``
    is passed through as a free-form string.
    """
    ctx = _issues_context()
    result = tools.dispatch_issues(
        ctx, issue_keys=issue_keys, harness=harness, model=model, effort=effort
    )
    if not result.success:
        return f"Error: {result.error}"
    ok = result.execute()
    if result.outcome:
        # Fix 8 (multi-review wave): execute() sets outcome to the real
        # DispatchResult.error on failure -- surface it instead of the
        # generic (and less useful) "Failed: {preview}" fallback below.
        return result.outcome
    return result.preview if ok else f"Failed: {result.preview}"


@mcp.tool()
@_guarded
def open_chat_session(
    target: str,
    harness: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    prompt: str | None = None,
) -> str:
    """Open a bare interactive agent REPL in a tmux window for one-off work.

    No issue, no worktree, no /goal loop -- reach for this instead of
    dispatch_issues for operational one-offs (e.g. "/clean-gone --all").

    ``target``: "scratch" (the scratch session, in ``dispatch.scratch_cwd``)
    or "repo:<owner/name>" (the configured repo's main session); sessions
    are reused when they exist, created otherwise. ``harness``:
    blank = config default; a harness the installed launcher doesn't
    advertise is rejected before anything launches. ``model``/``effort``:
    passed to the harness command line; omit for the harness default.
    ``prompt``: submitted as the session's first message.

    Returns JSON: pane_id, session_name, harness, started, prompt_delivered
    (null without a prompt), message. Use pane_id with kill_agent later;
    the agent appears in list_agents once discovery sees it.
    """
    ctx = ToolContext(
        config=_get_config(),
        github_cache=None,
        issues=[],
        agents=[],
        active_screen="agents",
        selected_keys=set(),
    )
    result = tools.open_chat_session(
        ctx, target=target, harness=harness, model=model, effort=effort, prompt=prompt
    )
    if not result.success:
        return f"Error: {result.error}"
    result.execute()
    return result.outcome


@mcp.tool()
@_guarded
def set_triage(
    issue_key: str,
    value: int | None = None,
    effort: str | None = None,
    fitness: str | None = None,
    interest: bool | None = None,
    pin: bool | None = None,
    autopilot: bool | None = None,
) -> str:
    """Edit local triage with human provenance, preserving unsupplied fields.

    Supply value (1-5), effort (S/M/L), fitness (solo/pair/needs_you),
    interest and/or pin. Never confirms; use confirm_triage for explicit
    approval once all three dimensions are set. Nothing writes GitHub.
    autopilot opts the issue into the nightly autopilot (#302; it still
    needs confirm_triage).
    """
    result = tools.set_triage(
        _issues_context(),
        issue_key,
        value=value,
        effort=effort,
        fitness=fitness,
        interest=interest,
        pin=pin,
        autopilot=autopilot,
    )
    if not result.success:
        return f"Error: {result.error}"
    ok = result.execute()
    return result.outcome or (result.preview if ok else f"Failed: {result.preview}")


@mcp.tool()
@_guarded
def confirm_triage(issue_key: str) -> str:
    """Explicitly approve the currently stored value, effort and fitness.

    Confirms what is stored now, without a proposal revision check. All three
    dimensions must be set. Preserves provenance (AI stays AI); this human
    approval is separate from set_triage edits. Nothing writes GitHub.
    """
    result = tools.confirm_triage(_issues_context(), issue_key)
    if not result.success:
        return f"Error: {result.error}"
    ok = result.execute()
    return result.outcome or (result.preview if ok else f"Failed: {result.preview}")


@mcp.tool()
@_guarded
def set_status(issue_key: str, status: str) -> str:
    """Set the status of a GitHub issue on the project board.

    Provide issue key in 'owner/repo#number' format.
    Status must be one of: Todo, In Progress, Done.
    """
    ctx = _issues_context()
    result = tools.set_status(ctx, issue_key=issue_key, status=status)
    if not result.success:
        return f"Error: {result.error}"
    ok = result.execute()
    return result.preview if ok else f"Failed: {result.preview}"


@mcp.tool()
@_guarded
def refresh_issues() -> str:
    """Force-refresh GitHub issues from the project board, bypassing the cache.

    Use after marking issues in progress or when you need the latest data.
    """
    if not _get_config():
        return "No config loaded"
    issues = _get_all_issues(force=True)
    return f"Refreshed: {len(issues)} issues"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Agent HQ MCP server (stdio transport)."""
    eventlog.set_process_role("mcp")
    mcp.run()


if __name__ == "__main__":
    main()
