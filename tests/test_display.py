"""Tests for display formatting module."""

from agent_hq.display import (
    ANSI_DIM,
    ANSI_RESET,
    format_agent_line,
    format_agents_for_fzf,
    format_for_fzf,
    format_json,
    format_status_line,
    format_tree,
    truncate,
)
from agent_hq.models import (
    Agent,
    AgentType,
    DevPhase,
    InputsObserved,
    PhaseSnapshot,
    RepoGroup,
    WorktreeGroup,
)


def make_agent(
    pane_id: str = "%1",
    agent_type: AgentType = AgentType.CLAUDE,
    branch: str | None = "main",
    worktree_name: str | None = None,
    repo_name: str | None = "project",
) -> Agent:
    """Helper to create test agents."""
    return Agent(
        pane_id=pane_id,
        session_name="test",
        window_index=1,
        pane_index=0,
        agent_type=agent_type,
        cwd="/project",
        repo_root="/project",
        repo_name=repo_name,
        branch=branch,
        worktree_name=worktree_name,
    )


def make_repo_group(
    repo_name: str = "project",
    worktrees: list[tuple[str, list[Agent]]] | None = None,
) -> RepoGroup:
    """Helper to create test repo groups."""
    if worktrees is None:
        worktrees = [("main", [make_agent()])]

    wt_groups = []
    for wt_name, agents in worktrees:
        wt_groups.append(
            WorktreeGroup(
                repo_name=repo_name,
                repo_root=f"/{repo_name}",
                worktree_path=f"/{repo_name}" if wt_name == "main" else f"/{repo_name}-{wt_name}",
                worktree_name=wt_name,
                branch=agents[0].branch if agents else "main",
                agents=agents,
            )
        )

    return RepoGroup(
        repo_name=repo_name,
        repo_root=f"/{repo_name}",
        worktrees=wt_groups,
    )


class TestTruncate:
    """Tests for truncate function."""

    def test_returns_text_when_fits(self):
        """Should return text unchanged when it fits."""
        assert truncate("hello", 10) == "hello"

    def test_truncates_with_ellipsis(self):
        """Should truncate with ellipsis when text is too long."""
        assert truncate("hello world", 8) == "hello..."

    def test_handles_exact_length(self):
        """Should return text when exactly at max width."""
        assert truncate("hello", 5) == "hello"

    def test_handles_very_small_width(self):
        """Should handle very small max_width gracefully."""
        assert truncate("hello", 3) == "hel"
        assert truncate("hello", 2) == "he"


