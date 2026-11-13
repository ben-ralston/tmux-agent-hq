"""Tool definitions for the natural language command interface.

Each tool is a plain Python function that returns a ToolResult with a deferred
execute() callable.  The MCP server in mcp_server.py wraps these functions.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from . import actions
from .adapters import get_adapter
from .capabilities import probe_launcher_capabilities, supports_harness
from .config import VALID_EFFORTS, VALID_HARNESSES
from .issues_hub import IssueAnnotation, annotate, filter_by_dimensions, queue_partition
from .models import Agent, AgentState, AgentType, Issue
from .runtime_status import CLAUDE_WAITING_FALLBACK
from .state import toggle_inactive
from .triage_store import AgentFitness, Effort, TriageRecord, load_rank_weights
from .triage_worker import MAX_REASONING_CHARS

if TYPE_CHECKING:
    from .config import Config
    from .github import GitHubCache
    from .triage_store import TriageStore


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Shared state available to every tool invocation."""

    config: Config | None
    github_cache: GitHubCache | None
    issues: list[Issue]
    agents: list[Agent]
    active_screen: str  # "agents" | "dispatch"
    selected_keys: set[str]  # Selected issue keys or pane_ids
    inactive_pane_ids: set[str] = field(default_factory=set)
    triage_store: TriageStore | None = None


@dataclass
class ToolResult:
    """Outcome of a tool call -- preview first, execute after confirmation."""

    success: bool
    preview: str  # Human-readable preview for confirmation modal
    execute: Callable[[], bool]  # Deferred execution, called after user confirms
    error: str = ""
    outcome: str = ""  # Past-tense result set by execute(); empty until then
    # Tool-specific raw result set by execute() (dispatch_issues: the
    # DispatchResult, whose ``launched`` the remote connector reports, #397).
    detail: Any = None


# ---------------------------------------------------------------------------
# Helper: fuzzy agent resolution
# ---------------------------------------------------------------------------


def _resolve_agent(identifier: str, agents: list[Agent]) -> list[Agent]:
    """Resolve a free-text identifier to matching agents.

    Priority order:
    1. Exact pane_id match (e.g. "%5")
    2. Repo name contains identifier (e.g. "agent-hq")
    3. Type + repo (e.g. "claude in agent-hq")
    4. State + repo (e.g. "idle in agent-hq")

    Returns list of matches (0 = not found, 1 = exact, >1 = ambiguous).
    """
    identifier = identifier.strip()
    if not identifier or not agents:
        return []

    # 1. Exact pane_id match
    if identifier.startswith("%"):
        matches = [a for a in agents if a.pane_id == identifier]
        if matches:
            return matches

    ident_lower = identifier.lower()

    # 3 & 4. Type/state + repo: "claude in agent-hq" or "idle in agent-hq"
    if " in " in ident_lower:
        parts = ident_lower.split(" in ", 1)
        qualifier, repo_str = parts[0].strip(), parts[1].strip()

        # 3. Type + repo
        type_repo_matches = [
            a
            for a in agents
            if a.agent_type.value.lower() == qualifier
            and a.repo_name
            and repo_str in a.repo_name.lower()
        ]
        if type_repo_matches:
            return type_repo_matches

        # 4. State + repo
        state_repo_matches = [
            a
            for a in agents
            if a.state.value.lower() == qualifier
            and a.repo_name
            and repo_str in a.repo_name.lower()
        ]
        if state_repo_matches:
            return state_repo_matches

    # 2. Repo name contains identifier (checked after compound patterns)
    repo_matches = [a for a in agents if a.repo_name and ident_lower in a.repo_name.lower()]
    if repo_matches:
        return repo_matches

    return []


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _noop() -> bool:
    """No-op execute function for read-only tools."""
    return True


