"""Dispatch issues via Claude Code sessions."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import eventlog
from .adapters import get_adapter
from .capabilities import (
    LauncherCapabilities,
    probe_launcher_capabilities,
    supported_flags,
    supports_harness,
)
from .config import VALID_EFFORTS, VALID_HARNESSES, Config, RepoConfig
from .discovery import build_process_tree, find_agent_command, get_all_panes
from .models import Issue

# Launch settings pinned on the issue-dispatch CONTROLLER session (#232) -- the
# claude Agent HQ starts to run /worktree-create. Left to itself it inherits the
# user's default model and config.dispatch.permission_mode ("acceptEdits"), which
# are the wrong defaults for a session whose whole job is to classify an issue and
# fire a launcher. "opus" is the alias for the latest Opus, so this doesn't pin a
# version. These deliberately override config.dispatch.permission_mode (the
# bare chat window has never had a permission mode -- open_chat_window sends no
# permission flag).
# They do NOT reach the dispatched worktree agent -- /worktree-create picks that
# model/mode from its own tier table (and from any --model/--effort Agent HQ
# forwards via resolve_dispatch_selection).
CONTROLLER_MODEL = "opus"
CONTROLLER_EFFORT = "high"
CONTROLLER_PERMISSION_MODE = "auto"


@dataclass
class DispatchResult:
    """Result of a dispatch operation."""

    success: bool
    branch: str = ""
    error: str = ""
    warning: str = ""
    # A FAILED dispatch that may nonetheless have started an agent (#59).
    # The harness command reaches the pane before readiness is checked, so a
    # readiness timeout can still boot claude/codex and run the prompt that
    # rode in on its argv. Callers use this to decide what is safe to retry:
    # restoring such a selection invites a duplicate worktree/pane.
    # Meaningless when ``success`` is True.
    launched: bool = False


def resolve_dispatch_selection(
    config: Config,
    harness: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Resolve (harness, model, effort): explicit > config harness defaults > None.

    An explicit argument beats the matching ``config.dispatch.harnesses[resolved_harness]``
    default, which in turn beats no override at all (``harness`` itself falls back to
    ``config.dispatch.default_harness`` when not given explicitly).

    Raises ``ValueError`` -- naming the offending value -- when the resolved harness
    isn't in ``VALID_HARNESSES``, the resolved effort isn't in ``VALID_EFFORTS``, or
    the resolved model contains whitespace (or is an explicit empty string).
    ``model`` is otherwise a free pass-through string, unvalidated here.

    The whitespace check matters because ``model`` rides ``/worktree-create``'s
    argv as a single token (``dispatch_via_claude`` / ``adapters.launch_args``):
    a model value like ``"sonnet 999 --effort max"`` would otherwise splice
    phantom arguments into that command's parsing.

    This is the single resolution boundary shared by ``dispatch_via_claude`` (which
    uses the resolved values to build the actual dispatch) and
    ``tools.dispatch_issues`` (which uses them to build a preview that can't drift
    from what dispatch will actually do).
    """
    resolved_harness = harness or config.dispatch.default_harness
    if resolved_harness not in VALID_HARNESSES:
        raise ValueError(f"Unknown harness: {resolved_harness!r}")
    defaults = config.dispatch.harnesses.get(resolved_harness)
    resolved_model = model if model is not None else (defaults.model if defaults else None)
    resolved_effort = effort if effort is not None else (defaults.effort if defaults else None)
    if resolved_effort is not None and resolved_effort not in VALID_EFFORTS:
        raise ValueError(f"Invalid effort: {resolved_effort!r}")
    if resolved_model is not None and (
        not resolved_model or any(c.isspace() for c in resolved_model)
    ):
        raise ValueError(
            f"Invalid model (must be a single token, no whitespace): {resolved_model!r}"
        )
    return resolved_harness, resolved_model, resolved_effort


