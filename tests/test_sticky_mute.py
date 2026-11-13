"""Mute is sticky (#303): a pane's content changing never unmutes it.

Every surface that used to compare the stored content fingerprint against
the live pane and silently unmute on a mismatch — the TUI refresh, the fzf
``--list`` reload and the interactive fzf entry — must leave the mute in
place. Only a deliberate unmute clears it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from agent_hq import cli
from agent_hq.dashboard import AgentDashboard
from agent_hq.models import Agent, AgentState, AgentType
from agent_hq.state import load_inactive_state, toggle_inactive


def _agent(pane_id: str) -> Agent:
    return Agent(
        pane_id=pane_id,
        session_name="s",
        window_index=1,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd="/project",
        repo_root="/project",
        repo_name="project",
        branch="main",
        state=AgentState.IDLE,
    )


def _mute_with_stale_fingerprint(pane_id: str) -> None:
    toggle_inactive(pane_id, "fingerprint-at-mute")
    assert pane_id in load_inactive_state()


def _run_cli(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr("sys.argv", ["agent-hq", *argv])
    with (
        patch("agent_hq.cli.is_in_tmux", return_value=True),
        patch("agent_hq.cli.get_agents", return_value=[_agent("%1")]),
        patch("agent_hq.cli.get_all_panes", return_value=[{"pane_id": "%1"}]),
        # The pane's screen has changed since the mute.
        patch(
            "agent_hq.enrichment.compute_content_fingerprint",
            return_value="fingerprint-now",
        ),
        patch("agent_hq.cli.run_fzf", return_value=""),
        patch("agent_hq.cli.get_terminal_width", return_value=120),
    ):
        return cli.main()


def test_list_reload_keeps_mute_when_content_changes(monkeypatch, capsys):
    _mute_with_stale_fingerprint("%1")

    assert _run_cli(monkeypatch, ["--list"]) == 0

    assert "%1" in load_inactive_state()


def test_interactive_fzf_keeps_mute_when_content_changes(monkeypatch):
    _mute_with_stale_fingerprint("%1")

    assert _run_cli(monkeypatch, []) == 0

    assert "%1" in load_inactive_state()


def test_dashboard_refresh_keeps_mute_when_content_changes():
    _mute_with_stale_fingerprint("%1")
    agent = _agent("%1")

    async def run_test() -> None:
        with (
            patch("agent_hq.dashboard.is_daemon_running", return_value=True),
            patch("agent_hq.dashboard.get_agents_cached", return_value=([agent], 1.0)),
            patch(
                "agent_hq.enrichment.compute_content_fingerprint",
                return_value="fingerprint-now",
            ),
            patch.object(AgentDashboard, "_load_preview"),
        ):
            app = AgentDashboard()
            async with app.run_test(size=(120, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert "%1" in app._inactive_pane_ids

    asyncio.run(run_test())
    assert "%1" in load_inactive_state()


def _run_cli_agent_missing(monkeypatch, argv: list[str]) -> int:
    """%1 is muted and its pane is alive, but discovery doesn't see an
    agent in it this tick (agent restarting, or a missed discovery)."""
    monkeypatch.setattr("sys.argv", ["agent-hq", *argv])
    with (
        patch("agent_hq.cli.is_in_tmux", return_value=True),
        patch("agent_hq.cli.get_agents", return_value=[_agent("%2")]),
        patch(
            "agent_hq.cli.get_all_panes",
            return_value=[{"pane_id": "%1"}, {"pane_id": "%2"}],
        ),
        patch("agent_hq.cli.run_fzf", return_value=""),
        patch("agent_hq.cli.get_terminal_width", return_value=120),
    ):
        return cli.main()


def test_list_reload_keeps_mute_when_agent_briefly_missing(monkeypatch, capsys):
    _mute_with_stale_fingerprint("%1")

    assert _run_cli_agent_missing(monkeypatch, ["--list"]) == 0

    assert "%1" in load_inactive_state()


def test_interactive_fzf_keeps_mute_when_agent_briefly_missing(monkeypatch):
    _mute_with_stale_fingerprint("%1")

    assert _run_cli_agent_missing(monkeypatch, []) == 0

    assert "%1" in load_inactive_state()


def test_list_reload_still_prunes_mute_of_closed_pane(monkeypatch, capsys):
    _mute_with_stale_fingerprint("%9")

    assert _run_cli_agent_missing(monkeypatch, ["--list"]) == 0

    assert "%9" not in load_inactive_state()