def dispatch_issues(
    context: ToolContext,
    issue_keys: list[str] | None = None,
    harness: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> ToolResult:
    """Dispatch issues to a new agent worktree.

    ``harness`` selects the agent CLI the dispatched worktree launches
    (default: claude, the controller pane's own harness) -- an explicit
    argument here beats ``context.config``'s per-harness defaults, mirroring
    ``dispatch_via_claude``/``resolve_dispatch_selection`` exactly (same
    helper, same precedence). The *resolved* harness (explicit, else the
    config default) is only accepted when it is both a known harness
    (``VALID_HARNESSES``) and actually advertised by the installed launcher
    (``probe_launcher_capabilities()`` / ``supports_harness()``) -- an older
    launch.sh predating that contract is rejected here, before dispatch,
    rather than failing later. The resolved ``effort`` is validated against
    ``VALID_EFFORTS``; ``model`` is passed through unvalidated (mirrors
    ``dispatch_via_claude``).
    """
    keys = list(issue_keys) if issue_keys else []

    # Fall back to currently selected keys
    if not keys and context.selected_keys:
        keys = list(context.selected_keys)

    if not keys:
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error="No issues specified and none selected.",
        )

    # Resolve keys to Issue objects
    issue_map = {i.key: i for i in context.issues}
    resolved: list[Issue] = []
    missing: list[str] = []
    for key in keys:
        if key in issue_map:
            resolved.append(issue_map[key])
        else:
            missing.append(key)

    if missing:
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"Issues not found: {', '.join(missing)}",
        )

    # Validate: all issues from same repo
    repos = {i.repo for i in resolved}
    if len(repos) > 1:
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=(
                f"Issues span multiple repos: {', '.join(sorted(repos))}. "
                "Dispatch one repo at a time."
            ),
        )

    # Resolve harness/model/effort exactly as dispatch_via_claude will --
    # via the shared helper -- so the preview can never drift from what
    # actually gets dispatched (config default_harness / per-harness
    # defaults included). Without a config, fall back to validating the
    # raw inputs directly (there's no config default to resolve against).
    if context.config is not None:
        from .dispatch import resolve_dispatch_selection

        try:
            resolved_harness, resolved_model, resolved_effort = resolve_dispatch_selection(
                context.config, harness, model, effort
            )
        except ValueError as e:
            return ToolResult(success=False, preview="", execute=_noop, error=str(e))
    else:
        resolved_harness = harness or "claude"
        resolved_model = model
        resolved_effort = effort
        if harness is not None and harness not in VALID_HARNESSES:
            return ToolResult(
                success=False,
                preview="",
                execute=_noop,
                error=(
                    f"Unknown harness: {harness!r}. "
                    f"Must be one of: {', '.join(sorted(VALID_HARNESSES))}"
                ),
            )
        if effort is not None and effort not in VALID_EFFORTS:
            return ToolResult(
                success=False,
                preview="",
                execute=_noop,
                error=(
                    f"Invalid effort: {effort!r}. "
                    f"Must be one of: {', '.join(sorted(VALID_EFFORTS))}"
                ),
            )

    if resolved_harness != "claude" and not supports_harness(
        probe_launcher_capabilities(), resolved_harness
    ):
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=(
                f"Harness {resolved_harness!r} is not supported by the installed launcher -- "
                "upgrade launch.sh or dispatch without a harness override."
            ),
        )

    numbers = ", ".join(f"#{i.number}" for i in resolved)
    repo = resolved[0].repo_short

    # Build the same flag fragment dispatch_via_claude will emit, so the
    # preview can never drift from what actually gets sent.
    adapter = get_adapter(resolved_harness)
    flag_args = adapter.launch_args(resolved_model, resolved_effort) if adapter else []
    prompt_args = " ".join([*flag_args, numbers]) if flag_args else numbers

    preview = f"Will send /worktree-create {prompt_args} to Claude in {repo} session"

    result = ToolResult(success=True, preview=preview, execute=_noop)

    def _execute() -> bool:
        from .dispatch import dispatch_via_claude

        if context.config is None:
            result.outcome = "Failed: no config loaded -- cannot resolve worktree_root."
            return False
        dispatch_result = dispatch_via_claude(
            [resolved], context.config, harness=harness, model=model, effort=effort
        )
        result.detail = dispatch_result
        if not dispatch_result.success:
            # Fix 8 (multi-review wave): surface the real DispatchResult.error
            # via result.outcome (mirrors kill_agent's pattern) instead of
            # discarding it -- the MCP handler's generic "Failed: {preview}"
            # fallback told the user nothing about WHY dispatch failed.
            result.outcome = f"Failed: {dispatch_result.error}"
        return dispatch_result.success

    result.execute = _execute
    return result


