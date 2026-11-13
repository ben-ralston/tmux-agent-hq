"""Tests for agent grouping module."""

from agent_hq.grouping import filter_agents, group_by_worktree, sort_agents_by_priority
from agent_hq.models import Agent, AgentState, AgentType


def make_agent(
    pane_id: str = "%1",
    agent_type: AgentType = AgentType.CLAUDE,
    repo_root: str | None = "/project",
    branch: str | None = "main",
    worktree_name: str | None = None,
    state: AgentState = AgentState.UNKNOWN,
) -> Agent:
    """Helper to create test agents."""
    return Agent(
        pane_id=pane_id,
        session_name="test",
        window_index=1,
        pane_index=0,
        agent_type=agent_type,
        cwd=repo_root or "/tmp",
        repo_root=repo_root,
        branch=branch,
        worktree_name=worktree_name,
        state=state,
    )


class TestGroupByWorktree:
    """Tests for group_by_worktree function."""

    def test_groups_single_repo(self):
        """Should group agents from single repo."""
        agents = [
            make_agent("%1", repo_root="/project"),
            make_agent("%2", repo_root="/project"),
        ]

        groups = group_by_worktree(agents)

        assert len(groups) == 1
        assert groups[0].repo_name == "project"
        assert len(groups[0].worktrees) == 1
        assert len(groups[0].worktrees[0].agents) == 2

    def test_groups_multiple_repos(self):
        """Should group agents from multiple repos."""
        agents = [
            make_agent("%1", repo_root="/project-a"),
            make_agent("%2", repo_root="/project-b"),
        ]

        groups = group_by_worktree(agents)

        assert len(groups) == 2
        assert {g.repo_name for g in groups} == {"project-a", "project-b"}

    def test_groups_worktrees_within_repo(self):
        """Should group agents by worktree within a repo."""
        agents = [
            make_agent("%1", repo_root="/project", worktree_name=None),
            make_agent("%2", repo_root="/project", worktree_name="feature-a"),
            make_agent("%3", repo_root="/project", worktree_name="feature-b"),
        ]

        groups = group_by_worktree(agents)

        assert len(groups) == 1
        assert len(groups[0].worktrees) == 3

        wt_names = [wt.worktree_name for wt in groups[0].worktrees]
        # Main worktree should be first (None indicates main)
        assert wt_names[0] is None
        # Others should be sorted
        assert wt_names[1:] == ["feature-a", "feature-b"]

    def test_handles_ungrouped_agents(self):
        """Should put agents without repo in ungrouped section."""
        agents = [
            make_agent("%1", repo_root="/project"),
            make_agent("%2", repo_root=None),
        ]

        groups = group_by_worktree(agents)

        assert len(groups) == 2
        # Ungrouped should be last
        assert groups[-1].repo_name == "[ungrouped]"
        assert len(groups[-1].worktrees[0].agents) == 1

    def test_sorts_repos_by_name(self):
        """Should sort repo groups alphabetically."""
        agents = [
            make_agent("%1", repo_root="/zebra"),
            make_agent("%2", repo_root="/alpha"),
        ]

        groups = group_by_worktree(agents)

        assert groups[0].repo_name == "alpha"
        assert groups[1].repo_name == "zebra"

    def test_empty_agents_list(self):
        """Should return empty list for no agents."""
        groups = group_by_worktree([])

        assert groups == []


class TestFilterAgents:
    """Tests for filter_agents function."""

    def test_filters_by_agent_type(self):
        """Should filter by agent type."""
        agents = [
            make_agent("%1", agent_type=AgentType.CLAUDE),
            make_agent("%2", agent_type=AgentType.AIDER),
        ]

        result = filter_agents(agents, agent_type="claude")

        assert len(result) == 1
        assert result[0].agent_type == AgentType.CLAUDE

    def test_filters_by_repo(self):
        """Should filter by repo name (partial match)."""
        agents = [
            make_agent("%1", repo_root="/Users/test/my-project"),
            make_agent("%2", repo_root="/Users/test/other"),
        ]

        result = filter_agents(agents, repo="project")

        assert len(result) == 1
        assert "project" in result[0].repo_root

    def test_filters_by_both(self):
        """Should filter by both agent type and repo."""
        agents = [
            make_agent("%1", agent_type=AgentType.CLAUDE, repo_root="/project"),
            make_agent("%2", agent_type=AgentType.AIDER, repo_root="/project"),
            make_agent("%3", agent_type=AgentType.CLAUDE, repo_root="/other"),
        ]

        result = filter_agents(agents, agent_type="claude", repo="project")

        assert len(result) == 1
        assert result[0].pane_id == "%1"

    def test_no_filters(self):
        """Should return all agents when no filters specified."""
        agents = [
            make_agent("%1"),
            make_agent("%2"),
        ]

        result = filter_agents(agents)

        assert len(result) == 2

    def test_case_insensitive_agent_type(self):
        """Agent type filter should be case insensitive."""
        agents = [make_agent("%1", agent_type=AgentType.CLAUDE)]

        result = filter_agents(agents, agent_type="CLAUDE")

        assert len(result) == 1