def resolve_and_validate_harness(
    config: Config | None,
    caps: LauncherCapabilities,
    harness: str | None,
    model: str | None = None,
    effort: str | None = None,
    *,
    resolve_model_effort: bool = True,
) -> tuple[str, str | None, str | None]:
    """Resolve a harness/model/effort selection from a request, and reject a
    resolved non-claude harness the installed launcher doesn't advertise.

    Mirrors ``resolve_dispatch_selection`` (explicit > config default > None)
    when ``config`` is available; without one (no ``projects.yaml``) it falls
    back to validating the raw inputs directly against ``VALID_HARNESSES`` /
    ``VALID_EFFORTS`` -- the same fallback ``tools.dispatch_issues`` uses when
    there's no config to resolve defaults from. Either way, the *resolved*
    harness is only accepted when it's ``"claude"`` or actually advertised by
    ``caps`` (``probe_launcher_capabilities()`` upstream) -- an older
    launch.sh predating the capabilities contract must never be treated as
    codex-capable. Raises ``ValueError`` (named after the offending value) on
    any invalid input; callers turn this into a 400 in their own error style.

    ``resolve_model_effort=False`` (multi-review fix wave, Fix 6) is for
    model-less callers -- the bare-REPL new-session flow, which accepts no
    model/effort field at all. Without this,
    ``resolve_dispatch_selection`` would still resolve (and validate) the
    CONFIGURED harness default model/effort even though the caller passed
    ``None`` for both, so a bad ``config.dispatch.harnesses[...].model``
    would 400 a request that never uses model/effort in the first place. In
    this mode only the harness is resolved/validated; ``model``/``effort``
    pass through unresolved (typically ``None``, since neither flow has a
    field to submit them from).
    """
    if not resolve_model_effort:
        resolved_harness = harness or (
            config.dispatch.default_harness if config is not None else "claude"
        )
        if resolved_harness not in VALID_HARNESSES:
            raise ValueError(
                f"Unknown harness: {resolved_harness!r}. "
                f"Must be one of: {', '.join(sorted(VALID_HARNESSES))}"
            )
        resolved_model, resolved_effort = model, effort
    elif config is not None:
        resolved_harness, resolved_model, resolved_effort = resolve_dispatch_selection(
            config, harness, model, effort
        )
    else:
        resolved_harness = harness or "claude"
        resolved_model = model
        resolved_effort = effort
        if harness is not None and harness not in VALID_HARNESSES:
            raise ValueError(
                f"Unknown harness: {harness!r}. "
                f"Must be one of: {', '.join(sorted(VALID_HARNESSES))}"
            )
        if effort is not None and effort not in VALID_EFFORTS:
            raise ValueError(
                f"Invalid effort: {effort!r}. Must be one of: {', '.join(sorted(VALID_EFFORTS))}"
            )
    if resolved_harness != "claude" and not supports_harness(caps, resolved_harness):
        raise ValueError(
            f"Harness {resolved_harness!r} is not supported by the installed launcher -- "
            "upgrade launch.sh or dispatch without a harness override."
        )
    return resolved_harness, resolved_model, resolved_effort


def dispatch_via_claude(
    batches: list[list[Issue]],
    config: Config,
    *,
    harness: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    capabilities_probe: Callable[[], LauncherCapabilities] | None = None,
) -> DispatchResult:
    """Dispatch wrapper that reports failures to the unified logger (#231).

    One site covers every consumer (web route, TUI screen, MCP tool).
    The benign "No issues to dispatch" result is not an error event.
    """
    issues = [f"{i.repo}#{i.number}" for batch in batches for i in batch]
    try:
        result = _dispatch_via_claude(
            batches,
            config,
            harness=harness,
            model=model,
            effort=effort,
            capabilities_probe=capabilities_probe,
        )
    except Exception as exc:
        reported = eventlog.emit(
            "error",
            "dispatch failed",
            {"issues": issues, "harness": harness, "error": repr(exc), "crashed": True},
        )
        # Callers with their own error boundary (mcp_server._guarded) must
        # not file this incident a second time (#237) — unless the dedupe
        # window swallowed this emit, in which case theirs is the only one.
        if reported:
            eventlog.mark_reported(exc)
        raise
    if not result.success and result.error != "No issues to dispatch":
        eventlog.emit(
            "error",
            "dispatch failed",
            {
                "issues": issues,
                "harness": harness,
                "error": result.error,
                "launched": result.launched,
            },
        )
    return result