def open_chat_session(
    context: ToolContext,
    target: str,
    harness: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    prompt: str | None = None,
) -> ToolResult:
    """Open a bare interactive harness REPL -- no issue, no worktree (#290).

    The MCP twin of the web "+ New session" button (#169): ``target`` is
    ``scratch`` (find-or-create the ``scratch`` session, cwd from
    ``dispatch.scratch_cwd``) or ``repo:<owner/name>`` (the repo's main
    session, same find-or-create path dispatch uses). ``harness`` resolves
    like the web route (blank -> config default; unsupported -> error before
    anything launches). Unlike the web flow, an explicit ``model``/``effort``
    reaches the harness command line; omitted means the harness's own
    default, never the dispatch config's per-harness pins (those belong to
    worktree agents). ``prompt`` rides the harness argv, so the harness
    submits it at startup. execute() waits for the harness process to appear
    in the pane (the same process check dispatch uses) and sets ``outcome``
    to a JSON object: pane_id, session_name, harness, started,
    prompt_delivered (null when no prompt) and a message.
    """
    from pathlib import Path

    from . import dispatch

    def _error(message: str) -> ToolResult:
        return ToolResult(success=False, preview="", execute=_noop, error=message)

    config = context.config
    repos = config.repos if config is not None else {}
    if target == "scratch":
        cwd = config.dispatch.scratch_cwd if config else Path.home()
        label = "scratch"
        repo_path = None
    elif target.startswith("repo:") and target[len("repo:") :] in repos:
        repo_path = repos[target[len("repo:") :]].local_path
        cwd = repo_path
        label = target[len("repo:") :]
    else:
        choices = ", ".join(["scratch", *(f"repo:{name}" for name in sorted(repos))])
        return _error(f"Unknown target: {target!r}. Must be one of: {choices}")

    try:
        resolved_harness, _, _ = dispatch.resolve_and_validate_harness(
            config,
            probe_launcher_capabilities(),
            harness or None,
            resolve_model_effort=False,
        )
    except ValueError as e:
        return _error(str(e))
    model = model or None
    effort = effort or None
    prompt = prompt if prompt and prompt.strip() else None
    efforts = get_adapter(resolved_harness).chat_efforts
    if effort is not None and effort not in efforts:
        return _error(
            f"Invalid effort for {resolved_harness}: {effort!r}. "
            f"Must be one of: {', '.join(sorted(efforts))}"
        )

    # The launch line is TYPED into the pane's shell: a Ctrl-C/Ctrl-U would
    # cancel or erase it and run the rest as a command, a Tab would trigger
    # completion. Prompt newlines are fine -- they sit inside the quoting.
    def _has_control(text: str, allowed: str = "") -> bool:
        return any((ord(c) < 32 or ord(c) == 127) and c not in allowed for c in text)

    if model is not None and (
        model.startswith("-") or any(c.isspace() for c in model) or _has_control(model)
    ):
        return _error(
            "Invalid model (must be a single token, no whitespace, control characters "
            f"or leading '-'): {model!r}"
        )
    if prompt is not None and _has_control(prompt, allowed="\n"):
        return _error("Invalid prompt: control characters (other than newlines) are not allowed.")

    launch_line = dispatch.chat_launch_line(
        resolved_harness, model=model, effort=effort, prompt=prompt
    )
    preview = f"Will open in {label}: {launch_line}"
    result = ToolResult(success=True, preview=preview, execute=_noop)

    def _execute() -> bool:
        if repo_path is None:
            existing = dispatch._find_session_by_name("scratch")
            name = "scratch"
        else:
            existing = dispatch._find_repo_session(repo_path)
            name = existing or dispatch._repo_session_name(repo_path)
        pane_id = dispatch.open_chat_window(
            existing_session=existing,
            new_session_name=name,
            cwd=cwd,
            harness=resolved_harness,
            model=model,
            effort=effort,
            prompt=prompt,
        )
        if pane_id is None:
            result.outcome = f"Failed: could not create a tmux window in session {name!r}."
            return False
        started = dispatch._wait_for_harness_process(pane_id, resolved_harness)
        if started:
            # The prompt rode argv; the harness submits it once ready. A
            # first-run folder-trust dialog can still hold it, so this says
            # "delivered", not "submitted" -- capture the pane to be sure.
            message = f"Opened {resolved_harness} in {name}" + (
                "; the prompt was passed on its command line." if prompt is not None else "."
            )
        else:
            message = (
                f"Window opened in {name}, but {resolved_harness} startup could not be "
                "confirmed; the pane is left open for inspection."
            )
        result.outcome = json.dumps(
            {
                "pane_id": pane_id,
                "session_name": name,
                "harness": resolved_harness,
                "started": started,
                "prompt_delivered": started if prompt is not None else None,
                "message": message,
            }
        )
        return started

    result.execute = _execute
    return result


