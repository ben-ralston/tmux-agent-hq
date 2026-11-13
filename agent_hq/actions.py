"""Actions to perform on agent panes."""

import subprocess
import sys
import time
from pathlib import Path

from .discovery import build_process_tree, find_agent_command
from .enrichment import get_agent_state, get_git_context, get_pane_cwd, get_repo_name
from .models import AgentState, AgentType
from .state import record_access


def jump_to_pane(pane_id: str) -> bool:
    """
    Switch tmux client to the specified pane, preserving zoom state.

    Uses switch-client -Z which natively preserves zoom across all cases:
    same-window, cross-session, and when target pane differs from zoomed pane.

    Also records the access time for priority sorting.

    Returns True if successful, False otherwise.
    """
    try:
        subprocess.run(
            ["tmux", "switch-client", "-Z", "-t", pane_id],
            check=True,
            capture_output=True,
        )
        # Ensure target pane is zoomed after switching.
        # switch-client -Z preserves zoom, but if neither source nor target
        # was zoomed, we still want to end up zoomed.
        zoom_check = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{window_zoomed_flag}"],
            capture_output=True,
            text=True,
        )
        if zoom_check.stdout.strip() != "1":
            subprocess.run(
                ["tmux", "resize-pane", "-Z", "-t", pane_id],
                capture_output=True,
            )
        record_access(pane_id)  # Track for priority sorting
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error: Could not jump to pane {pane_id}", file=sys.stderr)
        if e.stderr:
            stderr = e.stderr.decode().strip() if isinstance(e.stderr, bytes) else e.stderr.strip()
            print(f"  {stderr}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("Error: tmux not found", file=sys.stderr)
        return False


def kill_agent(pane_id: str) -> bool:
    """
    Kill the agent in the specified pane by sending Ctrl-C twice.

    Some agents (like Claude Code) require two Ctrl-C presses to exit,
    with a small delay between them.

    Returns True if successful, False otherwise.
    """
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "C-c"],
            check=True,
            capture_output=True,
        )
        time.sleep(0.1)
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "C-c"],
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error: Could not kill agent in pane {pane_id}", file=sys.stderr)
        if e.stderr:
            stderr = e.stderr.decode().strip() if isinstance(e.stderr, bytes) else e.stderr.strip()
            print(f"  {stderr}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("Error: tmux not found", file=sys.stderr)
        return False


def verify_agent_killed(
    pane_id: str,
    timeout: float = 3.0,
    interval: float = 0.3,
) -> tuple[bool, AgentType]:
    """
    Poll a pane until its agent process is gone or the timeout expires.

    Returns (died, remaining_agent_type): died is True once no agent
    command is found in the pane's process tree (also covers the pane
    itself disappearing). A failed inspection (ps error) never counts as
    death -- persistent failure times out with (False, AgentType.UNKNOWN).
    On timeout, remaining_agent_type reports what is still running.
    """
    deadline = time.monotonic() + timeout
    remaining = AgentType.UNKNOWN
    while True:
        info = _inspect_pane_agent(pane_id)
        if info is not None:
            agent_type, agent_pid = info
            if agent_type == AgentType.UNKNOWN or agent_pid is None:
                return True, AgentType.UNKNOWN
            remaining = agent_type
        if time.monotonic() >= deadline:
            return False, remaining
        time.sleep(interval)


def _inspect_pane_agent(pane_id: str) -> tuple[AgentType, int | None] | None:
    """
    Three-way pane inspection: (type, pid) when an agent is found,
    (AgentType.UNKNOWN, None) when the pane verifiably hosts no agent
    (including the pane being gone), or None when the process tree could
    not be read (ps failure) and no conclusion is possible.
    """
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_pid}"],
            capture_output=True,
            text=True,
            check=True,
        )
        pane_pid = int(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return AgentType.UNKNOWN, None

    children, commands = build_process_tree()
    if not commands:
        return None
    cmd, agent_pid = find_agent_command(pane_pid, children, commands)
    if cmd:
        return AgentType.from_command(cmd), agent_pid
    return AgentType.UNKNOWN, None


def _get_pane_agent_info(pane_id: str) -> tuple[AgentType, int | None]:
    """
    Get agent type and PID for the agent running in a pane.

    Looks up the pane's shell PID via tmux, then walks the process tree
    to find the agent command.

    Returns (agent_type, agent_pid) or (AgentType.UNKNOWN, None).
    """
    info = _inspect_pane_agent(pane_id)
    if info is None:
        return AgentType.UNKNOWN, None
    return info


def generate_session_name(pane_id: str) -> str:
    """
    Generate a descriptive session name from a pane's git context.

    Format: "repo-branch" (or just "repo" if branch is main/master).
    Falls back to cwd basename if not in a git repo.
    Sanitizes the name for tmux (replaces dots/colons, caps length).
    """
    cwd = get_pane_cwd(pane_id)
    repo_root, branch, _ = get_git_context(cwd)

    if repo_root:
        repo_name = get_repo_name(repo_root)
        if branch and branch not in ("main", "master"):
            name = f"{repo_name}-{branch}"
        else:
            name = repo_name
    elif cwd:
        name = Path(cwd).name or "unnamed"
    else:
        name = "unnamed"

    # Sanitize for tmux (dots and colons are delimiters)
    name = name.replace(".", "-").replace(":", "-")
    return name[:50]


def rename_session(pane_id: str, new_name: str | None = None) -> bool:
    """
    Rename the tmux session containing the given pane.

    If new_name is provided, renames directly via tmux.
    Otherwise, auto-renames:
    - Claude agents (idle): sends /rename to let Claude Code handle it
    - Claude agents (not idle): returns False with warning
    - Non-Claude agents: generates name from repo/branch context

    Returns True if successful, False otherwise.
    """
    try:
        if new_name:
            # Direct rename with explicit name
            session_result = subprocess.run(
                ["tmux", "display-message", "-t", pane_id, "-p", "#{session_name}"],
                capture_output=True,
                text=True,
                check=True,
            )
            current_session = session_result.stdout.strip()
            subprocess.run(
                ["tmux", "rename-session", "-t", current_session, new_name],
                check=True,
                capture_output=True,
            )
            return True

        # Auto-rename: detect agent type
        agent_type, agent_pid = _get_pane_agent_info(pane_id)

        if agent_type == AgentType.CLAUDE:
            # Check if Claude is idle (safe to send /rename)
            state = get_agent_state(pane_id, agent_type, agent_pid=agent_pid)
            if state != AgentState.IDLE:
                print(
                    f"Error: Claude agent in {pane_id} is {state.value}, "
                    "rename only works when idle",
                    file=sys.stderr,
                )
                return False

            # Send /rename to Claude Code
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, "/rename", "Enter"],
                check=True,
                capture_output=True,
            )
            return True
        else:
            # Non-Claude: generate name and rename via tmux
            session_result = subprocess.run(
                ["tmux", "display-message", "-t", pane_id, "-p", "#{session_name}"],
                capture_output=True,
                text=True,
                check=True,
            )
            current_session = session_result.stdout.strip()
            name = generate_session_name(pane_id)
            subprocess.run(
                ["tmux", "rename-session", "-t", current_session, name],
                check=True,
                capture_output=True,
            )
            return True

    except subprocess.CalledProcessError as e:
        print(f"Error: Could not rename session for pane {pane_id}", file=sys.stderr)
        if e.stderr:
            stderr = e.stderr.decode().strip() if isinstance(e.stderr, bytes) else e.stderr.strip()
            print(f"  {stderr}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("Error: tmux not found", file=sys.stderr)
        return False


def handle_fzf_result(result: str) -> bool:
    """
    Handle the result from fzf selection.

    Result is in format "action:pane_id" where action is 'jump' or 'kill'.

    Returns True if action was successful, False otherwise.
    """
    if ":" not in result:
        return False

    action, pane_id = result.split(":", 1)

    if action == "jump":
        return jump_to_pane(pane_id)
    elif action == "kill":
        return kill_agent(pane_id)
    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        return False