def _dispatch_via_claude(
    batches: list[list[Issue]],
    config: Config,
    *,
    harness: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    capabilities_probe: Callable[[], LauncherCapabilities] | None = None,
) -> DispatchResult:
    """Dispatch one or more same-repo batches via a single /worktree-create.

    ``batches`` is a list of issue groups — each inner list is one worktree's
    worth of issues (slash-joined). Distinct batches become separate
    worktrees (space-joined) but share one Claude session and one
    /worktree-create command. All batches must share a repo.

    ``harness``/``model``/``effort`` (#205 Stage 1B) select the harness the
    dispatched worktree launches: an explicit argument here beats the
    matching ``config.dispatch.harnesses[resolved_harness]`` default, which
    in turn beats no override at all. The controller pane always runs
    ``claude`` -- ``/worktree-create`` is a claude slash command regardless
    of which harness it's told to launch -- only the prompt text carries the
    selection through to the worktree. ``model`` is a free pass-through
    string (unvalidated beyond the single-token check ``resolve_dispatch_selection``
    already applies).

    ``capabilities_probe`` (final-review fix, #205 Stage 1B) defaults to
    ``capabilities.probe_launcher_capabilities`` -- the real launch.sh probe.
    Callers with their own cached/injected probe (web ``app.state``, the TUI's
    ``ConfirmDispatchModal``) should pass it through so there's one probe path
    and tests stay deterministic. This is the single INNER gate: every caller
    of ``dispatch_via_claude`` -- including surfaces with no UI-layer gate of
    their own, like the inbox-dispatch background task -- is protected, not
    just the ones that happen to call ``resolve_and_validate_harness`` first.
    A dispatch that emits no ``--harness``/``--model``/``--effort`` flags at
    all (plain claude, no overrides) skips the probe entirely -- there's
    nothing to gate.
    """
    non_empty = [b for b in batches if b]
    if not non_empty:
        return DispatchResult(success=False, error="No issues to dispatch")

    first_issue = non_empty[0][0]
    repo_config = config.repos.get(first_issue.repo)
    if not repo_config:
        return DispatchResult(success=False, error=f"No config for repo: {first_issue.repo}")

    try:
        resolved_harness, resolved_model, resolved_effort = resolve_dispatch_selection(
            config, harness, model, effort
        )
    except ValueError as e:
        return DispatchResult(success=False, error=str(e))

    flag_args = get_adapter(resolved_harness).launch_args(resolved_model, resolved_effort)
    if flag_args:
        probe = (
            capabilities_probe if capabilities_probe is not None else probe_launcher_capabilities
        )
        caps = probe()
        if resolved_harness != "claude" and not supports_harness(caps, resolved_harness):
            return DispatchResult(
                success=False,
                error=(f"Harness {resolved_harness!r} is not advertised by the installed launcher"),
            )
        if resolved_model is not None and "--model" not in supported_flags(caps, resolved_harness):
            return DispatchResult(
                success=False,
                error=(
                    f"--model is not supported by the installed launcher for "
                    f"harness {resolved_harness!r}"
                ),
            )
        if resolved_effort is not None and "--effort" not in supported_flags(
            caps, resolved_harness
        ):
            return DispatchResult(
                success=False,
                error=(
                    f"--effort is not supported by the installed launcher for "
                    f"harness {resolved_harness!r}"
                ),
            )

    numbers = " ".join("/".join(str(i.number) for i in batch) for batch in non_empty)

    session_name = _find_repo_session(repo_config.local_path)
    if not session_name:
        session_name = _create_repo_session(repo_config)
        if not session_name:
            return DispatchResult(success=False, error="Failed to create tmux session")

    prompt = (
        "/worktree-create " + " ".join([*flag_args, numbers])
        if flag_args
        else f"/worktree-create {numbers}"
    )

    # Always split a fresh pane rather than reusing an existing Claude. Reuse
    # would send the second "claude" and /worktree-create into a busy REPL.
    pane_id, was_ready = _new_split_with_claude(
        session_name,
        repo_config.local_path,
        CONTROLLER_PERMISSION_MODE,
        initial_prompt=prompt,
        model=CONTROLLER_MODEL,
        effort=CONTROLLER_EFFORT,
    )
    if not pane_id:
        # split-window subdivides whatever pane is active in the repo's
        # session and fails with "no space for new pane" (rc=1) when that
        # pane is too small to halve. Fall back to a fresh full-screen
        # window, which can't run out of space (#153).
        pane_id, was_ready = _new_split_with_claude(
            session_name,
            repo_config.local_path,
            CONTROLLER_PERMISSION_MODE,
            new_window=True,
            initial_prompt=prompt,
            model=CONTROLLER_MODEL,
            effort=CONTROLLER_EFFORT,
        )
    if not pane_id:
        return DispatchResult(success=False, error="Could not create pane for Claude")

    if not was_ready:
        return DispatchResult(
            success=False,
            # launched=True: the prompt already rode in on claude's argv
            # (``_new_split_with_claude`` sends the launch line before
            # waiting), so a slow boot still runs /worktree-create. This is
            # "probably not, but maybe" — never safe to blindly retry.
            launched=True,
            error=(
                f"Claude did not start in pane {pane_id} within "
                f"{_CLAUDE_READY_TIMEOUT}s — /worktree-create may still run "
                "if it boots late; pane left open for inspection"
            ),
        )

    return DispatchResult(
        success=True,
        branch=f"(via Claude {prompt})",
    )


