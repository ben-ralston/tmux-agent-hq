"""Group agents by repository and worktree."""

from collections import defaultdict

from .models import Agent, AgentState, RepoGroup, WorktreeGroup


def group_by_worktree(agents: list[Agent]) -> list[RepoGroup]:
    """
    Group agents hierarchically by repository and worktree.

    Structure:
    - RepoGroup (by repo_root)
      - WorktreeGroup (by worktree path or main)
        - Agents

    Agents not in git repos go in an "Ungrouped" pseudo-repo.

    Returns:
        List of RepoGroup objects, sorted by repo name.
        The "Ungrouped" group (if present) is placed last.
    """
    # Group agents by repo_root
    repo_agents: dict[str | None, list[Agent]] = defaultdict(list)
    for agent in agents:
        repo_agents[agent.repo_root].append(agent)

    repo_groups = []

    # Process git repos first
    for repo_root, repo_agents_list in repo_agents.items():
        if repo_root is None:
            continue  # Handle ungrouped agents separately

        # Use repo_name from agent (from git remote), fallback to directory basename
        repo_name = repo_agents_list[0].repo_name or repo_root.rsplit("/", 1)[-1]

        # Group by worktree within this repo
        worktree_agents: dict[str | None, list[Agent]] = defaultdict(list)
        for agent in repo_agents_list:
            worktree_agents[agent.worktree_name].append(agent)

        worktree_groups = []

        # Main worktree first (worktree_name is None)
        if None in worktree_agents:
            main_agents = worktree_agents[None]
            branch = main_agents[0].branch or "main"
            worktree_groups.append(
                WorktreeGroup(
                    repo_name=repo_name,
                    repo_root=repo_root,
                    worktree_path=repo_root,
                    worktree_name=None,  # None indicates main worktree
                    branch=branch,
                    agents=main_agents,
                )
            )

        # Then other worktrees, sorted by name
        for wt_name in sorted(k for k in worktree_agents if k is not None):
            wt_agents = worktree_agents[wt_name]
            branch = wt_agents[0].branch or wt_name
            # Construct worktree path - it's typically a sibling of the main repo
            # with the worktree name as directory name
            worktree_groups.append(
                WorktreeGroup(
                    repo_name=repo_name,
                    repo_root=repo_root,
                    worktree_path=wt_agents[0].cwd,  # Use agent's cwd as approximation
                    worktree_name=wt_name,
                    branch=branch,
                    agents=wt_agents,
                )
            )

        repo_groups.append(
            RepoGroup(
                repo_name=repo_name,
                repo_root=repo_root,
                worktrees=worktree_groups,
            )
        )

    # Sort repo groups by name
    repo_groups.sort(key=lambda rg: rg.repo_name.lower())

    # Handle ungrouped agents (not in git repos)
    ungrouped = repo_agents.get(None, [])
    if ungrouped:
        # Create a pseudo-repo for ungrouped agents
        ungrouped_group = RepoGroup(
            repo_name="[ungrouped]",
            repo_root="",
            worktrees=[
                WorktreeGroup(
                    repo_name="[ungrouped]",
                    repo_root="",
                    worktree_path="",
                    worktree_name="—",
                    branch="—",
                    agents=ungrouped,
                )
            ],
        )
        repo_groups.append(ungrouped_group)

    return repo_groups


def filter_agents(
    agents: list[Agent],
    agent_type: str | None = None,
    repo: str | None = None,
) -> list[Agent]:
    """
    Filter agents by type and/or repository.

    Args:
        agents: List of agents to filter
        agent_type: Filter by agent type (e.g., "claude", "aider")
        repo: Filter by repository name (partial match)

    Returns:
        Filtered list of agents
    """
    result = agents

    if agent_type:
        agent_type_lower = agent_type.lower()
        result = [a for a in result if a.agent_type.value == agent_type_lower]

    if repo:
        repo_lower = repo.lower()
        result = [
            a for a in result
            if (a.repo_name and repo_lower in a.repo_name.lower())
            or (a.repo_root and repo_lower in a.repo_root.lower())
        ]

    return result


def sort_agents_by_priority(
    agents: list[Agent],
    access_times: dict[str, float],
    inactive_pane_ids: set[str] | None = None,
) -> list[Agent]:
    """
    Sort agents by priority for display in fzf.

    Agents are sorted so that the most important ones appear at the TOP
    of the list (where fzf cursor starts), making them faster to reach.

    Sort order (top to bottom):
    1. WAITING agents (highest priority - at very top)
    2. Most recently accessed
    3. Less recently accessed
    4. Never accessed
    5. Inactive agents (lowest priority - at bottom)

    Args:
        agents: List of agents to sort
        access_times: Dict mapping pane_id to Unix timestamp of last access
        inactive_pane_ids: Set of pane IDs marked as inactive

    Returns:
        Sorted list of agents
    """
    if inactive_pane_ids is None:
        inactive_pane_ids = set()

    def priority(agent: Agent) -> tuple[int, int, float]:
        # Inactive agents get sorted to bottom (0 = active, 1 = inactive)
        is_inactive = 1 if agent.pane_id in inactive_pane_ids else 0
        # WAITING agents get priority 1, others 0
        is_waiting = 1 if agent.state == AgentState.WAITING else 0
        # More recent = higher timestamp
        last_access = access_times.get(agent.pane_id, 0.0)
        # Sort by: inactive (0 before 1), then waiting (1 before 0), then access time
        return (is_inactive, -is_waiting, -last_access)

    return sorted(agents, key=priority)