def _owner_for_project(config: "Config | None", project_number: int) -> str:
    """Look up the owner for a project number from config."""
    if config:
        for proj in config.projects:
            if proj.number == project_number:
                return proj.owner
    return "@me"


_VALID_STATUSES = {"Todo", "In Progress", "Done"}


def _triage_write(
    context: ToolContext, issue_key: str, *, changes: dict | None = None, actor: str = "human"
) -> ToolResult:
    store = context.triage_store
    if store is None:
        return ToolResult(
            False, "", _noop, "triage store not configured (set triage.data_dir in projects.yaml)"
        )
    issue = next((i for i in context.issues if i.key == issue_key), None)
    if issue is None:
        return ToolResult(False, "", _noop, f"Issue not found: {issue_key}")
    # Capture the target and supplied values when preparing deferred execution.
    issue = replace(issue)
    changes = dict(changes) if changes is not None else None
    result = ToolResult(
        True, f"Will {'edit' if changes is not None else 'confirm'} triage for {issue_key}", _noop
    )

    def execute() -> bool:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            if changes is None:
                record = store.confirm(issue_key, now=now)
                failure = (
                    "Record missing, tombstoned, incomplete or unreadable; "
                    "set value, effort and fitness first"
                )
            else:
                record = store.upsert(
                    TriageRecord(issue_key=issue_key, updated_at=now, **changes), actor=actor
                )
                failure = "Triage store unreadable; update failed"
            if record is None:
                result.success = False
                result.error = failure
                result.outcome = f"Error: {failure}"
                return False
            annotations = annotate(
                [issue], store, load_rank_weights(store.data_dir), records=[record]
            )
            result.outcome = _triage_line(issue, annotations[issue_key])
            return True
        except OSError as exc:
            result.success = False
            result.error = f"Triage write failed: {exc}"
            result.outcome = f"Error: {result.error}"
            return False

    result.execute = execute
    return result


def _normalize_triage_dimension(value: str) -> str:
    """Use the same dimension spelling for edits and listing filters."""
    return value.lower().replace("-", "_").replace(" ", "_")


def set_triage(
    context: ToolContext,
    issue_key: str,
    value: int | None = None,
    effort: str | None = None,
    fitness: str | None = None,
    interest: bool | None = None,
    pin: bool | None = None,
    autopilot: bool | None = None,
    reasoning: str | None = None,
    actor: str = "human",
) -> ToolResult:
    """Edit supplied dimensions; never confirm or write GitHub.

    ``actor`` is the provenance stamped on the supplied dimensions (human by
    default; the remote connector passes "duet", #397). ``reasoning`` is one
    printable sentence stored against each supplied dimension.
    """
    if all(v is None for v in (value, effort, fitness, interest, pin, autopilot)):
        return ToolResult(False, "", _noop, "Supply at least one triage field")
    if reasoning is not None and (
        not isinstance(reasoning, str)
        or not reasoning.strip()
        or len(reasoning) > MAX_REASONING_CHARS
        or not reasoning.isprintable()
    ):
        return ToolResult(
            False,
            "",
            _noop,
            f"reasoning must be one printable line of 1-{MAX_REASONING_CHARS} characters",
        )
    if value is not None and (type(value) is not int or not 1 <= value <= 5):
        return ToolResult(False, "", _noop, "value must be an integer from 1 to 5")
    for name, flag in (("interest", interest), ("pin", pin), ("autopilot", autopilot)):
        if flag is not None and not isinstance(flag, bool):
            return ToolResult(False, "", _noop, f"{name} must be a boolean")
    try:
        effort_enum = (
            Effort(_normalize_triage_dimension(effort).upper()) if effort is not None else None
        )
    except (ValueError, AttributeError):
        return ToolResult(False, "", _noop, "effort must be S, M or L")
    try:
        fitness_enum = (
            AgentFitness(_normalize_triage_dimension(fitness)) if fitness is not None else None
        )
    except (ValueError, AttributeError):
        return ToolResult(False, "", _noop, "fitness must be solo, pair or needs_you")
    return _triage_write(
        context,
        issue_key,
        changes={
            "value": value,
            "effort": effort_enum,
            "agent_fitness": fitness_enum,
            "interest": interest,
            "pin": pin,
            "autopilot": autopilot,
            "reasoning": (
                {
                    dim: reasoning.strip()
                    for dim, supplied in (
                        ("value", value),
                        ("effort", effort_enum),
                        ("agent_fitness", fitness_enum),
                    )
                    if supplied is not None
                }
                if reasoning is not None
                else {}
            ),
        },
        actor=actor,
    )


