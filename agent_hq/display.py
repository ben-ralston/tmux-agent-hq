"""Display formatting for fzf and terminal output."""

import json
import shutil
import subprocess
import sys

from .models import Agent, RepoGroup

# Width thresholds for display modes
WIDTH_WIDE = 100  # Wide layout - full column widths
WIDTH_NORMAL = 80  # Normal layout with side preview
WIDTH_MEDIUM = 70  # Medium layout - slightly narrower columns
WIDTH_COMPACT = 60  # Compact layout with bottom preview
HEIGHT_MIN_PREVIEW = 15  # Minimum lines to show bottom preview on narrow terminals

# ANSI codes for dimming inactive entries
ANSI_DIM = "\x1b[2m"
ANSI_RESET = "\x1b[0m"


def truncate(text: str, max_width: int) -> str:
    """Truncate text with ellipsis if it exceeds max_width."""
    if len(text) <= max_width:
        return text
    if max_width <= 3:
        return text[:max_width]
    return text[: max_width - 3] + "..."


def get_terminal_width() -> int:
    """Get the current terminal width in columns.

    Checks COLUMNS env var first (more reliable in subprocess/pipe contexts),
    then falls back to shutil detection.
    """
    import os

    # COLUMNS env var is more reliable when running as subprocess
    if cols := os.environ.get("COLUMNS"):
        try:
            return int(cols)
        except ValueError:
            pass
    return shutil.get_terminal_size().columns


def get_terminal_height() -> int:
    """Get the current terminal height in lines."""
    return shutil.get_terminal_size().lines