class TestFormatAgentLine:
    """Tests for format_agent_line function."""

    def test_formats_basic_line(self):
        """Should format agent as a single line with repo and branch."""
        agent = make_agent("%5")

        line = format_agent_line(agent, "project")

        assert "project" in line  # Repo name
        assert "main" in line  # Branch column
        assert "claude" in line
        assert "%5" in line

    def test_emoji_comes_first(self):
        """Emoji should be the first character in the line."""
        agent = make_agent("%5")

        line = format_agent_line(agent, "project")

        # First character should be the state emoji
        assert line[0] == agent.state.emoji

    def test_formats_line_with_feature_branch(self):
        """Should show feature branch in branch column."""
        agent = make_agent("%5", branch="feature/auth")

        line = format_agent_line(agent, "project")

        assert "project" in line
        assert "feature/auth" in line
        assert "claude" in line
        assert "%5" in line

    def test_truncates_long_repo_name(self):
        """Should truncate long repo name that exceeds column width."""
        agent = make_agent("%5")

        # At width=60, repo gets ~16 chars (40% of 40 available)
        # This 30-char name should be truncated
        line = format_agent_line(agent, "very-long-repository-name-here", width=60)

        assert "..." in line
        assert "%5" in line

    def test_pane_id_is_last(self):
        """Pane ID should be the last field for fzf extraction."""
        agent = make_agent("%99")

        line = format_agent_line(agent, "project")

        assert line.strip().endswith("%99")

    def test_compact_mode(self):
        """Should format compact line for narrow terminals."""
        agent = make_agent("%5")

        # Width < 60 triggers compact mode
        line = format_agent_line(agent, "project", width=50)

        # Compact format is much shorter
        assert len(line) < 50
        assert "claude" in line
        assert "%5" in line
        assert "project" in line

    def test_compact_mode_truncates_long_names(self):
        """Should truncate long repo/branch names in compact mode."""
        agent = make_agent("%5", branch="very-long-feature-branch-name")

        # Width < 60 triggers compact mode
        line = format_agent_line(agent, "very-long-repository-name", width=50)

        # Should be truncated with ...
        assert "..." in line
        assert line.strip().endswith("%5")

    def test_tiered_widths(self):
        """Columns should cap at max width rather than growing with terminal."""
        agent = make_agent("%5", branch="feature/long-branch-name")

        normal_line = format_agent_line(agent, "my-long-project-name", width=90)
        wide_line = format_agent_line(agent, "my-long-project-name", width=150)

        # With caps, wide terminal should not produce longer lines than normal
        assert len(wide_line) == len(normal_line)

        # But narrow should be shorter since columns haven't reached caps
        compact_line = format_agent_line(agent, "my-long-project-name", width=50)
        assert len(compact_line) < len(normal_line)

    def test_detached_head_shows_worktree_name(self):
        """Should show worktree name instead of HEAD when in detached state."""
        agent = make_agent("%5", branch="HEAD", worktree_name="feature-auth")

        line = format_agent_line(agent, "project")

        assert "feature-auth" in line
        assert "HEAD" not in line

    def test_detached_head_without_worktree_shows_head(self):
        """Should show HEAD when detached and no worktree name available."""
        agent = make_agent("%5", branch="HEAD")

        line = format_agent_line(agent, "project")

        assert "HEAD" in line


class TestFormatForFzf:
    """Tests for format_for_fzf function."""

    def test_formats_single_agent(self):
        """Should format single agent group."""
        groups = [make_repo_group()]

        output = format_for_fzf(groups)

        assert "%1" in output
        assert "project" in output

    def test_uses_compact_for_narrow_width(self):
        """Should use compact format when width is below threshold."""
        groups = [make_repo_group()]

        # Force narrow width
        output = format_for_fzf(groups, width=50)

        # Compact format is shorter
        lines = output.strip().split("\n")
        assert all(len(line) < 60 for line in lines)

    def test_does_not_use_compact_for_medium_widths(self):
        """Should NOT use compact format when width is >= 60."""
        groups = [make_repo_group()]

        # Test various widths that should NOT trigger compact mode
        for width in [60, 70, 80, 90, 100, 110, 120]:
            output = format_for_fzf(groups, width=width)
            lines = output.strip().split("\n")

            # Normal/tiered formats have columns lined up with padding
            # Compact format uses "/" between repo and branch
            for line in lines:
                # Check that we're not using compact format (which has repo/branch)
                assert "/" not in line.split()[1:3], (
                    f"Width {width} incorrectly used compact format"
                )

    def test_formats_multiple_agents(self):
        """Should format multiple agents with newlines."""
        groups = [
            make_repo_group(
                worktrees=[
                    ("main", [make_agent("%1"), make_agent("%2")]),
                ]
            ),
        ]

        output = format_for_fzf(groups)
        lines = output.strip().split("\n")

        assert len(lines) == 2

    def test_empty_groups(self):
        """Should return empty string for no groups."""
        output = format_for_fzf([])

        assert output == ""


class TestFormatTree:
    """Tests for format_tree function."""

    def test_formats_tree_structure(self):
        """Should format agents in tree structure."""
        groups = [make_repo_group()]

        output = format_tree(groups)

        assert "project" in output
        assert "main" in output
        assert "claude" in output
        assert "→" in output

    def test_multiple_worktrees(self):
        """Should show multiple worktrees with tree characters."""
        groups = [
            make_repo_group(
                worktrees=[
                    ("main", [make_agent("%1")]),
                    ("feature", [make_agent("%2")]),
                ]
            ),
        ]

        output = format_tree(groups)

        assert "├─" in output or "└─" in output
        assert "main" in output
        assert "feature" in output