class TestSortAgentsByPriority:
    """Tests for sort_agents_by_priority function."""

    def test_sorts_by_access_time(self):
        """Should sort by access time (most recent at top)."""
        agents = [
            make_agent("%1"),
            make_agent("%2"),
            make_agent("%3"),
        ]
        access_times = {"%1": 100.0, "%2": 300.0, "%3": 200.0}

        result = sort_agents_by_priority(agents, access_times)

        # Order: %2 (300), %3 (200), %1 (100) - most recent at top
        assert [a.pane_id for a in result] == ["%2", "%3", "%1"]

    def test_never_accessed_at_bottom(self):
        """Should put never-accessed agents at the bottom."""
        agents = [
            make_agent("%1"),
            make_agent("%2"),
            make_agent("%3"),
        ]
        access_times = {"%2": 100.0}  # Only %2 has been accessed

        result = sort_agents_by_priority(agents, access_times)

        # %1 and %3 have access time 0, %2 has 100
        # Recently accessed should be at top, never accessed at bottom
        assert result[0].pane_id == "%2"

    def test_waiting_at_top(self):
        """Should put WAITING agents at the very top."""
        agents = [
            make_agent("%1", state=AgentState.RUNNING),
            make_agent("%2", state=AgentState.WAITING),
            make_agent("%3", state=AgentState.IDLE),
        ]
        access_times = {"%1": 300.0, "%2": 100.0, "%3": 200.0}

        result = sort_agents_by_priority(agents, access_times)

        # WAITING should be at top regardless of access time
        assert result[0].pane_id == "%2"
        assert result[0].state == AgentState.WAITING

    def test_waiting_sorted_by_access_among_themselves(self):
        """Multiple WAITING agents should sort by access time among themselves."""
        agents = [
            make_agent("%1", state=AgentState.WAITING),
            make_agent("%2", state=AgentState.WAITING),
            make_agent("%3", state=AgentState.IDLE),
        ]
        access_times = {"%1": 100.0, "%2": 200.0, "%3": 300.0}

        result = sort_agents_by_priority(agents, access_times)

        # Sort key is (is_waiting, last_access), descending
        # %1: (1, 100), %2: (1, 200), %3: (0, 300)
        # Descending: (1, 200), (1, 100), (0, 300)
        # So: %2, %1, %3 - WAITING at top, most recent first within each group
        assert [a.pane_id for a in result] == ["%2", "%1", "%3"]

    def test_empty_access_times(self):
        """Should handle empty access times dict."""
        agents = [
            make_agent("%1"),
            make_agent("%2"),
        ]

        result = sort_agents_by_priority(agents, {})

        # All have 0 access time, order is stable
        assert len(result) == 2

    def test_empty_agents(self):
        """Should handle empty agents list."""
        result = sort_agents_by_priority([], {})

        assert result == []

    def test_inactive_at_bottom(self):
        """Should put inactive agents at the bottom."""
        agents = [
            make_agent("%1", state=AgentState.RUNNING),
            make_agent("%2", state=AgentState.IDLE),
            make_agent("%3", state=AgentState.IDLE),
        ]
        access_times = {"%1": 100.0, "%2": 200.0, "%3": 300.0}
        inactive_pane_ids = {"%3"}  # %3 is inactive

        result = sort_agents_by_priority(agents, access_times, inactive_pane_ids)

        # %3 should be at bottom despite having highest access time
        assert result[-1].pane_id == "%3"
        # Active agents sorted by access time
        assert result[0].pane_id == "%2"
        assert result[1].pane_id == "%1"

    def test_waiting_still_tops_inactive(self):
        """WAITING agents should be at top even if others are inactive."""
        agents = [
            make_agent("%1", state=AgentState.WAITING),
            make_agent("%2", state=AgentState.IDLE),
            make_agent("%3", state=AgentState.IDLE),
        ]
        access_times = {"%1": 100.0, "%2": 200.0, "%3": 300.0}
        inactive_pane_ids = {"%2"}  # %2 is inactive

        result = sort_agents_by_priority(agents, access_times, inactive_pane_ids)

        # WAITING at top, inactive at bottom
        assert result[0].pane_id == "%1"
        assert result[-1].pane_id == "%2"