def format_agent_line(
    agent: Agent, repo_name: str, width: int = 100, inactive: bool = False
) -> str:
    """
    Format a single agent as a line for fzf selection.

    Column order: EMOJI | REPO | BRANCH | AGENT | PANE_ID
    Emoji is first so it's always visible even on narrow terminals.

    Uses tiered column widths based on terminal width thresholds to provide
    dynamic resizing while avoiding flash from small width variations.

    Args:
        agent: The agent to format
        repo_name: Name of the repository
        width: Terminal width for layout tier selection
        inactive: If True, dim the line with ANSI codes

    The pane_id is always extractable as the last whitespace-separated field.
    """
    # Use worktree name when in detached HEAD state for better identification
    if agent.branch == "HEAD" and agent.worktree_name:
        branch = agent.worktree_name
    else:
        branch = agent.branch or "—"
    state_emoji = agent.state.emoji
    agent_type = agent.agent_type.value
    session_name = agent.session_slug or ""

    # Fixed parts: emoji(2) + spaces(4) + agent(6) + pane_id(~4) + buffer(4) = 20
    FIXED_CHARS = 20

    if width < WIDTH_COMPACT:
        # Ultra-compact: combined repo/branch field, no session name column
        location_width = max(10, width - FIXED_CHARS + 1)  # +1 since no space between repo/branch
        location = f"{repo_name}/{branch}"
        location = truncate(location, location_width)
        line = f"{state_emoji} {location:<{location_width}} {agent_type:<6} {agent.pane_id}"
    elif not session_name:
        # Standard mode without session name: two capped columns
        available = width - FIXED_CHARS
        REPO_W = min(14, max(10, int(available * 0.40)))
        BRANCH_W = min(25, max(12, available - REPO_W))

        repo_display = truncate(repo_name, REPO_W)
        branch_display = truncate(branch, BRANCH_W)

        line = (
            f"{state_emoji} {repo_display:<{REPO_W}} {branch_display:<{BRANCH_W}} "
            f"{agent_type:<6} {agent.pane_id}"
        )
    else:
        # Standard mode with session name: three evenly-capped columns
        available = width - FIXED_CHARS
        col_w = max(8, available // 3)
        REPO_W = min(14, col_w)
        BRANCH_W = min(22, col_w)
        NAME_W = min(28, max(8, available - REPO_W - BRANCH_W))

        repo_display = truncate(repo_name, REPO_W)
        branch_display = truncate(branch, BRANCH_W)
        name_display = truncate(session_name, NAME_W)

        line = (
            f"{state_emoji} {repo_display:<{REPO_W}} {branch_display:<{BRANCH_W}} "
            f"{name_display:<{NAME_W}} {agent_type:<6} {agent.pane_id}"
        )

    if inactive:
        return f"{ANSI_DIM}{line}{ANSI_RESET}"
    return line


def format_for_fzf(groups: list[RepoGroup], width: int | None = None) -> str:
    """
    Format agent groups for fzf selection.

    Uses tiered column widths based on terminal width thresholds. This provides
    dynamic resizing at meaningful breakpoints while avoiding flash from small
    width variations between processes.

    Width tiers:
    - >= 100: Wide (full columns)
    - >= 80: Normal (standard columns)
    - >= 70: Medium (moderate columns)
    - >= 60: Narrow (smaller columns)
    - < 60: Compact (ultra-compact format)

    Args:
        groups: List of RepoGroup objects to format
        width: Terminal width for layout tier (auto-detected if None)
    """
    if width is None:
        width = get_terminal_width()

    lines = []

    for repo_group in groups:
        for worktree in repo_group.worktrees:
            for agent in worktree.agents:
                line = format_agent_line(agent, repo_group.repo_name, width=width)
                lines.append(line)

    return "\n".join(lines)


def format_agents_for_fzf(
    agents: list[Agent],
    width: int | None = None,
    inactive_pane_ids: set[str] | None = None,
) -> str:
    """
    Format a flat list of agents for fzf selection.

    Unlike format_for_fzf which takes grouped agents, this takes a pre-sorted
    flat list (e.g., sorted by priority) and formats each agent.

    If inactive_pane_ids is provided, inactive agents are dimmed and a blank
    line separator is added between active and inactive sections.

    Args:
        agents: List of Agent objects to format (already sorted)
        width: Terminal width for layout tier (auto-detected if None)
        inactive_pane_ids: Set of pane IDs that are marked inactive
    """
    if width is None:
        width = get_terminal_width()
    if inactive_pane_ids is None:
        inactive_pane_ids = set()

    active_lines = []
    inactive_lines = []

    for agent in agents:
        repo_name = agent.repo_name or agent.cwd.rsplit("/", 1)[-1] or "—"
        is_inactive = agent.pane_id in inactive_pane_ids
        line = format_agent_line(agent, repo_name, width=width, inactive=is_inactive)

        if is_inactive:
            inactive_lines.append(line)
        else:
            active_lines.append(line)

    # Join with blank line separator if both sections exist
    if active_lines and inactive_lines:
        return "\n".join(active_lines) + "\n\n" + "\n".join(inactive_lines)
    return "\n".join(active_lines + inactive_lines)


def format_tree(groups: list[RepoGroup]) -> str:
    """
    Format agent groups as a tree view.

    Example:
    my-project
      main (main)
        claude → %5
      feat-auth (feature/auth)
        claude → %8
    """
    lines = []

    for repo_group in groups:
        lines.append(f"{repo_group.repo_name}")

        for i, worktree in enumerate(repo_group.worktrees):
            is_last_wt = i == len(repo_group.worktrees) - 1
            wt_prefix = "└─" if is_last_wt else "├─"
            # For main worktree, just show branch; for linked worktrees show name (branch)
            if worktree.worktree_name:
                wt_display = f"{worktree.worktree_name} ({worktree.branch})"
            else:
                wt_display = worktree.branch or "main"
            lines.append(f"  {wt_prefix} {wt_display}")

            for j, agent in enumerate(worktree.agents):
                is_last_agent = j == len(worktree.agents) - 1
                if is_last_wt:
                    agent_prefix = "     └─" if is_last_agent else "     ├─"
                else:
                    agent_prefix = "  │  └─" if is_last_agent else "  │  ├─"
                state_emoji = agent.state.emoji
                agent_name = agent.agent_type.value
                lines.append(f"{agent_prefix} {state_emoji} {agent_name} → {agent.pane_id}")

        lines.append("")  # Blank line between repos

    return "\n".join(lines).rstrip()


def format_status_line(groups: list[RepoGroup]) -> str:
    """
    Format a one-liner summary for tmux status bar.

    Example: "Agents: 4 (my-project: 3, other-repo: 1)"
    """
    total = sum(rg.agent_count for rg in groups)
    if total == 0:
        return "Agents: 0"

    repo_counts = []
    for rg in groups:
        if rg.repo_name != "[ungrouped]":
            repo_counts.append(f"{rg.repo_name}: {rg.agent_count}")

    # Handle ungrouped
    ungrouped = next((rg for rg in groups if rg.repo_name == "[ungrouped]"), None)
    if ungrouped:
        repo_counts.append(f"other: {ungrouped.agent_count}")

    if repo_counts:
        return f"Agents: {total} ({', '.join(repo_counts)})"
    return f"Agents: {total}"


def format_json(agents: list[Agent]) -> str:
    """Format agents as JSON for scripting."""
    data = []
    for agent in agents:
        # Use the canonical serializer: it recursively converts nested
        # dataclasses *and* their enums (e.g. phase_snapshot's DevPhase
        # fields). asdict() left those enums as objects, crashing json.dumps.
        agent_dict = agent.to_dict()
        agent_dict["session_label"] = agent.session_label
        data.append(agent_dict)
    return json.dumps(data, indent=2)


def print_no_agents_message():
    """Print a friendly message when no agents are found."""
    print("No AI coding agents found running in tmux.", file=sys.stderr)
    print("", file=sys.stderr)
    print("Supported agents: claude, codex, aider, opencode, gemini, cursor, roo", file=sys.stderr)


def run_fzf(content: str, list_mode: bool = False) -> str | None:
    """
    Run fzf with the given content and return the selected pane_id.

    Auto-adjusts layout based on terminal width:
    - Wide (≥80): Side preview, full header
    - Medium (60-79): Bottom preview, shorter header
    - Narrow (<60): No preview, minimal header

    Args:
        content: Formatted content to pipe to fzf
        list_mode: If True, return the action prefix (jump: or kill:) with pane_id

    Returns:
        Selected pane_id or action:pane_id string, or None if cancelled.
    """
    width = get_terminal_width()
    height = get_terminal_height()

    # Kill command sends Ctrl-C twice with delay (needed for Claude Code)
    kill_cmd = "sh -c 'tmux send-keys -t {-1} C-c && sleep 0.1 && tmux send-keys -t {-1} C-c'"

    # Toggle inactive command
    toggle_cmd = "agent-hq --toggle-inactive {-1}"

    # Rename session command
    rename_cmd = "agent-hq --rename-session {-1}"

    # Calculate list width for reload commands (must pass explicitly for subprocess)
    # Use $COLUMNS for dynamic width detection in shell
    # Wide mode: side preview takes 50%, so list gets ~50% (use 45% to be safe)
    # Other modes: list gets full width
    if width >= WIDTH_NORMAL:
        reload_cmd = "agent-hq --list --width=$(($COLUMNS * 45 / 100))"
    else:
        reload_cmd = "agent-hq --list --width=$COLUMNS"

    # Base fzf options
    fzf_cmd = [
        "fzf",
        "--bind=enter:accept",
        f"--bind=ctrl-k:execute-silent({kill_cmd})+reload({reload_cmd})",
        f"--bind=ctrl-r:reload({reload_cmd})",
        f"--bind=resize:reload({reload_cmd})",
        f"--bind=tab:execute-silent({toggle_cmd})+reload({reload_cmd})",
        f"--bind=f2:execute-silent({rename_cmd})+reload({reload_cmd})",
        "--ansi",
        "--no-sort",
    ]

    # Adjust header and preview based on terminal dimensions
    if width >= WIDTH_NORMAL:
        # Wide: full header, side preview (50/50 split)
        fzf_cmd.extend(
            [
                "--header=Agent HQ | Enter=jump  Tab=mute  F2=rename  Ctrl-K=kill  Ctrl-R=refresh",
                "--preview=tmux capture-pane -e -t {-1} -p 2>/dev/null | tail -30",
                "--preview-window=right:50%",
            ]
        )
    elif width >= WIDTH_COMPACT:
        # Medium: shorter header, bottom preview
        fzf_cmd.extend(
            [
                "--header=Enter=jump Tab=mute F2=rename ^K=kill ^R=refresh",
                "--preview=tmux capture-pane -e -t {-1} -p 2>/dev/null | tail -15",
                "--preview-window=bottom:50%",
            ]
        )
    elif height >= HEIGHT_MIN_PREVIEW:
        # Narrow but tall (e.g., phone portrait): bottom preview
        fzf_cmd.extend(
            [
                "--header=Tab=mute ^K=kill",
                "--preview=tmux capture-pane -e -t {-1} -p 2>/dev/null | tail -10",
                "--preview-window=bottom:40%",
            ]
        )
    else:
        # Narrow and short: no preview
        fzf_cmd.extend(
            [
                "--header=Tab=mute ^K=kill",
            ]
        )

    try:
        result = subprocess.run(
            fzf_cmd,
            input=content,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            output = result.stdout.strip()
            if output:
                # Extract pane_id from the last field of the selected line
                pane_id = output.split()[-1]
                return f"jump:{pane_id}"
        # returncode 130 means user pressed Esc/Ctrl-C
        return None

    except FileNotFoundError:
        print("Error: fzf is not installed. Please install fzf.", file=sys.stderr)
        print("  brew install fzf  # macOS", file=sys.stderr)
        print("  apt install fzf   # Debian/Ubuntu", file=sys.stderr)
        return None
    except subprocess.CalledProcessError:
        return None
