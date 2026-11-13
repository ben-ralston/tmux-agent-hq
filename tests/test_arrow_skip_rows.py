"""Arrow keys (and the other DataTable-native movement keys) must skip
header/separator rows exactly like ``j``/``k`` do (#261).

The focused DataTable's own ``up``/``down``/``pageup``/``pagedown``/
``ctrl+home``/``ctrl+end`` bindings take precedence over the screen's, so the
raw Textual cursor move used to strand the cursor on a non-selectable row
until the next refresh tick rebuilt the table.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from textual.app import App, ComposeResult
from textual.widgets import DataTable

from agent_hq.dashboard import AgentDashboard
from agent_hq.models import Issue, Priority
from agent_hq.screens._dispatch_common import SkipRowTable
from tests.test_dashboard import make_agent
from tests.test_dispatch_screen import _DispatchTestApp, _DispatchTestScreen, _make_config

# ---------------------------------------------------------------------------
# Shared table widget
# ---------------------------------------------------------------------------


class _TableApp(App):
    """Bare host for a SkipRowTable: rows keyed None are non-selectable."""

    def __init__(self, keys: list[str | None]) -> None:
        super().__init__()
        self.keys = keys

    def compose(self) -> ComposeResult:
        yield SkipRowTable(
            id="t",
            cursor_type="row",
            is_data_row=lambda row: 0 <= row < len(self.keys) and self.keys[row] is not None,
        )

    def on_mount(self) -> None:
        table = self.query_one("#t", SkipRowTable)
        table.add_columns("A")
        for key in self.keys:
            table.add_row(key or "")
        table.focus()


def _run_table(keys: list[str | None], start_row: int, presses: list[str]) -> int:
    result: dict[str, int] = {}

    async def run() -> None:
        app = _TableApp(keys)
        async with app.run_test(size=(40, 6)) as pilot:
            await pilot.pause()
            table = app.query_one("#t", SkipRowTable)
            table.move_cursor(row=start_row)
            await pilot.pause()
            for key in presses:
                await pilot.press(key)
                await pilot.pause()
            result["row"] = table.cursor_row

    asyncio.run(run())
    return result["row"]


class TestSkipRowTable:
    # header, a, sep, header, b, sep, sep, c
    KEYS = [None, "a", None, None, "b", None, None, "c"]

    def test_down_arrow_skips_separator_and_header(self):
        assert _run_table(self.KEYS, 1, ["down"]) == 4

    def test_up_arrow_skips_separator_and_header(self):
        assert _run_table(self.KEYS, 4, ["up"]) == 1

    def test_up_arrow_at_top_reverses_off_leading_header(self):
        # From ``a`` the only row above is the header — reverse back to ``a``.
        assert _run_table(self.KEYS, 1, ["up"]) == 1

    def test_down_arrow_at_bottom_stays_on_last_data_row(self):
        assert _run_table(self.KEYS, 7, ["down"]) == 7

    def test_repeated_down_walks_every_data_row(self):
        assert _run_table(self.KEYS, 1, ["down", "down"]) == 7

    def test_ctrl_home_lands_on_first_data_row(self):
        assert _run_table(self.KEYS, 7, ["ctrl+home"]) == 1

    def test_ctrl_end_lands_on_last_data_row(self):
        keys = [None, "a", None, "b", None, None]
        assert _run_table(keys, 1, ["ctrl+end"]) == 3

    def test_pagedown_skips_to_next_data_row(self):
        # Table height 6 ⇒ a page is 5 rows: a raw page-down from ``a`` (row
        # 1) lands on row 6, a separator, and must carry on to ``f``.
        keys = [None, "a", "b", "c", "d", "e", None, None, "f", "g"]
        assert _run_table(keys, 1, ["pagedown"]) == 8

    def test_pageup_skips_to_previous_data_row(self):
        # Raw page-up from row 8 lands on row 3, a separator; keep going up.
        keys = ["a", "b", None, None, None, "e", "f", "g", "h", "i"]
        assert _run_table(keys, 8, ["pageup"]) == 1

    def test_direct_action_matches_key_press(self):
        """Screen-level j/k call the table action directly — same skip."""
        result: dict[str, int] = {}

        async def run() -> None:
            app = _TableApp(self.KEYS)
            async with app.run_test(size=(40, 6)) as pilot:
                await pilot.pause()
                table = app.query_one("#t", SkipRowTable)
                table.move_cursor(row=1)
                table.action_cursor_down()
                result["down"] = table.cursor_row
                table.action_cursor_up()
                result["up"] = table.cursor_row

        asyncio.run(run())
        assert result == {"down": 4, "up": 1}

    def test_skip_to_data_row_reverses_at_boundary(self):
        """The explicit helper (used after rebuilds / gg / G) reverses when
        the requested direction hits a wall while still on a non-data row."""
        result: dict[str, int] = {}

        async def run() -> None:
            app = _TableApp([None, "a", None, "b", None, None])
            async with app.run_test(size=(40, 6)) as pilot:
                await pilot.pause()
                table = app.query_one("#t", SkipRowTable)
                table.move_cursor(row=5)
                table.skip_to_data_row(downward=True)
                result["bottom"] = table.cursor_row
                table.move_cursor(row=0)
                table.skip_to_data_row(downward=False)
                result["top"] = table.cursor_row

        asyncio.run(run())
        assert result == {"bottom": 3, "top": 1}


# ---------------------------------------------------------------------------
# Agents tab
# ---------------------------------------------------------------------------


class TestAgentsArrowKeys:
    def test_pilot_down_arrow_skips_repo_header_and_separator(self):
        a1 = make_agent("%1", repo_name="r1", branch="b1")
        a2 = make_agent("%2", repo_name="r2", branch="b2")
        a2.cwd = a2.repo_root = "/other"  # distinct repo ⇒ own header group

        async def run() -> None:
            with (
                patch("agent_hq.dashboard.is_daemon_running", return_value=True),
                patch("agent_hq.dashboard.get_agents_cached", return_value=([a1, a2], 0.0)),
                patch.object(AgentDashboard, "_load_preview"),
            ):
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    app._agents = [a1, a2]
                    app._inactive_pane_ids = set()
                    app._pinned_keys = set()
                    app._search_query = ""
                    app._sort_mode = "repo"  # repo headers + separators
                    app._rebuild_table()
                    await pilot.pause()
                    table = app.query_one("#agent-table", DataTable)
                    table.focus()
                    table.move_cursor(row=app._row_pane_ids.index("%1"))
                    await pilot.pause()
                    # [hdr r1, %1, sep, hdr r2, %2]
                    assert app._row_pane_ids == [None, "%1", None, None, "%2"]

                    await pilot.press("down")
                    await pilot.pause()
                    assert app._selected_pane_id() == "%2"

                    await pilot.press("up")
                    await pilot.pause()
                    assert app._selected_pane_id() == "%1"

                    # Up from the first agent: only the header is above —
                    # the cursor must not strand there.
                    await pilot.press("up")
                    await pilot.pause()
                    assert app._selected_pane_id() == "%1"

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Dispatch tab
# ---------------------------------------------------------------------------


class TestDispatchArrowKeys:
    def test_pilot_down_and_up_skip_rank_separator(self):
        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen._issues = [
                    Issue(
                        number=1, title="Need", repo="o/r", status="Todo", priority=Priority.NEED
                    ),
                    Issue(
                        number=2, title="Want", repo="o/r", status="Todo", priority=Priority.WANT
                    ),
                ]
                from agent_hq.issues_hub import IssueAnnotation

                screen._annotations = {"o/r#1": IssueAnnotation(pinned=True)}
                screen._rebuild_table()
                await pilot.pause()
                table = screen.query_one("#issue-table", DataTable)
                table.focus()
                table.move_cursor(row=1)
                await pilot.pause()
                assert screen._row_issue_keys == [None, "o/r#1", None, None, "o/r#2"]

                await pilot.press("down")
                await pilot.pause()
                assert screen._selected_issue_key() == "o/r#2"

                await pilot.press("up")
                await pilot.pause()
                assert screen._selected_issue_key() == "o/r#1"

        asyncio.run(run())