def _repo_session_name(local_path: Path) -> str:
    """tmux session name for a repo path: the dir name, leading dot sanitized.

    tmux uses ``.`` as a target delimiter, so a leading dot in a session name
    (e.g. for ``~/.claude``) is replaced with ``_`` (``.claude`` -> ``_claude``).
    Both session creation and the name-match lookup MUST go through this, or a
    dot-prefixed repo is created under one name and searched under another: its
    name-match fallback never fires, so once the session's ``session_path``
    drifts off the repo path, every dispatch re-creates a duplicate and fails.
    """
    name = local_path.name
    if name.startswith("."):
        name = "_" + name[1:]
    return name


def _find_repo_session(local_path: Path) -> str | None:
    """Find a tmux session matching the given repo path.

    Checks two strategies:
    1. Session path matches local_path (set at session creation)
    2. Session name matches the (sanitized) directory name (fallback for
       sessions whose reported path has drifted off the repo path)
    """
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_path}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
    except FileNotFoundError:
        return None

    resolved = local_path.resolve()
    target_name = _repo_session_name(local_path)
    name_match: str | None = None

    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        name, path_str = parts
        # Prefer exact path match
        if path_str and Path(path_str).resolve() == resolved:
            return name
        # Fall back to session name matching the sanitized directory name
        if name == target_name and name_match is None:
            name_match = name

    return name_match


def _create_repo_session(repo_config: RepoConfig) -> str | None:
    """Create a new tmux session for the given repo.

    Returns the session name on success, or None if creation failed.
    """
    name = _repo_session_name(repo_config.local_path)

    try:
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", str(repo_config.local_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
    except FileNotFoundError:
        return None
    return name