class TestFormatStatusLine:
    """Tests for format_status_line function."""

    def test_formats_status(self):
        """Should format one-liner status."""
        groups = [make_repo_group()]

        output = format_status_line(groups)

        assert output.startswith("Agents:")
        assert "1" in output
        assert "project" in output

    def test_empty_agents(self):
        """Should show zero for no agents."""
        output = format_status_line([])

        assert output == "Agents: 0"

    def test_multiple_repos(self):
        """Should show counts per repo."""
        groups = [
            make_repo_group("project-a"),
            make_repo_group("project-b"),
        ]

        output = format_status_line(groups)

        assert "Agents: 2" in output
        assert "project-a: 1" in output
        assert "project-b: 1" in output


class TestFormatJson:
    """Tests for format_json function."""

    def test_formats_as_json(self):
        """Should format agents as valid JSON."""
        import json

        agents = [make_agent("%1"), make_agent("%2")]

        output = format_json(agents)
        parsed = json.loads(output)

        assert len(parsed) == 2
        assert parsed[0]["pane_id"] == "%1"
        assert parsed[0]["agent_type"] == "claude"

    def test_empty_list(self):
        """Should format empty list as empty JSON array."""
        import json

        output = format_json([])
        parsed = json.loads(output)

        assert parsed == []

    def test_serializes_agent_with_phase_snapshot(self):
        """Should JSON-serialize the nested phase_snapshot, including its
        DevPhase enums (regression: asdict() left DevPhase objects in place,
        crashing json.dumps)."""
        import json

        agent = make_agent("%1")
        agent.phase_snapshot = PhaseSnapshot(
            detected_phase=DevPhase.IMPLEMENT,
            effective_phase=DevPhase.QA,
            phase_source="transcript",
            phase_session_id="sess-123",
            feedback=None,
            inputs_observed=InputsObserved(overrides_revision=0, hook_state_revision=0),
            producer="daemon",
            producer_epoch=0.0,
            sequence=0,
            monotonic_ns=0,
            timestamp=0.0,
        )

        output = format_json([agent])
        parsed = json.loads(output)

        snap = parsed[0]["phase_snapshot"]
        assert snap["detected_phase"] == "implement"
        assert snap["effective_phase"] == "qa"


class TestInactiveFormatting:
    """Tests for inactive agent formatting."""

    def test_format_agent_line_dims_inactive(self):
        """Should wrap inactive agent lines with ANSI dim codes."""
        agent = make_agent("%5")

        line = format_agent_line(agent, "project", inactive=True)

        assert line.startswith(ANSI_DIM)
        assert line.endswith(ANSI_RESET)
        assert "%5" in line

    def test_format_agent_line_active_not_dimmed(self):
        """Active agents should not have dim codes."""
        agent = make_agent("%5")

        line = format_agent_line(agent, "project", inactive=False)

        assert ANSI_DIM not in line
        assert ANSI_RESET not in line

    def test_format_agents_for_fzf_with_inactive_section(self):
        """Should add blank line separator between active and inactive sections."""
        agents = [
            make_agent("%1"),
            make_agent("%2"),
            make_agent("%3"),
        ]
        inactive_pane_ids = {"%3"}

        output = format_agents_for_fzf(agents, width=100, inactive_pane_ids=inactive_pane_ids)

        lines = output.split("\n")
        # Should have blank line separator
        assert "" in lines
        # Inactive line should be dimmed
        assert any(ANSI_DIM in line for line in lines)

    def test_format_agents_for_fzf_no_separator_when_all_active(self):
        """Should not add blank line when all agents are active."""
        agents = [make_agent("%1"), make_agent("%2")]

        output = format_agents_for_fzf(agents, width=100, inactive_pane_ids=set())

        lines = output.split("\n")
        assert "" not in lines  # No blank lines

    def test_format_agents_for_fzf_no_separator_when_all_inactive(self):
        """Should not add blank line when all agents are inactive."""
        agents = [make_agent("%1"), make_agent("%2")]

        output = format_agents_for_fzf(agents, width=100, inactive_pane_ids={"%1", "%2"})

        lines = output.split("\n")
        assert "" not in lines  # No blank lines