def confirm_triage(context: ToolContext, issue_key: str) -> ToolResult:
    """Explicitly approve the currently stored dimensions; no GitHub write."""
    return _triage_write(context, issue_key)


def set_status(
    context: ToolContext,
    issue_key: str,
    status: str,
) -> ToolResult:
    """Set the status of a GitHub issue on the project board."""
    if status not in _VALID_STATUSES:
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"Invalid status: {status}. Valid: Todo, In Progress, Done.",
        )

    issue_map = {i.key: i for i in context.issues}
    issue = issue_map.get(issue_key)

    if not issue:
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"Issue not found: {issue_key}",
        )

    preview = f"Will set status to {status} on {issue.repo_short}#{issue.number}"

    def _execute() -> bool:
        if context.github_cache is None:
            return False
        owner = _owner_for_project(context.config, issue.project_number)
        return context.github_cache.set_status(issue, owner, status)

    return ToolResult(success=True, preview=preview, execute=_execute)


def kill_agent(
    context: ToolContext,
    identifier: str,
    confirm: bool = False,
) -> ToolResult:
    """Kill (stop) a running agent.

    Two-step handshake: without confirm, resolve the identifier and return a
    preview of the exact target -- nothing executes. With confirm=True (which
    requires an exact pane-id identifier), execute the kill and verify the
    agent actually died, reporting the real outcome via ToolResult.outcome.
    """
    if confirm and not identifier.strip().startswith("%"):
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=(
                "confirm=true requires an exact pane id -- "
                "call without confirm first to preview the target."
            ),
        )

    matches = _resolve_agent(identifier, context.agents)

    if not matches:
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"No agent found matching: {identifier}",
        )

    if len(matches) > 1:
        desc = ", ".join(f"{a.pane_id} ({a.agent_type.value}, {a.repo_name})" for a in matches)
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"Ambiguous -- multiple agents match: {desc}",
        )

    agent = matches[0]
    desc_parts = [agent.agent_type.value, agent.repo_name or agent.cwd or "?"]
    if agent.branch:
        desc_parts.append(agent.branch)
    desc_parts.append(agent.state.value)
    target_desc = ", ".join(desc_parts)
    if agent.pane_id in context.inactive_pane_ids:
        target_desc += " [muted -- hidden from list_agents]"

    if not confirm:
        preview = (
            f"Would kill agent in {agent.pane_id} -- {target_desc}.\n"
            f"Not executed. To proceed: "
            f'kill_agent(identifier="{agent.pane_id}", confirm=true)'
        )
        return ToolResult(success=True, preview=preview, execute=_noop)

    preview = f"Will kill agent in {agent.pane_id} ({target_desc})"
    result = ToolResult(success=True, preview=preview, execute=_noop)

    def _execute() -> bool:
        # A dying agent can close its pane after the first Ctrl-C, failing the
        # second send-keys -- so verification, not send-keys exit status,
        # decides the outcome.
        sent = actions.kill_agent(agent.pane_id)
        died, remaining = actions.verify_agent_killed(agent.pane_id)
        if died:
            result.outcome = (
                f"Killed {agent.agent_type.value} agent in {agent.pane_id} "
                f"({agent.repo_name or agent.cwd or '?'}) -- verified: "
                "no agent process remains in the pane."
            )
            return True
        if not sent:
            result.outcome = (
                f"Failed to send Ctrl-C to {agent.pane_id} -- the pane may no longer exist."
            )
            return False
        if remaining == AgentType.UNKNOWN:
            result.outcome = (
                f"Sent Ctrl-C twice to {agent.pane_id} but could not verify the agent "
                "died (pane inspection failed); jump to the pane to check."
            )
            return False
        from .enrichment import get_agent_state

        state = get_agent_state(
            agent.pane_id,
            remaining,
            agent_pid=agent.agent_pid if remaining is agent.agent_type else None,
        )
        result.outcome = (
            f"Sent Ctrl-C twice to {agent.pane_id} but a {remaining.value} agent "
            f"is still running (state: {state.value}) -- it may be stuck at a "
            "prompt; jump to the pane to inspect."
        )
        return False

    result.execute = _execute
    return result


