"""Tests for the `agent-hq recover` CLI subcommand wiring. #125 #135"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_hq.cli import create_parser, main


class TestRecoverParser:
    def test_recover_subcommand_recognized(self):
        args = create_parser().parse_args(["recover"])
        assert args.subcommand == "recover"
        assert args.json is False
        assert args.since is None
        assert args.baseline_back is None

    def test_json_flag(self):
        args = create_parser().parse_args(["recover", "--json"])
        assert args.json is True

    def test_top_level_json_before_recover_is_honored(self):
        # The recover --json dest must not clobber a top-level --json. #125
        args = create_parser().parse_args(["--json", "recover"])
        assert args.json is True

    def test_since_flag(self):
        args = create_parser().parse_args(["recover", "--since", "45m"])
        assert args.since == "45m"

    def test_baseline_flag(self):
        args = create_parser().parse_args(["recover", "--baseline", "3"])
        assert args.baseline_back == 3

    def test_since_and_baseline_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            create_parser().parse_args(["recover", "--since", "1h", "--baseline", "2"])


class TestRecoverDispatch:
    @patch("agent_hq.recover.run_recover", return_value=0)
    def test_main_dispatches_recover(self, mock_run):
        with patch("sys.argv", ["agent-hq", "recover"]):
            rc = main()
        assert rc == 0
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("as_json") is False

    @patch("agent_hq.recover.run_recover", return_value=0)
    def test_main_dispatches_recover_json(self, mock_run):
        with patch("sys.argv", ["agent-hq", "recover", "--json"]):
            main()
        assert mock_run.call_args.kwargs.get("as_json") is True

    @patch("agent_hq.recover.run_recover", return_value=0)
    def test_main_passes_since_and_baseline(self, mock_run):
        with patch("sys.argv", ["agent-hq", "recover", "--since", "2h"]):
            main()
        assert mock_run.call_args.kwargs.get("since") == "2h"
        with patch("sys.argv", ["agent-hq", "recover", "--baseline", "3"]):
            main()
        assert mock_run.call_args.kwargs.get("baseline_back") == 3
