"""Tests for CLI help output (issue #45)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agent_hq.cli import create_parser


def _find_subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    """Walk a parser's actions to find a named subparser."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            if name in action.choices:
                return action.choices[name]
    raise AssertionError(f"subparser {name!r} not found")


class TestTopLevelHelp:
    def test_has_examples_section(self):
        parser = create_parser()
        assert "Examples:" in parser.format_help()

    def test_groups_output_format_flags(self):
        parser = create_parser()
        help_text = parser.format_help()
        assert "Output format:" in help_text

    def test_groups_filter_flags(self):
        parser = create_parser()
        help_text = parser.format_help()
        assert "Filters:" in help_text

    def test_groups_mode_flags(self):
        parser = create_parser()
        help_text = parser.format_help()
        assert "Modes:" in help_text

    def test_groups_internal_flags_separately(self):
        parser = create_parser()
        help_text = parser.format_help()
        # fzf-binding flags are real but confusing to newcomers — they should
        # be visible under a clearly-labeled "Internal" group rather than
        # mixed into the main flag list.
        assert "Internal" in help_text
        assert "--toggle-inactive" in help_text

    def test_lists_daemon_subcommand(self):
        parser = create_parser()
        help_text = parser.format_help()
        assert "daemon" in help_text
        # Subcommand help string should describe what it does.
        assert "background" in help_text.lower()

    def test_example_shows_common_workflow(self):
        parser = create_parser()
        help_text = parser.format_help()
        # At least one concrete invocation should appear in the epilog.
        assert "agent-hq --dashboard" in help_text or "agent-hq --list" in help_text


class TestDaemonHelp:
    def test_daemon_has_description(self):
        parser = create_parser()
        daemon = _find_subparser(parser, "daemon")
        help_text = daemon.format_help()
        assert "daemon" in help_text.lower()
        # Description should be a real sentence, not just the usage line.
        assert daemon.description
        assert len(daemon.description) > 20

    def test_daemon_has_examples(self):
        parser = create_parser()
        daemon = _find_subparser(parser, "daemon")
        assert "Examples:" in daemon.format_help()

    def test_daemon_lists_actions(self):
        parser = create_parser()
        daemon = _find_subparser(parser, "daemon")
        help_text = daemon.format_help()
        assert "start" in help_text
        assert "stop" in help_text
        assert "status" in help_text


class TestDaemonLeafHelp:
    def test_daemon_start_has_description(self):
        parser = create_parser()
        daemon = _find_subparser(parser, "daemon")
        start = _find_subparser(daemon, "start")
        assert start.description
        assert len(start.description) > 10

    def test_daemon_start_interval_has_metavar(self):
        parser = create_parser()
        daemon = _find_subparser(parser, "daemon")
        start = _find_subparser(daemon, "start")
        help_text = start.format_help()
        # After the improvement the interval flag should show a named metavar.
        assert "SECONDS" in help_text

    def test_daemon_stop_has_description(self):
        parser = create_parser()
        daemon = _find_subparser(parser, "daemon")
        stop = _find_subparser(daemon, "stop")
        assert stop.description
        assert len(stop.description) > 10

    def test_daemon_status_has_description(self):
        parser = create_parser()
        daemon = _find_subparser(parser, "daemon")
        status = _find_subparser(daemon, "status")
        assert status.description
        assert len(status.description) > 10


class TestHelpExitsCleanly:
    """`-h` should exit 0 at every level without blowing up."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["-h"],
            ["--help"],
            ["daemon", "-h"],
            ["daemon", "start", "-h"],
            ["daemon", "stop", "-h"],
            ["daemon", "status", "-h"],
            ["web", "-h"],
            ["web", "service", "-h"],
            ["web", "service", "install", "-h"],
            ["web", "service", "uninstall", "-h"],
            ["web", "service", "status", "-h"],
            ["web", "service", "restart", "-h"],
        ],
    )
    def test_help_exits_zero(self, argv, capsys):
        parser = create_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(argv)
        assert exc_info.value.code == 0


class TestWebSubcommand:
    """Mobile-first web dashboard subcommand parsing."""

    def test_default_args(self):
        parser = create_parser()
        args = parser.parse_args(["web"])
        assert args.subcommand == "web"
        assert args.port == 8765
        assert args.bind is None  # auto-detect tailnet at runtime
        assert args.no_daemon is False
        assert args.verbose is False

    def test_port_flag(self):
        parser = create_parser()
        args = parser.parse_args(["web", "--port", "9000"])
        assert args.port == 9000

    def test_bind_flag(self):
        parser = create_parser()
        args = parser.parse_args(["web", "--bind", "0.0.0.0"])
        assert args.bind == "0.0.0.0"

    def test_no_daemon_flag(self):
        parser = create_parser()
        args = parser.parse_args(["web", "--no-daemon"])
        assert args.no_daemon is True

    def test_quiet_flag(self):
        parser = create_parser()
        assert parser.parse_args(["web"]).quiet is False
        assert parser.parse_args(["web", "--quiet"]).quiet is True

    def test_token_file_flag(self):
        parser = create_parser()
        args = parser.parse_args(["web", "--token-file", "/tmp/foo"])
        assert args.token_file == Path("/tmp/foo")

    def test_has_description(self):
        parser = create_parser()
        web = _find_subparser(parser, "web")
        assert web.description
        assert len(web.description) > 10


class TestWebServiceSubcommand:
    """`agent-hq web service <action>` launchd management (issue #100)."""

    def test_install_parses(self):
        parser = create_parser()
        args = parser.parse_args(["web", "service", "install"])
        assert args.subcommand == "web"
        assert args.web_action == "service"
        assert args.service_action == "install"

    def test_install_default_flags(self):
        parser = create_parser()
        args = parser.parse_args(["web", "service", "install"])
        assert args.bind is None  # auto-detect tailnet
        assert args.port == 8765

    def test_install_flags(self):
        parser = create_parser()
        args = parser.parse_args(
            ["web", "service", "install", "--bind", "0.0.0.0", "--port", "9000"]
        )
        assert args.bind == "0.0.0.0"
        assert args.port == 9000

    @pytest.mark.parametrize("action", ["uninstall", "status", "restart"])
    def test_other_actions_parse(self, action):
        parser = create_parser()
        args = parser.parse_args(["web", "service", action])
        assert args.web_action == "service"
        assert args.service_action == action

    def test_bare_web_still_parses(self):
        # Regression: bare `web` must still fall through to run_web.
        parser = create_parser()
        args = parser.parse_args(["web"])
        assert args.subcommand == "web"
        assert getattr(args, "web_action", None) is None

    def test_bind_before_subcommand_survives(self):
        # Regression: a --bind passed BEFORE `service install` must not be
        # silently clobbered by the install subparser's default.
        parser = create_parser()
        args = parser.parse_args(["web", "--bind", "0.0.0.0", "service", "install"])
        assert args.bind == "0.0.0.0"

    def test_port_before_subcommand_survives(self):
        parser = create_parser()
        args = parser.parse_args(["web", "--port", "9000", "service", "install"])
        assert args.port == 9000

    def test_install_no_flags_leaves_parent_defaults(self):
        parser = create_parser()
        args = parser.parse_args(["web", "service", "install"])
        assert args.bind is None
        assert args.port == 8765

    def test_service_lists_actions(self):
        parser = create_parser()
        web = _find_subparser(parser, "web")
        service = _find_subparser(web, "service")
        help_text = service.format_help()
        for action in ("install", "uninstall", "status", "restart"):
            assert action in help_text