def _find_session_by_name(name: str) -> str | None:
    """Find a tmux session by exact session name.

    Distinct from ``_find_repo_session``: the ``scratch`` chat target
    addresses a single fixed session, not a per-repo one.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_path}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
    except FileNotFoundError:
        return None

    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if parts[0] == name:
            return name
    return None


def chat_launch_line(
    harness: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    prompt: str | None = None,
    name: str | None = None,
) -> str:
    """The shell line ``open_chat_window`` types into the new pane.

    Quoted with ``shlex`` plus one zsh fix: shlex treats ``=`` as safe, but
    zsh's EQUALS option expands an unquoted leading ``=word`` to a command
    path, so such words are force-quoted (#290 review).
    """
    adapter = get_adapter("codex" if harness == "codex" else "claude")
    argv = adapter.chat_command(model=model, effort=effort, prompt=prompt, name=name)
    return " ".join(
        "'" + arg.replace("'", "'\"'\"'") + "'" if arg.startswith("=") else shlex.quote(arg)
        for arg in argv
    )


def open_chat_window(
    *,
    existing_session: str | None,
    new_session_name: str,
    cwd: Path,
    harness: str = "claude",
    model: str | None = None,
    effort: str | None = None,
    prompt: str | None = None,
    name: str | None = None,
) -> str | None:
    """Open a fresh tmux window running a bare interactive harness REPL.

    Backs the web UI's "+ New session" button (#169). With an
    ``existing_session``, a new window is added to it; otherwise a detached
    session named ``new_session_name`` is created and its initial pane is
    used directly (adding a window to a brand-new session would leave a
    stray empty one). Unlike ``_new_split_with_claude`` there is no
    ready-wait: the caller jumps the user into the pane immediately and
    the harness boots in view.

    ``harness`` selects the bare command sent to the pane: ``"codex"`` sends
    the ``agent-hq codex --`` wrapper, anything else sends plain ``claude``
    (#205 Stage 1B) — no permission mode either way. ``model``/``effort``/
    ``prompt`` (#290, MCP ``open_chat_session`` only — the web button passes
    none) are appended by the harness adapter's ``chat_command`` and the line
    is shell-quoted; the prompt rides argv so the harness submits it at
    startup instead of racing keystrokes into a booting REPL (#183). ``name``
    (#429, the remote ``start_session`` only) is claude's ``--name``.

    Returns the pane id, or None if pane creation failed.
    """
    if existing_session is not None:
        cmd = ["tmux", "new-window", "-t", existing_session]
    else:
        cmd = ["tmux", "new-session", "-d", "-s", new_session_name]
    cmd += ["-c", str(cwd), "-P", "-F", "#{pane_id}"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip():
            return None
    except FileNotFoundError:
        return None

    pane_id = result.stdout.strip().splitlines()[0]
    cmd_str = chat_launch_line(harness, model=model, effort=effort, prompt=prompt, name=name)
    try:
        send = subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, cmd_str, "Enter"],
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if send.returncode != 0:
        return None
    return pane_id


_CLAUDE_READY_TIMEOUT = 15  # seconds to wait for the harness to become ready
_CLAUDE_POLL_INTERVAL = 1  # seconds between polls


def _wait_for_harness_process(pane_id: str, harness: str = "claude") -> bool:
    """Poll until a ``harness`` process is running in the pane's process tree.

    Readiness is process-based — the same detection the dashboard uses via
    discovery.py — rather than pane-content scraping: a prompt glyph also
    matches shell prompts (starship/p10k) and the echoed command, which made
    a failed launch look ready (#183). Returns False if no matching process
    appears within the timeout, or if ``harness`` isn't a registered adapter.
    """
    adapter = get_adapter(harness)
    if adapter is None:
        return False
    for _ in range(int(_CLAUDE_READY_TIMEOUT / _CLAUDE_POLL_INTERVAL)):
        time.sleep(_CLAUDE_POLL_INTERVAL)
        pane_pid = next(
            (p["pane_pid"] for p in get_all_panes() if p["pane_id"] == pane_id),
            None,
        )
        if pane_pid is None:
            continue
        children, commands = build_process_tree()
        if not commands:
            continue
        cmd, _agent_pid = find_agent_command(pane_pid, children, commands)
        if cmd is not None and adapter.matches_process(cmd):
            return True
    return False


def _new_split_with_claude(
    session_name: str,
    cwd: Path,
    permission_mode: str,
    *,
    new_window: bool = False,
    initial_prompt: str | None = None,
    harness: str = "claude",
    model: str | None = None,
    effort: str | None = None,
) -> tuple[str | None, bool]:
    """Spawn a new tmux pane and start the harness's REPL in it.

    ``cwd`` becomes the new pane's working directory. The launch line comes
    from ``adapters.get_adapter(harness).repl_command(permission_mode)`` —
    for claude this is ``claude --permission-mode <mode>`` (byte-identical to
    the pre-#205 hardcoded line when no ``model``/``effort`` is passed; see
    below); for codex it's the bare ``codex`` command
    (codex's REPL takes no permission-mode flag). Returns (pane_id,
    was_ready); pane_id is None if pane creation failed, and was_ready is
    False when the launch line could not be delivered or no matching harness
    process appeared within the timeout.

    ``new_window=True`` creates a fresh full-screen window in the session
    via ``tmux new-window``. Issue dispatch falls back to it when the
    repo session's active pane is too small to split ("no space for new
    pane", #153).

    ``initial_prompt`` is appended to the harness's argv (the worktree
    skill's own launcher does the same): if the harness never starts, the
    prompt can never be typed into a bare shell (#183).

    ``model``/``effort`` (#232) add ``--model``/``--effort`` to the claude
    launch line; issue dispatch pins them to ``CONTROLLER_MODEL`` /
    ``CONTROLLER_EFFORT``. Without them the claude line is the pre-#232 one.
    Codex ignores both (its REPL takes neither).
    """
    tmux_cmd = "new-window" if new_window else "split-window"
    try:
        result = subprocess.run(
            [
                "tmux",
                tmux_cmd,
                "-t",
                session_name,
                "-c",
                str(cwd),
                "-P",
                "-F",
                "#{pane_id}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None, False
    except FileNotFoundError:
        return None, False

    pane_id = result.stdout.strip().splitlines()[0]
    launch_cmd = shlex.join(
        get_adapter(harness).repl_command(permission_mode, model=model, effort=effort)
    )
    if initial_prompt is not None:
        # A MULTILINE initial_prompt survives this
        # plain send-keys only because shlex.quote wraps it in single quotes:
        # each embedded newline still arrives at the shell as its own Enter
        # keypress, but bash/zsh treat an unterminated single-quoted string
        # as a syntax error to keep completing, so they buffer every line
        # until the closing quote lands and then hand the harness argv the
        # full literal multi-line string intact (verified live on bash and
        # zsh, 2026-07-18), so no load-buffer/paste-buffer route is needed
        # to keep newlines from becoming Enter presses. An unusual shell without
        # quote-continuation would fragment the prompt into multiple
        # submissions instead — there's no in-band way to detect that here;
        # readiness only proves the harness process started, not that its
        # argv arrived whole.
        launch_cmd += f" {shlex.quote(initial_prompt)}"
    try:
        send = subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, launch_cmd, "Enter"],
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return pane_id, False
    if send.returncode != 0:
        return pane_id, False

    was_ready = _wait_for_harness_process(pane_id, harness)
    return pane_id, was_ready


def generate_branch_name(issues: list[Issue]) -> str:
    """Generate branch name from issues: feature/<numbers>-<slug>."""
    numbers = sorted(i.number for i in issues)
    nums_str = "-".join(str(n) for n in numbers)

    title = issues[0].title if issues else "work"
    slug = re.sub(r"[^a-z0-9\s]", "", title.lower())
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = slug[:40].rstrip("-")

    return f"feature/{nums_str}-{slug}"
