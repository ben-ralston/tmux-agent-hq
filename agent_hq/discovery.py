"""Agent discovery via tmux and process tree walking."""

import os
import subprocess
from collections import defaultdict

# Known AI coding agent command patterns
AGENT_COMMANDS = frozenset(
    [
        "claude",
        "codex",
        "aider",
        "opencode",
        "gemini",
        "cursor",
        "roo",
    ]
)


def is_in_tmux() -> bool:
    """Check if we're running inside a tmux session."""
    return bool(os.environ.get("TMUX"))


def get_all_panes() -> list[dict]:
    """
    Get all tmux panes with their PIDs.

    Returns list of dicts with keys:
        session_name, window_index, pane_index, pane_id, pane_pid
    """
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_id}\t#{pane_pid}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return []
    except FileNotFoundError:
        return []

    panes = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        panes.append(
            {
                "session_name": parts[0],
                "window_index": int(parts[1]),
                "pane_index": int(parts[2]),
                "pane_id": parts[3],
                "pane_pid": int(parts[4]),
            }
        )
    return panes


def build_process_tree() -> tuple[dict[int, list[int]], dict[int, str]]:
    """
    Build a process tree from ps output.

    Returns:
        children: dict mapping pid -> list of child pids
        commands: dict mapping pid -> command name
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "pid,ppid,comm", "-ax"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return {}, {}
    except FileNotFoundError:
        return {}, {}

    children: dict[int, list[int]] = defaultdict(list)
    commands: dict[int, str] = {}

    for line in result.stdout.strip().split("\n")[1:]:  # Skip header
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            # Command may have spaces, but we only need the first word
            cmd = parts[2]
            children[ppid].append(pid)
            commands[pid] = cmd
        except ValueError:
            continue

    return children, commands


def find_agent_command(
    pid: int, children: dict[int, list[int]], commands: dict[int, str]
) -> tuple[str | None, int | None]:
    """
    Walk the process tree from pid to find an agent command.

    Searches the process and all its descendants for a matching agent command.
    Returns (command, agent_pid) tuple, or (None, None) if not found.
    """
    visited = set()
    stack = [pid]

    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)

        cmd = commands.get(current)
        if cmd:
            # Check if this command matches any known agent
            cmd_base = cmd.lower().split("/")[-1]  # Handle full paths
            for agent_cmd in AGENT_COMMANDS:
                if agent_cmd in cmd_base:
                    return cmd, current

        # Add children to stack
        stack.extend(children.get(current, []))

    return None, None


def discover_agents(filter_commands: list[str] | None = None) -> list[dict]:
    """
    Discover all running AI coding agents across tmux panes.

    Args:
        filter_commands: Optional list of command names to filter by.
                        If None, discovers all known agent types.

    Returns:
        List of dicts with pane info and detected agent command.
        Each dict has keys:
            session_name, window_index, pane_index, pane_id, pane_pid, command
    """
    panes = get_all_panes()
    if not panes:
        return []

    children, commands = build_process_tree()
    if not commands:
        return []

    # Determine which commands to look for
    target_commands = set(filter_commands) if filter_commands else AGENT_COMMANDS

    from .codex_runtime import _alive, read_registrations

    # Process-generation failures invalidate status in the collector, not the
    # pane identity. Independently verify the frontend belongs to this pane below.
    registrations = read_registrations(alive=_alive)

    agents = []
    for pane in panes:
        agent_cmd, agent_pid = find_agent_command(pane["pane_pid"], children, commands)
        # The wrapper owns two Codex children. Only its registered frontend
        # represents the visible session, regardless of process-tree order.
        receipts = [r for r in registrations if r["pane_id"] == pane["pane_id"]]
        if len(receipts) == 1:
            frontend = receipts[0]["frontend_pid"]
            stack, descendants = [pane["pane_pid"]], set()
            while stack:
                current = stack.pop()
                if current in descendants:
                    continue
                descendants.add(current)
                stack.extend(children.get(current, []))
            if frontend in descendants and "codex" in commands.get(frontend, "").lower():
                agent_cmd, agent_pid = commands[frontend], frontend
        if agent_cmd:
            cmd_base = agent_cmd.lower().split("/")[-1]
            # Check if this matches our target commands
            for target in target_commands:
                if target.lower() in cmd_base:
                    agents.append(
                        {
                            **pane,
                            "command": agent_cmd,
                            "agent_pid": agent_pid,
                        }
                    )
                    break

    return agents