def jump_to_agent(
    context: ToolContext,
    identifier: str,
) -> ToolResult:
    """Jump to (focus) an agent's tmux pane."""
    matches = _resolve_agent(identifier, context.agents)

    if not matches:
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"No agent found matching: {identifier}",
        )

    if len(matches) > 1:
        desc = ", ".join(f"{a.pane_id} ({a.agent_type.value}, {a.repo_name})" for a in matches)
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"Ambiguous -- multiple agents match: {desc}",
        )

    agent = matches[0]
    preview = f"Will jump to agent in {agent.pane_id} ({agent.agent_type.value}, {agent.repo_name})"

    def _execute() -> bool:
        return actions.jump_to_pane(agent.pane_id)

    return ToolResult(success=True, preview=preview, execute=_execute)


def mute_agent(
    context: ToolContext,
    identifier: str,
) -> ToolResult:
    """Mute an agent so it is hidden from the active list."""
    matches = _resolve_agent(identifier, context.agents)

    if not matches:
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"No agent found matching: {identifier}",
        )

    if len(matches) > 1:
        desc = ", ".join(f"{a.pane_id} ({a.agent_type.value}, {a.repo_name})" for a in matches)
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"Ambiguous -- multiple agents match: {desc}",
        )

    agent = matches[0]
    if agent.pane_id in context.inactive_pane_ids:
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"Agent {agent.pane_id} is already muted.",
        )

    preview = f"Will mute agent in {agent.pane_id} ({agent.agent_type.value}, {agent.repo_name})"

    def _execute() -> bool:
        from .enrichment import compute_content_fingerprint

        fingerprint = compute_content_fingerprint(agent.pane_id)
        toggle_inactive(agent.pane_id, fingerprint)
        return True

    return ToolResult(success=True, preview=preview, execute=_execute)


def unmute_agent(
    context: ToolContext,
    identifier: str,
) -> ToolResult:
    """Unmute a previously muted agent so it reappears."""
    matches = _resolve_agent(identifier, context.agents)

    if not matches:
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"No agent found matching: {identifier}",
        )

    if len(matches) > 1:
        desc = ", ".join(f"{a.pane_id} ({a.agent_type.value}, {a.repo_name})" for a in matches)
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"Ambiguous -- multiple agents match: {desc}",
        )

    agent = matches[0]
    if agent.pane_id not in context.inactive_pane_ids:
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"Agent {agent.pane_id} is not muted.",
        )

    preview = f"Will unmute agent in {agent.pane_id} ({agent.agent_type.value}, {agent.repo_name})"

    def _execute() -> bool:
        toggle_inactive(agent.pane_id, "")
        return True

    return ToolResult(success=True, preview=preview, execute=_execute)


def _triage_annotations(context: ToolContext) -> dict[str, IssueAnnotation]:
    store = context.triage_store
    return annotate(context.issues, store, load_rank_weights(store.data_dir)) if store else {}


def _triage_line(issue: Issue, ann: IssueAnnotation) -> str:
    chip = (
        "—"
        if ann.score is None
        else " · ".join([str(ann.score), *(label for label, _ in ann.breakdown)])
    )
    pin = " ⌖" if ann.pinned else ""
    state = ann.record.state.value if ann.record else "untriaged"
    sentinel = " ⚑" if issue.is_sentinel else ""
    return f"  {issue.key}  {chip}{pin} [{state}] [{issue.status or '---'}] {issue.title}{sentinel}"


