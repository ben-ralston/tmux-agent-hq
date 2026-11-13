"""Tests for agent discovery module."""

from unittest.mock import patch

from agent_hq.discovery import (
    AGENT_COMMANDS,
    build_process_tree,
    discover_agents,
    find_agent_command,
    get_all_panes,
)


class TestGetAllPanes:
    """Tests for get_all_panes function."""

    def test_parses_tmux_output(self):
        """Should parse tmux list-panes output correctly."""
        mock_output = "main\t1\t0\t%5\t1234\nwork\t2\t1\t%8\t5678\n"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = mock_output
            mock_run.return_value.returncode = 0

            panes = get_all_panes()

        assert len(panes) == 2
        assert panes[0] == {
            "session_name": "main",
            "window_index": 1,
            "pane_index": 0,
            "pane_id": "%5",
            "pane_pid": 1234,
        }
        assert panes[1] == {
            "session_name": "work",
            "window_index": 2,
            "pane_index": 1,
            "pane_id": "%8",
            "pane_pid": 5678,
        }

    def test_handles_empty_output(self):
        """Should return empty list for empty output."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
            mock_run.return_value.returncode = 0

            panes = get_all_panes()

        assert panes == []

    def test_handles_tmux_not_found(self):
        """Should return empty list if tmux is not installed."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()

            panes = get_all_panes()

        assert panes == []


class TestBuildProcessTree:
    """Tests for build_process_tree function."""

    def test_builds_tree_from_ps_output(self):
        """Should build process tree from ps output."""
        mock_output = "  PID  PPID COMM\n    1     0 launchd\n 1234     1 zsh\n 5678  1234 claude\n"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = mock_output
            mock_run.return_value.returncode = 0

            children, commands = build_process_tree()

        assert children[0] == [1]
        assert children[1] == [1234]
        assert children[1234] == [5678]
        assert commands[1] == "launchd"
        assert commands[1234] == "zsh"
        assert commands[5678] == "claude"

    def test_handles_ps_not_found(self):
        """Should return empty dicts if ps is not available."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()

            children, commands = build_process_tree()

        assert children == {}
        assert commands == {}


class TestFindAgentCommand:
    """Tests for find_agent_command function."""

    def test_finds_direct_agent(self):
        """Should find agent that is direct child of pane pid."""
        children = {1: [2], 2: []}
        commands = {1: "zsh", 2: "claude"}

        cmd, pid = find_agent_command(1, children, commands)

        assert cmd == "claude"
        assert pid == 2

    def test_finds_nested_agent(self):
        """Should find agent that is deeply nested."""
        children = {1: [2], 2: [3], 3: [4], 4: []}
        commands = {1: "zsh", 2: "bash", 3: "python", 4: "aider"}

        cmd, pid = find_agent_command(1, children, commands)

        assert cmd == "aider"
        assert pid == 4

    def test_returns_none_for_no_agent(self):
        """Should return None if no agent command found."""
        children = {1: [2], 2: []}
        commands = {1: "zsh", 2: "vim"}

        cmd, pid = find_agent_command(1, children, commands)

        assert cmd is None
        assert pid is None

    def test_handles_missing_pid(self):
        """Should return None for non-existent pid."""
        children = {}
        commands = {}

        cmd, pid = find_agent_command(999, children, commands)

        assert cmd is None
        assert pid is None

    def test_prevents_infinite_loop(self):
        """Should handle cycles in process tree (shouldn't happen but be safe)."""
        # Create a cycle: 1 -> 2 -> 1
        children = {1: [2], 2: [1]}
        commands = {1: "zsh", 2: "bash"}

        # Should not hang
        cmd, pid = find_agent_command(1, children, commands)

        assert cmd is None
        assert pid is None


class TestDiscoverAgents:
    """Tests for discover_agents function."""

    def test_discovers_agents_in_panes(self):
        """Should discover agents across all panes."""
        panes_output = "main\t1\t0\t%5\t1234\n"
        ps_output = "  PID  PPID COMM\n 1234     1 zsh\n 5678  1234 claude\n"

        with patch("subprocess.run") as mock_run:

            def side_effect(*args, **kwargs):
                cmd = args[0]
                mock = type("Mock", (), {"returncode": 0})()
                if cmd[0] == "tmux":
                    mock.stdout = panes_output
                else:
                    mock.stdout = ps_output
                return mock

            mock_run.side_effect = side_effect

            agents = discover_agents()

        assert len(agents) == 1
        assert agents[0]["pane_id"] == "%5"
        assert agents[0]["command"] == "claude"
        assert agents[0]["agent_pid"] == 5678

    def test_filters_by_command(self):
        """Should filter agents by specified commands."""
        panes_output = "main\t1\t0\t%5\t1234\nwork\t2\t0\t%8\t4321\n"
        ps_output = (
            "  PID  PPID COMM\n"
            " 1234     1 zsh\n"
            " 5678  1234 claude\n"
            " 4321     1 zsh\n"
            " 8765  4321 aider\n"
        )

        with patch("subprocess.run") as mock_run:

            def side_effect(*args, **kwargs):
                cmd = args[0]
                mock = type("Mock", (), {"returncode": 0})()
                if cmd[0] == "tmux":
                    mock.stdout = panes_output
                else:
                    mock.stdout = ps_output
                return mock

            mock_run.side_effect = side_effect

            agents = discover_agents(filter_commands=["claude"])

        assert len(agents) == 1
        assert agents[0]["command"] == "claude"


class TestAgentCommands:
    """Tests for AGENT_COMMANDS constant."""

    def test_contains_known_agents(self):
        """Should contain all known AI coding agents."""
        expected = {"claude", "codex", "aider", "opencode", "gemini", "cursor", "roo"}
        assert expected == AGENT_COMMANDS