def _triage_json(issue: Issue, ann: IssueAnnotation) -> dict:
    rec = ann.record
    return {
        "key": issue.key,
        "repo": issue.repo,
        "number": issue.number,
        "title": issue.title,
        "status": issue.status,
        "labels": issue.labels,
        "sentinel": issue.is_sentinel,
        "state": rec.state.value if rec else "untriaged",
        "value": rec.value if rec else None,
        "effort": rec.effort.value if rec and rec.effort else None,
        "agent_fitness": rec.agent_fitness.value if rec and rec.agent_fitness else None,
        "interest": rec.interest if rec else None,
        "pin": rec.pin if rec else None,
        "score": ann.score,
        "breakdown": ann.breakdown,
        "reasoning": {
            key: "".join(c for c in value if c.isprintable())
            for key, value in rec.reasoning.items()
            if isinstance(value, str)
        }
        if rec
        else {},
        "model": rec.model if rec else None,
        "proposed_at": rec.proposed_at if rec else None,
        "updated_at": rec.updated_at if rec else None,
    }


def list_issues(
    context: ToolContext,
    *,
    repo: str | None = None,
    status: str | None = None,
    effort: str | None = None,
    fitness: str | None = None,
    state: str | None = None,
    format: str = "text",
) -> ToolResult:
    """List the local triage backlog in Queue order. Read-only; never seeds."""
    effort = _normalize_triage_dimension(effort) if effort is not None else None
    fitness = _normalize_triage_dimension(fitness) if fitness is not None else None
    for name, value, allowed in [
        ("format", format, ("text", "json")),
        ("effort", effort, ("S", "M", "L", "none")),
        ("fitness", fitness, ("solo", "pair", "needs_you", "none")),
        ("state", state, ("untriaged", "proposed", "confirmed")),
    ]:
        if value is not None and value.lower() not in {v.lower() for v in allowed}:
            return ToolResult(
                False, "", _noop, f"Invalid {name}: {value}. Valid: {', '.join(allowed)}"
            )
    if context.triage_store is None and any(v is not None for v in (effort, fitness, state)):
        return ToolResult(
            False, "", _noop, "triage store not configured (set triage.data_dir in projects.yaml)"
        )
    annotations = _triage_annotations(context)
    filtered = list(context.issues)
    if repo:
        filtered = [i for i in filtered if repo.lower() in i.repo_short.lower()]
    if status:
        filtered = [i for i in filtered if i.status and i.status.lower() == status.lower()]
    filtered = filter_by_dimensions(
        filtered,
        annotations,
        efforts=[effort.lower()] if effort is not None else None,
        fitnesses=[fitness.lower()] if fitness is not None else None,
    )
    if state is not None:
        filtered = [
            i
            for i in filtered
            if (annotations[i.key].record.state.value if annotations[i.key].record else "untriaged")
            == state.lower()
        ]
    filtered = [i for section in queue_partition(filtered, annotations) for i in section]
    if format.lower() == "json":
        preview = json.dumps(
            [_triage_json(i, annotations.get(i.key, IssueAnnotation())) for i in filtered],
            ensure_ascii=False,
        )
    elif filtered:
        preview = "\n".join(
            [
                f"Found {len(filtered)} issue(s):",
                *[_triage_line(i, annotations.get(i.key, IssueAnnotation())) for i in filtered],
            ]
        )
    else:
        preview = "No issues match the given filters."
    return ToolResult(success=True, preview=preview, execute=_noop)


def _native_status(agent: Agent) -> tuple[str | None, str | None]:
    """``(native_status, waiting_for)`` from a fresh Claude live observation.

    ``None`` for non-Claude agents and for an Unknown observation (no
    collector, expired, ambiguous) — never inferred from anything else.
    """
    snap = agent.status_snapshot
    if snap is None or snap.source != "claude" or snap.state == "unknown":
        return None, None
    waiting_for = snap.reason if snap.state == "waiting" else None
    if waiting_for == CLAUDE_WAITING_FALLBACK:
        waiting_for = None  # the collector's placeholder, not native waitingFor
    return snap.native_value, waiting_for or None


def _waiting_since(agent: Agent) -> float | None:
    """When a session seen waiting right now started to (#415), either harness."""
    snap = agent.status_snapshot
    return snap.waiting_since if snap is not None and snap.state == "waiting" else None


def waiting_since_iso(agent: Agent) -> str | None:
    since = _waiting_since(agent)
    if since is None:
        return None
    return datetime.fromtimestamp(since).astimezone().isoformat(timespec="seconds")


def format_waited(since: float, *, now: float | None = None) -> str:
    """``<1m``, ``45m``, ``3h12m``, ``2d3h``."""
    minutes = int(((time.time() if now is None else now) - since) // 60)
    if minutes < 1:
        return "<1m"
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h" if days else f"{hours}h{minutes}m"


def _agent_json(agent: Agent) -> dict[str, Any]:
    native_status, waiting_for = _native_status(agent)
    return {
        "pane_id": agent.pane_id,
        "agent_type": agent.agent_type.value,
        "state": agent.state.value,
        "repo": agent.repo_name,
        "branch": agent.branch,
        "worktree": agent.worktree_name,
        "native_name": agent.native_name,
        "native_name_source": agent.native_name_source,
        "native_status": native_status,
        "waiting_for": waiting_for,
        "waiting_since": waiting_since_iso(agent),
        "session_id": agent.claude_transcript_session_id,
    }


def _agent_line(agent: Agent) -> str:
    line = (
        f"  {agent.pane_id}  {agent.state.emoji} {agent.agent_type.value}"
        f"  {agent.repo_name or '---'}"
    )
    if agent.native_name:
        line += f"  @{agent.native_name}"
        if agent.native_name_source == "derived":
            line += " (derived)"
    native_status, waiting_for = _native_status(agent)
    since = _waiting_since(agent)
    status_text = f"waiting {format_waited(since)}" if since is not None else native_status
    if status_text:
        line += f" · {status_text}"
        if waiting_for:
            line += f": {waiting_for}"
    return line


def list_agents(
    context: ToolContext,
    repo: str | None = None,
    state: str | None = None,
    agent_type: str | None = None,
    format: str = "text",
) -> ToolResult:
    """List agents, optionally filtered by repo, state, or type. Read-only."""
    format = format or "text"
    if format.lower() not in ("text", "json"):
        return ToolResult(False, "", _noop, f"Invalid format: {format}. Valid: text, json")
    filtered = [a for a in context.agents if a.pane_id not in context.inactive_pane_ids]

    if repo:
        repo_lower = repo.lower()
        filtered = [a for a in filtered if a.repo_name and repo_lower in a.repo_name.lower()]

    if state:
        try:
            st = AgentState(state.lower())
        except ValueError:
            return ToolResult(
                success=False,
                preview="",
                execute=_noop,
                error=(f"Unknown state: {state}. Valid: idle, running, waiting, unknown"),
            )
        filtered = [a for a in filtered if a.state == st]

    if agent_type:
        try:
            at = AgentType(agent_type.lower())
        except ValueError:
            return ToolResult(
                success=False,
                preview="",
                execute=_noop,
                error=(
                    f"Unknown agent type: {agent_type}. "
                    "Valid: claude, codex, aider, opencode, gemini, cursor, roo"
                ),
            )
        filtered = [a for a in filtered if a.agent_type == at]

    if format.lower() == "json":
        preview = json.dumps([_agent_json(a) for a in filtered], ensure_ascii=False)
    elif not filtered:
        preview = "No agents match the given filters."
    else:
        lines = [f"Found {len(filtered)} agent(s):"]
        lines.extend(_agent_line(agent) for agent in filtered)
        preview = "\n".join(lines)

    return ToolResult(success=True, preview=preview, execute=_noop)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_TOOL_REGISTRY: dict[str, Callable[..., ToolResult]] = {
    "dispatch_issues": lambda args, ctx: dispatch_issues(ctx, **args),
    "open_chat_session": lambda args, ctx: open_chat_session(ctx, **args),
    "set_triage": lambda args, ctx: set_triage(ctx, **args),
    "confirm_triage": lambda args, ctx: confirm_triage(ctx, **args),
    "set_status": lambda args, ctx: set_status(ctx, **args),
    "kill_agent": lambda args, ctx: kill_agent(ctx, **args),
    "jump_to_agent": lambda args, ctx: jump_to_agent(ctx, **args),
    "mute_agent": lambda args, ctx: mute_agent(ctx, **args),
    "unmute_agent": lambda args, ctx: unmute_agent(ctx, **args),
    "list_issues": lambda args, ctx: list_issues(ctx, **args),
    "list_agents": lambda args, ctx: list_agents(ctx, **args),
}


def execute_tool_call(
    name: str,
    args: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    """Dispatch a tool call by name.

    Returns a ToolResult -- on unknown tool names, returns a failed result.
    """
    func = _TOOL_REGISTRY.get(name)
    if func is None:
        return ToolResult(
            success=False,
            preview="",
            execute=_noop,
            error=f"Unknown tool: {name}",
        )
    return func(args, context)
