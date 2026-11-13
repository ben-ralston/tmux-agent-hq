"""Tests for dispatch screen data helpers."""

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from textual.app import App
from textual.widgets import DataTable, SelectionList

from agent_hq.capabilities import CLAUDE_ONLY, LauncherCapabilities
from agent_hq.config import Config, HarnessDefaults, ProjectConfig, RepoConfig
from agent_hq.models import Issue, Priority
from agent_hq.screens._dispatch_common import DispatchGuard, SelectionState
from agent_hq.screens.dispatch_screen import (
    ConfirmDispatchModal,
    ConfirmModal,
    DispatchBatch,
    DispatchScreen,
    build_issue_rows,
    group_batches_by_repo,
)

_CODEX_CAPS = LauncherCapabilities(agents=frozenset({"claude", "codex"}), flags={})


def _issue(
    number,
    repo="o/r",
    priority=Priority.NEED,
    status="Todo",
    title="Test",
    body="",
    updated_at=None,
    created_at=None,
    labels=None,
):
    return Issue(
        number=number,
        title=title,
        repo=repo,
        status=status,
        priority=priority,
        body=body,
        updated_at=updated_at,
        created_at=created_at,
        labels=labels or [],
    )


class TestBuildIssueRows:
    def test_marks_sentinel_issues(self):
        # #235: sentinel-labeled issues get a colored marker, display only.
        sentinel = _issue(211, labels=["sentinel"])
        normal = _issue(212)
        rows = build_issue_rows(
            [sentinel, normal],
            selected_keys=set(),
            grouped_batches=[],
            active_issues=set(),
        )
        by_key = {r["issue_key"]: r for r in rows if not r["is_separator"]}
        assert "⚑" in by_key["o/r#211"]["issue"]
        assert "dark_orange" in by_key["o/r#211"]["issue"]
        assert "⚑" not in by_key["o/r#212"]["issue"]
        assert "dark_orange" not in by_key["o/r#212"]["issue"]

    def test_title_with_rich_markup_is_escaped(self):
        # A title containing "[/]"-shaped text must not be interpreted as
        # Rich markup (it would raise MarkupError on render).
        from rich.text import Text

        issue = _issue(300, title="Nasty [/bold] title")
        rows = build_issue_rows(
            [issue],
            selected_keys=set(),
            grouped_batches=[],
            active_issues=set(),
        )
        row = rows[0]
        Text.from_markup(row["issue"])  # must not raise MarkupError
        assert row["issue"] == "#300 Nasty \\[/bold] title"

    def test_sentinel_title_with_rich_markup_is_escaped(self):
        # The sentinel ⚑ prefix still applies on top of an escaped title.
        from rich.text import Text

        issue = _issue(301, title="Nasty [/bold] title", labels=["sentinel"])
        rows = build_issue_rows(
            [issue],
            selected_keys=set(),
            grouped_batches=[],
            active_issues=set(),
        )
        row = rows[0]
        Text.from_markup(row["issue"])  # must not raise MarkupError
        assert "⚑" in row["issue"]
        assert "dark_orange" in row["issue"]
        assert "Nasty \\[/bold] title" in row["issue"]

    def test_single_issue_row(self):
        rows = build_issue_rows(
            [_issue(42)],
            selected_keys=set(),
            grouped_batches=[],
            active_issues=set(),
        )
        data_rows = [r for r in rows if not r["is_separator"]]
        assert len(data_rows) == 1
        assert data_rows[0]["issue_key"] == "o/r#42"
        assert data_rows[0]["score"] == "—"

    def test_sorted_by_priority(self):
        issues = [
            _issue(1, priority=Priority.IDEA),
            _issue(2, priority=Priority.NEED),
            _issue(3, priority=Priority.WANT),
        ]
        rows = build_issue_rows(issues, set(), [], set())
        data_rows = [r for r in rows if not r["is_separator"]]
        assert data_rows[0]["issue_key"] == "o/r#2"  # NEED first
        assert data_rows[1]["issue_key"] == "o/r#3"  # WANT
        assert data_rows[2]["issue_key"] == "o/r#1"  # IDEA

    def test_storeless_has_no_group_separators(self):
        issues = [
            _issue(1, priority=Priority.NEED),
            _issue(2, priority=Priority.WANT),
        ]
        rows = build_issue_rows(issues, set(), [], set())
        assert not any(r["is_separator"] for r in rows)

    def test_selected_indicator(self):
        rows = build_issue_rows(
            [_issue(42)],
            selected_keys={"o/r#42"},
            grouped_batches=[],
            active_issues=set(),
        )
        data_rows = [r for r in rows if not r["is_separator"]]
        assert data_rows[0]["sel"] != ""

    def test_active_indicator(self):
        rows = build_issue_rows(
            [_issue(42)],
            selected_keys=set(),
            grouped_batches=[],
            active_issues={"o/r#42"},
        )
        data_rows = [r for r in rows if not r["is_separator"]]
        assert data_rows[0]["active"] != ""

    def test_empty_issues(self):
        rows = build_issue_rows([], set(), [], set())
        assert rows == []

    def test_grouped_batch_indicator(self):
        rows = build_issue_rows(
            [_issue(1), _issue(2)],
            selected_keys={"o/r#1", "o/r#2"},
            grouped_batches=[{"o/r#1", "o/r#2"}],
            active_issues=set(),
        )
        data_rows = [r for r in rows if not r["is_separator"]]
        assert data_rows[0]["sel"] == "G1"
        assert data_rows[1]["sel"] == "G1"

    def test_created_mode_orders_by_recency_ignoring_priority(self):
        issues = [
            _issue(1, priority=Priority.NEED, created_at="2026-05-10T00:00:00Z"),
            _issue(2, priority=Priority.IDEA, created_at="2026-05-20T00:00:00Z"),
        ]
        rows = build_issue_rows(issues, set(), [], set(), sort_mode="created")
        data_rows = [r for r in rows if not r["is_separator"]]
        # Newest first even though #1 has higher priority.
        assert [r["issue_key"] for r in data_rows] == ["o/r#2", "o/r#1"]

    def test_date_mode_has_no_priority_separators(self):
        issues = [
            _issue(1, priority=Priority.NEED, created_at="2026-05-10T00:00:00Z"),
            _issue(2, priority=Priority.IDEA, created_at="2026-05-20T00:00:00Z"),
        ]
        rows = build_issue_rows(issues, set(), [], set(), sort_mode="created")
        assert not any(r["is_separator"] for r in rows)

    def test_storeless_default_has_no_separators(self):
        issues = [
            _issue(1, priority=Priority.NEED),
            _issue(2, priority=Priority.WANT),
        ]
        rows = build_issue_rows(issues, set(), [], set(), sort_mode="default")
        assert not any(r["is_separator"] for r in rows)

    def test_ascending_direction_reverses_order(self):
        issues = [
            _issue(1, created_at="2026-05-10T00:00:00Z"),
            _issue(2, created_at="2026-05-20T00:00:00Z"),
        ]
        rows = build_issue_rows(issues, set(), [], set(), sort_mode="created", descending=False)
        data_rows = [r for r in rows if not r["is_separator"]]
        assert [r["issue_key"] for r in data_rows] == ["o/r#1", "o/r#2"]

    def test_updated_mode_uses_updated_at(self):
        rows = build_issue_rows(
            [_issue(1, created_at=None, updated_at="2026-05-20T00:00:00Z")],
            set(),
            [],
            set(),
            sort_mode="updated",
        )
        data_rows = [r for r in rows if not r["is_separator"]]
        assert data_rows[0]["date"] != ""

    def test_row_carries_relative_age_for_dated_issue(self):
        rows = build_issue_rows(
            [_issue(1, created_at="2026-05-20T00:00:00Z")],
            set(),
            [],
            set(),
            sort_mode="created",
        )
        data_rows = [r for r in rows if not r["is_separator"]]
        # The age is rendered relative to wall-clock, so just assert it's
        # present and non-empty (exact buckets covered in test_filters).
        assert data_rows[0]["date"] != ""

    def test_row_age_blank_for_undated_issue(self):
        rows = build_issue_rows(
            [_issue(1, created_at=None)],
            set(),
            [],
            set(),
            sort_mode="created",
        )
        data_rows = [r for r in rows if not r["is_separator"]]
        assert data_rows[0]["date"] == ""


class TestGroupBatchesByRepo:
    def _batch(self, numbers, repo="o/r"):
        issues = [_issue(n, repo=repo) for n in numbers]
        return DispatchBatch(issues=issues, branch="feature/test")

    def test_same_repo_collapses_to_one_group(self):
        batches = [self._batch([6]), self._batch([41])]
        groups = group_batches_by_repo(batches)
        assert len(groups) == 1
        assert len(groups[0]) == 2
        assert groups[0][0].issues[0].number == 6
        assert groups[0][1].issues[0].number == 41

    def test_cross_repo_stays_separate(self):
        batches = [self._batch([1], repo="a/r1"), self._batch([2], repo="b/r2")]
        groups = group_batches_by_repo(batches)
        assert len(groups) == 2
        assert groups[0][0].issues[0].repo == "a/r1"
        assert groups[1][0].issues[0].repo == "b/r2"

    def test_mixed_grouping_preserves_repo_partition(self):
        batches = [
            self._batch([1], repo="a/r1"),
            self._batch([2], repo="b/r2"),
            self._batch([3], repo="a/r1"),
        ]
        groups = group_batches_by_repo(batches)
        assert len(groups) == 2
        # First repo group has two batches (1 and 3)
        repo_a_group = next(g for g in groups if g[0].issues[0].repo == "a/r1")
        assert len(repo_a_group) == 2
        assert [b.issues[0].number for b in repo_a_group] == [1, 3]

    def test_empty_batches_skipped(self):
        batches = [DispatchBatch(issues=[], branch="x"), self._batch([1])]
        groups = group_batches_by_repo(batches)
        assert len(groups) == 1
        assert groups[0][0].issues[0].number == 1

    def test_empty_input_returns_empty(self):
        assert group_batches_by_repo([]) == []


class TestConfirmDispatchModalHarnessPicker:
    """#205 Stage 1B Task 16: the modal computes harness/effort/model
    defaults + codex availability at construction time from an injected
    capabilities probe, so tests never shell out to launch.sh."""

    def _batches(self):
        return [DispatchBatch(issues=[_issue(1)], branch="feature/1")]

    def _config(self, default_harness="claude", harnesses=None):
        cfg = _make_config()
        cfg.dispatch.default_harness = default_harness
        if harnesses:
            cfg.dispatch.harnesses = harnesses
        return cfg

    def test_confirm_modal_defaults_to_config_harness(self):
        cfg = self._config(
            default_harness="codex",
            harnesses={"codex": HarnessDefaults(model="o3", effort="high")},
        )
        modal = ConfirmDispatchModal(self._batches(), cfg, capabilities_probe=lambda: _CODEX_CAPS)
        assert modal._default_harness == "codex"
        assert modal._default_effort == "high"
        assert modal._default_model == "o3"

    def test_confirm_modal_hides_codex_when_unadvertised(self):
        cfg = self._config()
        modal = ConfirmDispatchModal(self._batches(), cfg, capabilities_probe=lambda: CLAUDE_ONLY)
        assert modal._codex_available is False
        assert ("Codex", "codex") not in modal._harness_options

    def test_confirm_modal_shows_codex_when_advertised(self):
        cfg = self._config()
        modal = ConfirmDispatchModal(self._batches(), cfg, capabilities_probe=lambda: _CODEX_CAPS)
        assert modal._codex_available is True
        assert ("Codex", "codex") in modal._harness_options

    def test_confirm_modal_falls_back_to_claude_when_default_unadvertised(self):
        # config says codex by default, but the installed launcher doesn't
        # advertise it -- must not silently pre-select an unusable harness.
        cfg = self._config(default_harness="codex")
        modal = ConfirmDispatchModal(self._batches(), cfg, capabilities_probe=lambda: CLAUDE_ONLY)
        assert modal._default_harness == "claude"

    def test_confirm_modal_defaults_without_config(self):
        modal = ConfirmDispatchModal(self._batches(), None, capabilities_probe=lambda: CLAUDE_ONLY)
        assert modal._default_harness == "claude"
        assert modal._default_effort is None
        assert modal._default_model is None

    def _inspect_mounted_modal(self, modal, inspect):
        """Push ``modal`` in a throwaway App and run ``inspect(screen)``
        while the pilot loop is still active, returning its result.

        Widget reactive state (``.value``, ``.placeholder``) can only be
        read while an App is active (compose() alone can't be driven
        outside one); the widgets also unmount once ``run_test``'s context
        manager exits, so the inspection must happen inside it, not after.
        """
        from textual.app import App

        class _ModalHostApp(App):
            def on_mount(self) -> None:
                self.push_screen(modal)

        result: dict = {}

        async def run_test() -> None:
            app = _ModalHostApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                result["value"] = inspect(app.screen)

        asyncio.run(run_test())
        return result["value"]

    def test_effort_and_model_do_not_prefill_as_values(self):
        """Multi-review fix wave, Fix 5: the effort select stays on its
        blank (no-selection) default and the model input stays empty, even
        though the config has explicit defaults -- pre-filling would become
        an explicit override that survives a harness switch, fighting the
        server-side per-harness default resolution."""
        from textual.widgets import Input, Select

        cfg = self._config(
            default_harness="codex",
            harnesses={"codex": HarnessDefaults(model="o3", effort="high")},
        )
        modal = ConfirmDispatchModal(self._batches(), cfg, capabilities_probe=lambda: _CODEX_CAPS)
        effort_value, model_value, model_placeholder = self._inspect_mounted_modal(
            modal,
            lambda screen: (
                screen.query_one("#effort-select", Select).value,
                screen.query_one("#model-input", Input).value,
                screen.query_one("#model-input", Input).placeholder,
            ),
        )
        assert effort_value is Select.NULL
        assert model_value == ""
        assert model_placeholder == "model (default: o3)"

    def test_model_placeholder_generic_when_no_config_default(self):
        from textual.widgets import Input

        modal = ConfirmDispatchModal(self._batches(), None, capabilities_probe=lambda: CLAUDE_ONLY)
        model_value, model_placeholder = self._inspect_mounted_modal(
            modal,
            lambda screen: (
                screen.query_one("#model-input", Input).value,
                screen.query_one("#model-input", Input).placeholder,
            ),
        )
        assert model_value == ""
        assert model_placeholder == "model (default)"

    def test_harness_select_still_prefills_default(self):
        """Only the harness select is a safe prefill (#205 review) -- it's a
        genuine harness choice, not a value that fights per-harness default
        resolution."""
        from textual.widgets import Select

        cfg = self._config(default_harness="codex")
        modal = ConfirmDispatchModal(self._batches(), cfg, capabilities_probe=lambda: _CODEX_CAPS)
        harness_value = self._inspect_mounted_modal(
            modal, lambda screen: screen.query_one("#harness-select", Select).value
        )
        assert harness_value == "codex"


class TestOnDispatchConfirmedForwarding:
    """The modal's dismiss result -- (confirmed, harness, model, effort) --
    must be forwarded verbatim to _execute_all_dispatches."""

    def _make_screen(self):
        screen = DispatchScreen.__new__(DispatchScreen)
        screen._execute_all_dispatches = MagicMock()
        screen._issues = []
        screen._selection = SelectionState()
        screen._dispatch_guard = DispatchGuard()
        screen._rebuild_table = MagicMock()
        screen.notify = MagicMock()
        return screen

    def test_confirmed_forwards_harness_model_effort(self):
        screen = self._make_screen()
        batches = [DispatchBatch(issues=[_issue(1)], branch="feature/1")]
        screen._on_dispatch_confirmed((True, "codex", None, "high"), batches)
        screen._execute_all_dispatches.assert_called_once_with(batches, "codex", None, "high")

    def test_cancelled_does_not_dispatch(self):
        screen = self._make_screen()
        screen._on_dispatch_confirmed((False, "claude", None, None), [])
        screen._execute_all_dispatches.assert_not_called()


class TestExecuteAllDispatchesForwarding:
    """_execute_all_dispatches must forward the resolved harness/model/effort
    to dispatch_via_claude for every repo group -- captured at dispatch time,
    never re-read off widget state inside the worker."""

    def test_forwards_selection(self, monkeypatch):
        screen = DispatchScreen.__new__(DispatchScreen)
        screen._config = _make_config()
        screen._capabilities_probe = lambda: CLAUDE_ONLY
        batches = [DispatchBatch(issues=[_issue(1)], branch="feature/1")]

        captured = {}

        def fake_dispatch_via_claude(
            issue_batches, config, *, harness=None, model=None, effort=None, **kwargs
        ):
            captured["harness"] = harness
            captured["model"] = model
            captured["effort"] = effort
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="b")

        monkeypatch.setattr("agent_hq.dispatch.dispatch_via_claude", fake_dispatch_via_claude)

        fake_app = MagicMock()
        with patch.object(type(screen), "app", new_callable=PropertyMock, return_value=fake_app):
            DispatchScreen._execute_all_dispatches.__wrapped__(
                screen, batches, "codex", None, "high"
            )

        assert captured == {"harness": "codex", "model": None, "effort": "high"}
        fake_app.call_from_thread.assert_called_once()

    def test_forwards_capabilities_probe(self, monkeypatch):
        """The screen's own injected capabilities_probe must reach
        dispatch_via_claude's inner gate -- not the real launch.sh probe."""
        screen = DispatchScreen.__new__(DispatchScreen)
        screen._config = _make_config()
        sentinel_probe = lambda: _CODEX_CAPS  # noqa: E731
        screen._capabilities_probe = sentinel_probe
        batches = [DispatchBatch(issues=[_issue(1)], branch="feature/1")]

        captured = {}

        def fake_dispatch_via_claude(issue_batches, config, **kwargs):
            captured["capabilities_probe"] = kwargs.get("capabilities_probe")
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="b")

        monkeypatch.setattr("agent_hq.dispatch.dispatch_via_claude", fake_dispatch_via_claude)

        fake_app = MagicMock()
        with patch.object(type(screen), "app", new_callable=PropertyMock, return_value=fake_app):
            DispatchScreen._execute_all_dispatches.__wrapped__(
                screen, batches, "codex", None, "high"
            )

        assert captured["capabilities_probe"] is sentinel_probe


class TestSelectionGrouping:
    def _make_screen(self):
        """Create a DispatchScreen for unit testing selection logic."""
        screen = DispatchScreen.__new__(DispatchScreen)
        screen._issues = []
        screen._selection = SelectionState()
        screen._active_issues = set()
        screen._row_issue_keys = []
        screen._search_query = ""
        return screen

    def test_toggle_selection_adds(self):
        screen = self._make_screen()
        screen.toggle_selection("o/r#42")
        assert "o/r#42" in screen._selection.selected

    def test_toggle_selection_removes(self):
        screen = self._make_screen()
        screen._selection.selected.add("o/r#42")
        screen.toggle_selection("o/r#42")
        assert "o/r#42" not in screen._selection.selected

    def test_group_same_repo(self):
        screen = self._make_screen()
        screen._selection.selected = {"o/r#1", "o/r#2"}
        screen._issues = [_issue(1, repo="o/r"), _issue(2, repo="o/r")]
        result = screen.group_selected()
        assert result == ""  # empty string = success
        assert len(screen._selection.batches) == 1
        assert screen._selection.batches[0] == {"o/r#1", "o/r#2"}

    def test_group_cross_repo_rejected(self):
        screen = self._make_screen()
        screen._selection.selected = {"a/r1#1", "b/r2#2"}
        screen._issues = [_issue(1, repo="a/r1"), _issue(2, repo="b/r2")]
        result = screen.group_selected()
        assert "different repos" in result
        assert len(screen._selection.batches) == 0

    def test_ungroup_dissolves_batch(self):
        screen = self._make_screen()
        screen._selection.selected = {"o/r#1", "o/r#2"}
        screen._selection.batches = [{"o/r#1", "o/r#2"}]
        screen.ungroup("o/r#1")
        assert len(screen._selection.batches) == 0
        # Selections remain after ungrouping
        assert "o/r#1" in screen._selection.selected

    def test_ungroup_key_not_in_group(self):
        screen = self._make_screen()
        screen._selection.batches = [{"o/r#1", "o/r#2"}]
        screen.ungroup("o/r#99")  # Not in any group
        assert len(screen._selection.batches) == 1  # Unchanged

    def test_worktree_count_ungrouped_only(self):
        screen = self._make_screen()
        screen._selection.selected = {"o/r#1", "o/r#2", "o/r#3"}
        assert screen.worktree_count() == 3

    def test_worktree_count_with_group(self):
        screen = self._make_screen()
        screen._selection.selected = {"o/r#1", "o/r#2", "o/r#3"}
        screen._selection.batches = [{"o/r#1", "o/r#2"}]
        # 1 group (1 worktree) + 1 ungrouped (1 worktree) = 2
        assert screen.worktree_count() == 2

    def test_group_requires_selection(self):
        screen = self._make_screen()
        result = screen.group_selected()
        assert result != ""  # non-empty string = error


class _DispatchTestScreen(DispatchScreen):
    """DispatchScreen that skips GitHub auth check and data refresh.

    Defaults ``capabilities_probe`` to a claude-only stub so tests that
    exercise the full dispatch flow (e.g. the Enter-key regression tests
    below) never shell out to the real ``launch.sh``.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("capabilities_probe", lambda: CLAUDE_ONLY)
        super().__init__(*args, **kwargs)

    def _check_gh_auth(self) -> None:  # type: ignore[override]
        return

    def _refresh_data(self) -> None:  # type: ignore[override]
        return


class _DispatchTestApp(App):
    """Test harness mimicking AgentDashboard's own RowSelected → exit handler.

    AgentDashboard (the real app) defines on_data_table_row_selected that calls
    self.exit(...). If DispatchScreen does not stop the RowSelected message from
    bubbling, the app exits instead of dispatching. This harness captures that.
    """

    def __init__(self, screen: _DispatchTestScreen):
        super().__init__()
        self._test_screen = screen
        self.app_row_selected_count = 0

    def on_mount(self) -> None:
        self.install_screen(self._test_screen, name="dispatch")
        self.push_screen("dispatch")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.app_row_selected_count += 1


def _make_config() -> Config:
    return Config(
        repos={"o/r": RepoConfig(owner_name="o/r", local_path=Path("/tmp/o-r"))},
        projects=[],
        worktree_root=Path("/tmp"),
    )


class TestEnterKeyDispatch:
    def test_enter_on_single_selected_issue_does_not_bubble_to_app(self):
        """Regression test for bug #36.

        AgentDashboard (the real App) handles DataTable.RowSelected by calling
        self.exit(...). If DispatchScreen's on_data_table_row_selected does not
        stop the message from bubbling, pressing Enter on the dispatch tab exits
        the whole app before the dispatch modal can open.

        The fix: DispatchScreen must call event.stop() so the RowSelected message
        is consumed at the screen level and does not reach the App handler.
        """
        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen._issues = [
                    Issue(
                        number=1,
                        title="First issue",
                        repo="o/r",
                        status="Todo",
                        priority=Priority.NEED,
                    )
                ]
                screen._rebuild_table()
                await pilot.pause()

                await pilot.press("space")
                await pilot.pause()
                assert screen._selection.selected == {"o/r#1"}

                await pilot.press("enter")
                await pilot.pause()

                assert app.app_row_selected_count == 0, (
                    f"RowSelected bubbled to App {app.app_row_selected_count} time(s); "
                    "DispatchScreen must call event.stop() to prevent AgentDashboard "
                    "from exiting."
                )

        asyncio.run(run_test())

    def test_enter_on_single_selected_issue_pushes_exactly_one_modal(self):
        """Regression: Enter on one selected issue must not double-push the modal.

        Bug #36: the screen had both a Binding("enter", "dispatch") and an
        on_data_table_row_selected handler that each invoked action_dispatch,
        which pushed two ConfirmDispatchModal instances and made the app
        appear to exit when the stack unwound.
        """
        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen._issues = [
                    Issue(
                        number=1,
                        title="First issue",
                        repo="o/r",
                        status="Todo",
                        priority=Priority.NEED,
                    )
                ]
                screen._rebuild_table()
                await pilot.pause()

                await pilot.press("space")
                await pilot.pause()
                assert screen._selection.selected == {"o/r#1"}

                await pilot.press("enter")
                # Give the pushed modal time to mount.
                for _ in range(5):
                    await pilot.pause()

                modals = [s for s in app.screen_stack if isinstance(s, ConfirmDispatchModal)]
                assert len(modals) == 1, f"expected 1 modal, got {len(modals)}"
                assert not app._exit, "app should not have exited"

        asyncio.run(run_test())


class TestSortCycle:
    """Issue #156: the ``S`` key cycles the field default → created → updated;
    ``f`` flips the active date field's direction. Composes with search and
    preserves selection + cursor."""

    def _make_screen(self):
        screen = DispatchScreen.__new__(DispatchScreen)
        screen._sort_mode = "default"
        screen._created_dir = "desc"
        screen._updated_dir = "desc"
        return screen

    def _dated_issues(self):
        # All NEED / same repo, so default order is by number (1, 2, 3).
        return [
            _issue(1, title="alpha", created_at="2026-05-10T00:00:00Z"),
            _issue(2, title="beta", created_at="2026-05-20T00:00:00Z"),
            _issue(3, title="gamma", created_at="2026-05-01T00:00:00Z"),
        ]

    def test_toggle_sort_cycles_default_created_updated(self):
        screen = self._make_screen()
        with patch.object(screen, "_rebuild_table"):
            assert screen._sort_mode == "default"
            screen.action_toggle_sort()
            assert screen._sort_mode == "created"
            screen.action_toggle_sort()
            assert screen._sort_mode == "updated"
            screen.action_toggle_sort()
            assert screen._sort_mode == "default"

    def test_flip_sort_dir_flips_active_field_only(self):
        screen = self._make_screen()
        screen._sort_mode = "created"
        with patch.object(screen, "_rebuild_table"):
            screen.action_flip_sort_dir()
            assert screen._created_dir == "asc"
            assert screen._updated_dir == "desc"  # other field untouched
            screen.action_flip_sort_dir()
            assert screen._created_dir == "desc"

    def test_flip_sort_dir_noop_in_default(self):
        screen = self._make_screen()
        with patch.object(screen, "_rebuild_table") as rebuild:
            screen.action_flip_sort_dir()
            assert screen._sort_mode == "default"
            rebuild.assert_not_called()

    def test_sort_binding_precedes_status_in_footer(self):
        # #129: the Sort key-hint leads the filter controls in the Footer, so
        # its binding must come before the Status binding (Footer renders in
        # BINDINGS order).
        actions = [b.action for b in DispatchScreen.BINDINGS]
        assert actions.index("toggle_sort") < actions.index("toggle_status")

    def test_S_key_cycles_and_reorders_in_place(self):
        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen._issues = self._dated_issues()
                screen._rebuild_table()
                await pilot.pause()
                assert screen._sort_mode == "default"

                await pilot.press("S")
                await pilot.pause()
                assert screen._sort_mode == "created"
                keys = [k for k in screen._row_issue_keys if k]
                assert keys == ["o/r#2", "o/r#1", "o/r#3"]  # newest created first

                await pilot.press("S")
                await pilot.pause()
                assert screen._sort_mode == "updated"
                keys = [k for k in screen._row_issue_keys if k]
                # No updated_at on these issues ⇒ falls back to default order.
                assert keys == ["o/r#1", "o/r#2", "o/r#3"]

                await pilot.press("S")
                await pilot.pause()
                assert screen._sort_mode == "default"

        asyncio.run(run_test())

    def test_f_key_flips_direction_and_reverses(self):
        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen._issues = self._dated_issues()
                screen._sort_mode = "created"
                screen._rebuild_table()
                await pilot.pause()
                keys = [k for k in screen._row_issue_keys if k]
                assert keys == ["o/r#2", "o/r#1", "o/r#3"]  # desc (newest first)

                await pilot.press("f")  # flip to ascending
                await pilot.pause()
                assert screen._created_dir == "asc"
                keys = [k for k in screen._row_issue_keys if k]
                assert keys == ["o/r#3", "o/r#1", "o/r#2"]  # asc (oldest first)

        asyncio.run(run_test())

    def test_sort_composes_with_search(self):
        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen._issues = [
                    _issue(1, title="alpha one", created_at="2026-05-01T00:00:00Z"),
                    _issue(2, title="beta", created_at="2026-05-20T00:00:00Z"),
                    _issue(3, title="alpha two", created_at="2026-05-15T00:00:00Z"),
                ]
                screen._sort_mode = "created"
                screen._search_query = "alpha"
                screen._rebuild_table()
                await pilot.pause()
                keys = [k for k in screen._row_issue_keys if k]
                # Only the two "alpha" issues survive the search, newest first
                # (#3 May15 before #1 May01) — distinct from the default
                # by-number order, so this proves sort + search compose.
                assert keys == ["o/r#3", "o/r#1"]

        asyncio.run(run_test())

    def test_sort_preserves_selection_and_shows_mode_in_title(self):
        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen._issues = self._dated_issues()
                screen._rebuild_table()
                await pilot.pause()

                await pilot.press("space")  # select the highlighted row
                await pilot.pause()
                selected_before = set(screen._selection.selected)
                assert selected_before

                await pilot.press("S")
                await pilot.pause()
                assert screen._selection.selected == selected_before  # selection survives
                assert "sort: created" in app.sub_title

        asyncio.run(run_test())

    def test_cursor_follows_highlighted_issue_across_sort(self):
        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen._issues = self._dated_issues()
                screen._rebuild_table()
                await pilot.pause()

                await pilot.press("j")  # move highlight to issue #2
                await pilot.pause()
                assert screen._selected_issue_key() == "o/r#2"

                await pilot.press("S")  # created reorders, but cursor tracks #2
                await pilot.pause()
                assert screen._selected_issue_key() == "o/r#2"

        asyncio.run(run_test())

    def test_date_column_appears_only_in_date_modes(self):
        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        def _labels() -> list[str]:
            table = screen.query_one("#issue-table", DataTable)
            return [str(c.label) for c in table.columns.values()]

        async def run_test() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen._issues = self._dated_issues()
                screen._rebuild_table()
                await pilot.pause()
                # Default mode: no date column.
                assert not any(label.startswith(("CREATED", "UPDATED")) for label in _labels())

                await pilot.press("S")  # created
                await pilot.pause()
                # The date column sits at position 1 (right after SEL) so the
                # date reads next to the selector and ahead of priority/repo.
                assert _labels()[0] == "SEL"
                assert _labels()[1].startswith("CREATED")

                await pilot.press("f")  # flip direction → header arrow updates
                await pilot.pause()
                assert _labels()[1] == "CREATED ↑"

                await pilot.press("S")  # updated
                await pilot.pause()
                assert _labels()[1].startswith("UPDATED")

                await pilot.press("S")  # back to default
                await pilot.pause()
                assert not any(label.startswith(("CREATED", "UPDATED")) for label in _labels())
                assert _labels()[:2] == ["SEL", "SCORE"]

        asyncio.run(run_test())


class TestDispatchesCompleteWarnings:
    def _make_screen(self):
        screen = DispatchScreen.__new__(DispatchScreen)
        screen._issues = []
        screen._selection = SelectionState()
        screen._dispatch_guard = DispatchGuard()
        screen._rebuild_table = MagicMock()
        screen.notify = MagicMock()
        screen.post_message = MagicMock()
        return screen

    def test_success_warnings_are_notified(self):
        from agent_hq.dispatch import DispatchResult

        screen = self._make_screen()
        results = [
            DispatchResult(success=True, branch="(via Claude)", warning="cwd fell back to $HOME"),
            DispatchResult(success=True, branch="(via Claude)"),
        ]
        screen._on_dispatches_complete(results)
        warning_calls = [
            c for c in screen.notify.call_args_list if c.kwargs.get("severity") == "warning"
        ]
        assert len(warning_calls) == 1
        assert "cwd fell back" in warning_calls[0].args[0]

    def test_no_warning_toast_when_clean(self):
        from agent_hq.dispatch import DispatchResult

        screen = self._make_screen()
        screen._on_dispatches_complete([DispatchResult(success=True, branch="b")])
        severities = [c.kwargs.get("severity") for c in screen.notify.call_args_list]
        assert "warning" not in severities


class TestDispatchInFlight:
    """#59: optimistic clear, immediate toast, re-entry guard.

    The confirm modal was never a real guard — once dismissed, the screen
    regained focus with the selection intact, so Enter re-opened the modal
    and a second confirm fired a second concurrent dispatch of the same
    batches.
    """

    def _make_screen(self, issues):
        screen = DispatchScreen.__new__(DispatchScreen)
        screen._issues = issues
        screen._selection = SelectionState()
        screen._dispatch_guard = DispatchGuard()
        screen._config = _make_config()
        screen._capabilities_probe = lambda: CLAUDE_ONLY
        screen._show_dispatch_modal = MagicMock()
        screen._execute_all_dispatches = MagicMock()
        screen._rebuild_table = MagicMock()
        screen.notify = MagicMock()
        screen.post_message = MagicMock()
        return screen

    def _messages(self, screen):
        return [c.args[0] for c in screen.notify.call_args_list if c.args]

    def test_selection_clears_when_the_modal_opens(self):
        screen = self._make_screen([_issue(1)])
        screen._selection.toggle("o/r#1")

        screen.action_dispatch()

        screen._show_dispatch_modal.assert_called_once()
        assert screen._selection.selected == set()
        assert screen._dispatch_guard.active is True

    def test_second_enter_while_in_flight_does_not_push_a_second_modal(self):
        screen = self._make_screen([_issue(1), _issue(2)])
        screen._selection.toggle("o/r#1")
        screen.action_dispatch()
        screen.notify.reset_mock()

        screen._selection.toggle("o/r#2")
        screen.action_dispatch()

        screen._show_dispatch_modal.assert_called_once()
        assert any("already running" in m for m in self._messages(screen))
        assert screen._selection.selected == {"o/r#2"}

    def test_cancel_restores_the_full_selection(self):
        screen = self._make_screen([_issue(1), _issue(2)])
        screen._selection.toggle("o/r#1")
        screen._selection.toggle("o/r#2")
        assert screen._selection.group_ungrouped() == ""
        screen.action_dispatch()
        batches = screen._show_dispatch_modal.call_args.args[0]

        screen._on_dispatch_confirmed((False, "claude", None, None), batches)

        assert screen._selection.selected == {"o/r#1", "o/r#2"}
        assert screen._selection.batches == [{"o/r#1", "o/r#2"}]
        assert screen._dispatch_guard.active is False
        screen._execute_all_dispatches.assert_not_called()

    def test_confirm_notifies_immediately(self):
        screen = self._make_screen([_issue(1)])
        screen._selection.toggle("o/r#1")
        screen.action_dispatch()
        batches = screen._show_dispatch_modal.call_args.args[0]
        screen.notify.reset_mock()

        screen._on_dispatch_confirmed((True, "claude", None, None), batches)

        assert any("Dispatching" in m for m in self._messages(screen))
        assert screen._selection.selected == set()

    def test_re_entry_stays_blocked_after_the_modal_is_confirmed(self):
        # The window the issue missed: modal dismissed, worker still running.
        screen = self._make_screen([_issue(1)])
        screen._selection.toggle("o/r#1")
        screen.action_dispatch()
        batches = screen._show_dispatch_modal.call_args.args[0]
        screen._on_dispatch_confirmed((True, "claude", None, None), batches)

        screen._selection.toggle("o/r#1")
        screen.action_dispatch()

        screen._show_dispatch_modal.assert_called_once()
        screen._execute_all_dispatches.assert_called_once()

    def test_no_selection_does_not_engage_the_guard(self):
        screen = self._make_screen([_issue(1)])

        screen.action_dispatch()

        assert screen._dispatch_guard.active is False
        screen._show_dispatch_modal.assert_not_called()

    def test_success_leaves_selection_empty_and_releases_guard(self):
        from agent_hq.dispatch import DispatchResult

        screen = self._make_screen([_issue(1)])
        screen._selection.toggle("o/r#1")
        screen.action_dispatch()
        batches = screen._show_dispatch_modal.call_args.args[0]

        screen._on_dispatches_complete(
            [DispatchResult(success=True, branch="feature/1")],
            group_batches_by_repo(batches),
        )

        assert screen._selection.selected == set()
        assert screen._dispatch_guard.active is False

    def test_partial_failure_restores_only_the_failed_repo_group(self):
        from agent_hq.dispatch import DispatchResult

        screen = self._make_screen([_issue(1, repo="o/a"), _issue(2, repo="o/b")])
        screen._selection.toggle("o/a#1")
        screen._selection.toggle("o/b#2")
        screen.action_dispatch()
        batches = screen._show_dispatch_modal.call_args.args[0]
        groups = group_batches_by_repo(batches)
        results = [
            DispatchResult(success=group[0].issues[0].repo == "o/a", error="boom")
            for group in groups
        ]

        screen._on_dispatches_complete(results, groups)

        assert screen._selection.selected == {"o/b#2"}

    def test_reentry_toast_names_the_running_dispatch(self):
        # The guard check must precede the empty-selection check: begin()
        # already emptied the selection, so checking "selected" first told
        # the user "No issues selected" — i.e. nothing is running — during
        # the exact window #59 is about.
        screen = self._make_screen([_issue(1)])
        screen._selection.toggle("o/r#1")
        screen.action_dispatch()
        screen.notify.reset_mock()

        screen.action_dispatch()

        messages = self._messages(screen)
        assert any("already running" in m for m in messages), messages
        assert not any("No issues selected" in m for m in messages), messages

    def test_all_stale_selection_warns_instead_of_silently_doing_nothing(self):
        # Selected keys that no longer match any live issue build zero
        # batches. Returning silently left the user with a dead Enter and no
        # visible row to deselect.
        screen = self._make_screen([_issue(1)])
        screen._selection.selected = {"o/r#999"}

        screen.action_dispatch()

        assert self._messages(screen), "expected a toast, got silence"
        screen._show_dispatch_modal.assert_not_called()
        assert screen._dispatch_guard.active is False

    def test_cancel_keeps_a_batch_whose_member_vanished(self):
        # The batch is restored from the snapshot, not from batches rebuilt
        # off live issues — otherwise cancelling dissolves the grouping.
        screen = self._make_screen([_issue(1), _issue(2)])
        screen._selection.toggle("o/r#1")
        screen._selection.toggle("o/r#2")
        assert screen._selection.group_ungrouped() == ""
        screen.action_dispatch()
        batches = screen._show_dispatch_modal.call_args.args[0]
        # #1 disappears from the backlog mid-modal.
        screen._issues = [_issue(2)]

        screen._on_dispatch_confirmed((False, "claude", None, None), batches)

        assert screen._selection.selected == {"o/r#2"}
        assert screen._selection.batches == [{"o/r#2"}], "grouping must survive as a pruned batch"

    def test_restore_drops_keys_no_longer_in_the_backlog(self):
        from agent_hq.dispatch import DispatchResult

        screen = self._make_screen([_issue(1)])
        screen._selection.toggle("o/r#1")
        screen.action_dispatch()
        batches = screen._show_dispatch_modal.call_args.args[0]
        groups = group_batches_by_repo(batches)
        # Issue closed elsewhere while the dispatch ran.
        screen._issues = []

        screen._on_dispatches_complete([DispatchResult(success=False, error="boom")], groups)

        assert screen._selection.selected == set(), "a ghost key must not come back"

    def test_launched_failure_is_not_restored(self):
        # A readiness timeout may still create the worktree, so re-selecting
        # it would hand the user a duplicate on the next Enter.
        from agent_hq.dispatch import DispatchResult

        screen = self._make_screen([_issue(1)])
        screen._selection.toggle("o/r#1")
        screen.action_dispatch()
        batches = screen._show_dispatch_modal.call_args.args[0]
        groups = group_batches_by_repo(batches)

        screen._on_dispatches_complete(
            [DispatchResult(success=False, error="did not start", launched=True)], groups
        )

        assert screen._selection.selected == set()
        assert screen._dispatch_guard.active is False

    def test_confirm_callback_raising_still_releases_the_guard(self):
        # begin() runs before push_screen, so a callback that dies would
        # otherwise strand the guard latched and the selection destroyed
        # for the rest of the session. The error still propagates (hiding it
        # would mask a real bug) — but dispatch stays usable.
        import pytest

        screen = self._make_screen([_issue(1)])
        screen._selection.toggle("o/r#1")
        screen.action_dispatch()

        with pytest.raises(ValueError):
            screen._on_dispatch_confirmed("not-a-4-tuple", [])

        assert screen._dispatch_guard.active is False
        assert screen._selection.selected == {"o/r#1"}, "the selection must come back"

    def test_dispatcher_exception_still_releases_the_guard(self, monkeypatch):
        screen = self._make_screen([_issue(1)])
        screen._selection.toggle("o/r#1")
        screen.action_dispatch()
        batches = screen._show_dispatch_modal.call_args.args[0]

        def boom(*args, **kwargs):
            raise RuntimeError("worktree exploded")

        monkeypatch.setattr("agent_hq.dispatch.dispatch_via_claude", boom)
        fake_app = MagicMock()
        fake_app.call_from_thread.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
        with patch.object(type(screen), "app", new_callable=PropertyMock, return_value=fake_app):
            DispatchScreen._execute_all_dispatches.__wrapped__(
                screen, batches, "claude", None, None
            )

        assert screen._dispatch_guard.active is False
        assert screen._selection.selected == {"o/r#1"}
        assert any("worktree exploded" in m for m in self._messages(screen))


class TestCloseNotPlanned:
    """Unit tests for the `D` close-as-not-planned flow (plain seams)."""

    def _make_screen(self):
        screen = DispatchScreen.__new__(DispatchScreen)
        screen._issues = []
        screen._selection = SelectionState()
        screen._active_issues = set()
        screen._row_issue_keys = []
        screen._search_query = ""
        screen._github_cache = MagicMock()
        screen._rebuild_table = MagicMock()
        screen._move_cursor_to = MagicMock()
        screen.notify = MagicMock()
        return screen

    def test_action_pushes_confirm_modal(self):
        # push_screen lives on App, not Screen — patch the real read-only
        # `app` property so this catches a wrong-receiver regression
        # (live smoke hit exactly this: AttributeError on self.push_screen).
        screen = self._make_screen()
        issue = _issue(70)
        screen._get_current_issue = MagicMock(return_value=issue)
        fake_app = MagicMock()
        with patch.object(type(screen), "app", new_callable=PropertyMock, return_value=fake_app):
            screen.action_close_not_planned()
        fake_app.push_screen.assert_called_once()
        modal = fake_app.push_screen.call_args[0][0]
        assert isinstance(modal, ConfirmModal)
        assert "70" in modal._message

    def test_action_noop_without_current_issue(self):
        screen = self._make_screen()
        screen._get_current_issue = MagicMock(return_value=None)
        fake_app = MagicMock()
        with patch.object(type(screen), "app", new_callable=PropertyMock, return_value=fake_app):
            screen.action_close_not_planned()
        fake_app.push_screen.assert_not_called()

    def test_confirm_removes_issue_and_spawns_worker(self):
        screen = self._make_screen()
        issue = _issue(70)
        screen._issues = [issue, _issue(71)]
        screen._close_issue_to_github = MagicMock()
        screen._on_close_confirmed(True, issue)
        assert issue not in screen._issues
        screen._rebuild_table.assert_called_once()
        screen._close_issue_to_github.assert_called_once_with(issue)

    def test_decline_keeps_issue_and_skips_worker(self):
        screen = self._make_screen()
        issue = _issue(70)
        screen._issues = [issue]
        screen._close_issue_to_github = MagicMock()
        screen._on_close_confirmed(False, issue)
        assert issue in screen._issues
        screen._close_issue_to_github.assert_not_called()

    def test_apply_result_success_marks_closed(self):
        screen = self._make_screen()
        issue = _issue(70, repo="ben-ralston/agent-hq")
        screen._apply_close_result(issue, True)
        screen._github_cache.mark_closed.assert_called_once_with("ben-ralston/agent-hq", 70)
        assert screen.notify.call_args.kwargs.get("severity") == "information"

    def test_apply_result_failure_reverts(self):
        screen = self._make_screen()
        issue = _issue(70)
        screen._issues = []  # was optimistically removed
        screen._apply_close_result(issue, False)
        assert issue in screen._issues
        screen._github_cache.mark_closed.assert_not_called()
        assert screen.notify.call_args.kwargs.get("severity") == "error"


class _ClosedFilterScreen(DispatchScreen):
    def _check_gh_auth(self) -> None:  # type: ignore[override]
        return


class TestRefreshHidesClosed:
    def test_refresh_filters_closed_issues(self):
        open_issue = Issue(
            number=1, title="open", repo="o/r", status="Todo", priority=Priority.NEED
        )
        closed_issue = Issue(
            number=2, title="closed", repo="o/r", status="Todo", priority=Priority.NEED
        )
        cache = MagicMock()
        cache.get_issues.return_value = [open_issue, closed_issue]
        cache.get_closed_keys.return_value = {"o/r#2"}

        config = Config(
            repos={"o/r": RepoConfig(owner_name="o/r", local_path=Path("/tmp/o-r"))},
            projects=[ProjectConfig(name="P", number=12, owner="@me")],
            worktree_root=Path("/tmp"),
        )
        screen = _ClosedFilterScreen(config=config, github_cache=cache)
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            with patch(
                "agent_hq.screens.dispatch_screen.detect_active_worktrees",
                return_value=set(),
            ):
                async with app.run_test(size=(120, 40)) as pilot:
                    for _ in range(8):
                        await pilot.pause()
            keys = {i.key for i in screen._issues}
            assert "o/r#1" in keys
            assert "o/r#2" not in keys
            cache.get_closed_keys.assert_called()

        asyncio.run(run_test())


def _chip_backlog():
    """Mixed backlog spanning two repos, two priorities, and three statuses
    (including a status-less issue, which the shared helpers bucket as the
    "none" chip)."""
    return [
        _issue(1, repo="o/alpha", priority=Priority.NEED, status="Todo", title="fix crash"),
        _issue(2, repo="o/alpha", priority=Priority.WANT, status="In Progress", title="polish"),
        _issue(3, repo="o/beta", priority=Priority.NEED, status=None, title="crash on boot"),
    ]


class TestChipFilterState:
    """Issue #86: chip filters (repo / status) on the TUI dispatch
    screen, sharing ``filters.filter_issues`` / ``available_filter_options``
    with the web view so both surfaces behave identically."""

    def _make_screen(self):
        screen = DispatchScreen.__new__(DispatchScreen)
        screen._issues = []
        screen._search_query = ""
        screen._filter_repos = []
        screen._filter_statuses = []
        screen._filter_sentinels = []
        screen._filter_efforts = []
        screen._filter_fitnesses = []
        screen._filter_autopilots = []
        screen._annotations = None
        return screen

    def test_apply_filter_values_sets_and_clears_dimension(self):
        # The palette's live-apply seam: replacing a dimension's chip set
        # turns a filter on, replacing with [] turns it back off.
        screen = self._make_screen()
        screen._issues = _chip_backlog()
        screen._rebuild_table = MagicMock()
        screen._apply_filter_values("status", ["todo", "none"])
        assert screen._filter_statuses == ["todo", "none"]
        screen._apply_filter_values("status", [])
        assert screen._filter_statuses == []

    def test_apply_filter_values_each_dimension(self):
        screen = self._make_screen()
        screen._issues = _chip_backlog()
        screen._rebuild_table = MagicMock()
        screen._apply_filter_values("repo", ["o/alpha"])
        screen._apply_filter_values("status", ["todo"])
        assert screen._filter_repos == ["o/alpha"]
        assert screen._filter_statuses == ["todo"]

    def test_apply_filter_values_unknown_dimension_is_noop(self):
        screen = self._make_screen()
        screen._rebuild_table = MagicMock()
        screen._apply_filter_values("bogus", ["x"])
        assert screen._filter_repos == []
        assert screen._filter_statuses == []
        screen._rebuild_table.assert_not_called()

    def test_visible_issues_unfiltered_passes_all(self):
        screen = self._make_screen()
        screen._issues = _chip_backlog()
        assert [i.number for i in screen.visible_issues()] == [1, 2, 3]

    def test_visible_issues_dimensions_intersect(self):
        # Within a dimension values OR; across dimensions they AND — the
        # ``filter_issues`` contract shared with the web view.
        screen = self._make_screen()
        screen._issues = _chip_backlog()
        screen._filter_statuses = ["todo", "none"]
        assert [i.number for i in screen.visible_issues()] == [1, 3]
        screen._filter_repos = ["o/alpha"]
        assert [i.number for i in screen.visible_issues()] == [1]

    def test_visible_issues_status_none_chip(self):
        screen = self._make_screen()
        screen._issues = _chip_backlog()
        screen._filter_statuses = ["none"]
        assert [i.number for i in screen.visible_issues()] == [3]

    def test_visible_issues_intersects_with_search(self):
        screen = self._make_screen()
        screen._issues = _chip_backlog()
        screen._filter_statuses = ["todo", "none"]
        screen._search_query = "boot"
        assert [i.number for i in screen.visible_issues()] == [3]

    def test_search_matches_body_like_web(self):
        # Routing search through ``filter_issues`` gives the TUI the web's
        # haystack (title + body + repo + number) — body was previously
        # unsearchable in the TUI.
        screen = self._make_screen()
        screen._issues = [_issue(1, title="plain", body="needle in the body")]
        screen._search_query = "needle"
        assert [i.number for i in screen.visible_issues()] == [1]

    def test_empty_message_no_matches_when_chips_active(self):
        screen = self._make_screen()
        screen._filter_repos = ["o/zzz"]
        assert "No matches" in screen._empty_message()

    def test_empty_message_waiting_when_unfiltered(self):
        screen = self._make_screen()
        assert "waiting" in screen._empty_message()

    def test_chip_bar_lists_only_present_values_with_counts(self):
        screen = self._make_screen()
        screen._issues = _chip_backlog()
        markup = screen._chip_bar_markup()
        assert "Need" not in markup
        assert "Want" not in markup
        assert "Idea" not in markup
        assert "alpha 2" in markup
        assert "beta 1" in markup
        assert "Todo 1" in markup
        assert "In Progress 1" in markup
        assert "— 1" in markup  # the status-less bucket

    def test_chip_bar_highlights_active_chips(self):
        screen = self._make_screen()
        screen._issues = _chip_backlog()
        screen._filter_statuses = ["todo", "none"]
        assert "[reverse] Todo 1 [/reverse]" in screen._chip_bar_markup()

    def test_chip_bar_empty_backlog_renders_nothing(self):
        screen = self._make_screen()
        assert screen._chip_bar_markup() == ""

    def test_chip_bar_escapes_markup_in_labels(self):
        # A bracketed status must not be interpreted as Rich markup (it
        # would crash the Static render).
        screen = self._make_screen()
        screen._issues = [_issue(1, status="Todo [urgent]")]
        assert r"Todo \[urgent] 1" in screen._chip_bar_markup()

    def test_filter_binding_registered(self):
        keys = [b.key for b in DispatchScreen.BINDINGS]
        actions = [b.action for b in DispatchScreen.BINDINGS]
        assert "F" in keys
        assert "open_filters" in actions

    def test_refresh_prunes_stale_filter_values(self):
        # A refresh that drops the last issue carrying an active chip value
        # must prune that value — otherwise it keeps filtering invisibly
        # (no chip renders for it) and the palette can't untoggle it.
        screen = self._make_screen()
        screen._filter_statuses = ["todo", "in progress"]
        screen._filter_repos = ["o/alpha"]
        screen._rebuild_table = MagicMock()
        new_backlog = [_issue(9, repo="o/alpha", priority=Priority.WANT, status="In Progress")]
        screen._apply_refresh(new_backlog, set())
        assert screen._filter_statuses == ["in progress"]
        assert screen._filter_repos == ["o/alpha"]

    def test_transient_empty_refresh_keeps_filters(self):
        # An empty refresh (e.g. a failed fetch) must not wipe the user's
        # filters — prune only against a non-empty backlog.
        screen = self._make_screen()
        screen._filter_statuses = ["todo", "none"]
        screen._rebuild_table = MagicMock()
        screen._apply_refresh([], set())
        assert screen._filter_statuses == ["todo", "none"]

    def test_apply_filter_values_prunes_stale_value_from_open_palette(self):
        # A refresh can land while the palette is open; the palette then
        # holds stale options. Applying one must drop values the backlog
        # no longer carries instead of re-arming the invisible filter.
        screen = self._make_screen()
        screen._issues = _chip_backlog()
        screen._rebuild_table = MagicMock()
        screen._apply_filter_values("status", ["todo", "none", "missing"])
        assert screen._filter_statuses == ["todo", "none"]


class TestFilterModal:
    """Pilot tests for the ``F`` filter palette (issue #86)."""

    def test_F_opens_modal_and_toggle_narrows_table(self):
        from agent_hq.screens.dispatch_screen import FilterModal

        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen._issues = _chip_backlog()
                screen._rebuild_table()
                await pilot.pause()

                await pilot.press("F")
                await pilot.pause()
                assert isinstance(app.screen, FilterModal)

                assert not list(app.screen.query("#filter-priority"))
                sel = app.screen.query_one("#filter-status", SelectionList)
                sel.toggle(sel.get_option_at_index(0))  # In Progress sorts first
                await pilot.pause()
                assert screen._filter_statuses == ["in progress"]
                keys = [k for k in screen._row_issue_keys if k]
                assert keys == ["o/alpha#2"]

                await pilot.press("escape")
                await pilot.pause()
                assert app.screen is screen
                # The filter stays active after the palette closes.
                keys = [k for k in screen._row_issue_keys if k]
                assert keys == ["o/alpha#2"]

        asyncio.run(run_test())

    def test_clear_all_restores_full_list(self):
        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen._issues = _chip_backlog()
                screen._filter_statuses = ["todo", "none"]
                screen._filter_repos = ["o/alpha"]
                screen._rebuild_table()
                await pilot.pause()
                assert [k for k in screen._row_issue_keys if k] == ["o/alpha#1"]

                await pilot.press("F")
                await pilot.pause()
                await pilot.press("c")  # clear all filters
                await pilot.pause()
                assert screen._filter_repos == []
                assert screen._filter_statuses == []

                await pilot.press("escape")
                await pilot.pause()
                keys = [k for k in screen._row_issue_keys if k]
                assert keys == ["o/alpha#1", "o/beta#3", "o/alpha#2"]

        asyncio.run(run_test())

    def test_empty_result_renders_no_matches(self):
        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen._issues = _chip_backlog()
                screen._filter_repos = ["o/alpha"]
                screen._search_query = "boot"  # only matches the o/beta issue
                screen._rebuild_table()
                await pilot.pause()
                assert [k for k in screen._row_issue_keys if k] == []
                table = screen.query_one("#issue-table", DataTable)
                row = table.get_row_at(0)
                assert any("No matches" in str(cell) for cell in row)

        asyncio.run(run_test())

    def test_palette_scrolls_on_short_terminal(self):
        # On a short terminal the three chip groups overflow the palette's
        # max-height; the container must scroll so tabbing to the Repo group
        # brings it into view instead of leaving it clipped and invisible.
        screen = _DispatchTestScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            async with app.run_test(size=(100, 24)) as pilot:
                await pilot.pause()
                screen._issues = _chip_backlog()
                screen._rebuild_table()
                await pilot.pause()
                await pilot.press("F")
                await pilot.pause()
                container = app.screen.query_one("#modal-container")
                # Opening must not auto-scroll (the title stays visible even
                # though the first group grabs focus).
                assert container.scroll_offset.y == 0
                await pilot.press("tab")  # Repo → Status
                await pilot.pause()
                assert app.screen.focused.id == "filter-status"

        asyncio.run(run_test())


class TestEmptyProjectsRefresh:
    def test_refresh_with_no_projects_does_not_crash(self):
        """Regression test for the empty-projects refresh crash (#170 audit, AHQ-004).

        A projects.yaml with repos but no projects entries yields
        Config(projects=[]); the real _refresh_data then applies an empty
        backlog. This must not blow up the worker (which would exit the app).
        """

        class _AuthSkipScreen(DispatchScreen):
            def _check_gh_auth(self) -> None:  # type: ignore[override]
                return

        screen = _AuthSkipScreen(config=_make_config(), github_cache=MagicMock())
        app = _DispatchTestApp(screen)

        async def run_test() -> None:
            async with app.run_test(size=(120, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert screen._issues == []
                assert screen._active_issues == set()

        asyncio.run(run_test())


class TestGitHubFetchFailureNotice:
    """The TUI must say "GitHub is unreachable, this is cached data" rather
    than silently showing a stale (or empty) table (#254)."""

    def _screen(self, status):
        screen = DispatchScreen.__new__(DispatchScreen)
        screen._issues = []
        screen._filter_repos = []
        screen._filter_statuses = []
        screen._filter_sentinels = []
        screen._filter_efforts = []
        screen._filter_fitnesses = []
        screen._filter_autopilots = []
        screen._annotations = None
        screen._rebuild_table = MagicMock()
        screen.notify = MagicMock()
        screen._github_cache = MagicMock()
        screen._github_cache.fetch_status.return_value = status
        return screen

    def test_notifies_when_fetch_failed(self):
        from agent_hq.github import FetchStatus

        screen = self._screen(
            FetchStatus(ok=False, message="project @me/4: boom", stale_since=time.time() - 3600)
        )
        screen._apply_refresh([_issue(1)], set())
        assert screen.notify.called
        assert "GitHub unreachable" in screen.notify.call_args.args[0]
        assert screen.notify.call_args.kwargs.get("severity") == "warning"

    def test_silent_when_healthy(self):
        from agent_hq.github import FetchStatus

        screen = self._screen(FetchStatus())
        screen._apply_refresh([_issue(1)], set())
        screen.notify.assert_not_called()

    def test_repeat_failure_notifies_once(self):
        """A 2s poll loop must not stack a toast per tick — notify only when
        the notice text changes."""
        from agent_hq.github import FetchStatus

        status = FetchStatus(ok=False, message="boom", stale_since=1000.0)
        screen = self._screen(status)
        screen._apply_refresh([_issue(1)], set())
        screen._apply_refresh([_issue(1)], set())
        assert screen.notify.call_count == 1

    def test_recovery_clears_notice_state(self):
        from agent_hq.github import FetchStatus

        status = FetchStatus(ok=False, message="boom", stale_since=1000.0)
        screen = self._screen(status)
        screen._apply_refresh([_issue(1)], set())
        screen._github_cache.fetch_status.return_value = FetchStatus()
        screen._apply_refresh([_issue(1)], set())
        screen._github_cache.fetch_status.return_value = status
        screen._apply_refresh([_issue(1)], set())
        assert screen.notify.call_count == 2

    def test_advancing_age_does_not_restack_the_toast(self):
        """The notice embeds relative durations ("from 12m ago · resets in
        5m") that tick with the wall clock, so a persistent outage would
        otherwise toast on every 300s refresh — the exact stacking the dedupe
        is meant to prevent."""
        from agent_hq.github import FetchStatus

        now = time.time()
        screen = self._screen(
            FetchStatus(ok=False, message="boom", rate_limited=True, stale_since=now - 600)
        )
        screen._apply_refresh([_issue(1)], set())
        screen._github_cache.fetch_status.return_value = FetchStatus(
            ok=False, message="boom", rate_limited=True, stale_since=now - 1200
        )
        screen._apply_refresh([_issue(1)], set())
        assert screen.notify.call_count == 1


def test_numeric_priority_keys_are_unbound():
    assert not ({"1", "2", "3", "0"} & {binding.key for binding in DispatchScreen.BINDINGS})


def _ranked_backlog(tmp_path):
    from agent_hq.issues_hub import annotate
    from agent_hq.triage_store import AgentFitness, Effort, TriageRecord, TriageStore

    store = TriageStore(tmp_path)
    issues = [_issue(n) for n in range(1, 6)]
    for n, value in [(1, 2), (2, 4), (4, 5)]:
        key = f"o/r#{n}"
        store.upsert(
            TriageRecord(
                key, value=value, effort=Effort.S, agent_fitness=AgentFitness.SOLO, interest=n == 2
            ),
            actor="ai" if n == 4 else "human",
        )
        if n != 4:
            store.confirm(key, now="2026-09-13T00:00:00+00:00")
    store.upsert(TriageRecord("o/r#3", pin=True), actor="human")
    return issues, store, annotate(issues, store)


def test_rank_sections_and_chips(tmp_path):
    issues, store, annotations = _ranked_backlog(tmp_path)
    rows = build_issue_rows(issues, set(), [], set(), annotations=annotations)
    assert [r["label"] for r in rows if r["is_separator"]] == ["Pinned", "Ranked", "Untriaged"]
    data = [r for r in rows if not r["is_separator"]]
    assert [r["issue_key"] for r in data] == ["o/r#3", "o/r#2", "o/r#1", "o/r#4", "o/r#5"]
    assert data[1]["score"] == "14"
    assert data[1]["chips"] == "V4 · S · solo · ★"
    assert "⌖" in data[0]["chips"]
    assert data[3]["score"] == "14"  # proposed complete, still in Untriaged
    assert all("pri" not in r for r in rows)
    assert not any(
        r["is_separator"]
        for r in build_issue_rows([issues[1]], set(), [], set(), annotations=annotations)
    )


def test_rank_column_resizes(tmp_path):
    issues, store, annotations = _ranked_backlog(tmp_path)
    screen = _DispatchTestScreen(
        config=_make_config(), github_cache=MagicMock(), triage_store=store
    )
    app = _DispatchTestApp(screen)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            screen._apply_refresh(issues, set(), annotations)
            await pilot.pause()
            table = screen.query_one(DataTable)
            assert [str(c.label) for c in table.columns.values()][:3] == ["SEL", "SCORE", "RANK"]
            await pilot.resize_terminal(80, 40)
            await pilot.pause()
            assert "RANK" not in [str(c.label) for c in table.columns.values()]
            assert "SCORE" in [str(c.label) for c in table.columns.values()]
            assert all(len(table.get_row_at(n)) == 6 for n in range(table.row_count))
            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            assert "RANK" in [str(c.label) for c in table.columns.values()]

    asyncio.run(exercise())


@pytest.mark.parametrize("with_store", [False, True])
def test_refresh_reads_annotations_in_worker(tmp_path, with_store):
    import threading

    from agent_hq.screens import dispatch_screen as ds

    issues, store, _ = _ranked_backlog(tmp_path)
    issues += [_issue(6, status="Done"), _issue(7)]
    cache = MagicMock()
    cache.get_issues.return_value = issues + [issues[0]]
    cache.get_closed_keys.return_value = {"o/r#7"}
    cfg = _make_config()
    cfg.projects = [ProjectConfig(name="Test", number=1, owner="o")]
    screen = DispatchScreen(cfg, cache, triage_store=store if with_store else None)
    read_threads = []
    original = store.all_records

    def read(**kwargs):
        read_threads.append(threading.get_ident())
        return original(**kwargs)

    store.all_records = read
    fake_app = MagicMock()
    with (
        patch.object(DispatchScreen, "app", new=fake_app),
        patch.object(ds, "detect_active_worktrees", return_value=set()),
    ):
        worker = threading.Thread(target=DispatchScreen._refresh_data.__wrapped__, args=(screen,))
        worker.start()
        worker.join()
    fn, loaded, active, annotations = fake_app.call_from_thread.call_args.args
    assert fn == screen._apply_refresh
    assert [i.number for i in loaded] == [1, 2, 3, 4, 5]
    if with_store:
        assert annotations["o/r#2"].score == 14
        assert len(read_threads) == 1 and read_threads[0] != threading.get_ident()
    else:
        assert annotations is None and not read_threads


def test_dimension_palette_order_and_filtering(tmp_path):
    issues, store, annotations = _ranked_backlog(tmp_path)
    from agent_hq.issues_hub import annotate
    from agent_hq.triage_store import AgentFitness, Effort, TriageRecord

    issues[0].labels = ["sentinel"]
    issues.append(_issue(6))
    store.upsert(
        TriageRecord("o/r#6", effort=Effort.M, agent_fitness=AgentFitness.PAIR), actor="human"
    )
    annotations = annotate(issues, store)
    screen = _DispatchTestScreen(_make_config(), MagicMock(), triage_store=store)
    app = _DispatchTestApp(screen)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            screen._apply_refresh(issues, set(), annotations)
            await pilot.press("F")
            await pilot.pause()
            assert [w.id for w in app.screen.query(SelectionList)] == [
                "filter-repo",
                "filter-sentinel",
                "filter-status",
                "filter-effort",
                "filter-fitness",
            ]
            effort = app.screen.query_one("#filter-effort", SelectionList)
            effort.select("s")
            await pilot.pause()
            assert {i.number for i in screen.visible_issues()} == {1, 2, 4}
            effort.deselect_all()
            effort.select("none")
            await pilot.pause()
            assert {i.number for i in screen.visible_issues()} == {3, 5}
            await pilot.press("escape")
            assert screen._has_active_filters()
            screen._search_query = "5"
            assert [i.number for i in screen.visible_issues()] == [5]

    asyncio.run(exercise())


@pytest.mark.parametrize("mixed", [False, True])
def test_palette_omits_unavailable_dimensions(mixed):
    screen = _DispatchTestScreen(_make_config(), MagicMock())
    app = _DispatchTestApp(screen)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            issues = [_issue(1), _issue(2, labels=["sentinel"] if mixed else [])]
            screen._apply_refresh(issues, set())
            await pilot.press("F")
            await pilot.pause()
            expected = (
                ["filter-repo", "filter-sentinel", "filter-status"]
                if mixed
                else [
                    "filter-repo",
                    "filter-status",
                ]
            )
            assert [w.id for w in app.screen.query(SelectionList)] == expected

    asyncio.run(exercise())


def test_shared_filter_group_order_and_dimension_pruning(tmp_path):
    from agent_hq.screens.dispatch_screen import _FILTER_GROUPS

    issues, store, annotations = _ranked_backlog(tmp_path)
    issues[0].labels = ["sentinel"]
    screen = _DispatchTestScreen(_make_config(), MagicMock(), triage_store=store)
    screen._issues, screen._annotations = issues, annotations
    markup = screen._chip_bar_markup()
    labels = [group[3] for group in _FILTER_GROUPS]
    assert labels == ["Repo", "Src", "Status", "Eff", "Fit", "Auto"]
    # Nothing is flagged for autopilot here, so its group renders no chips.
    assert "[b]Auto[/b]" not in markup
    positions = [markup.index(f"[b]{label}[/b]") for label in labels[:-1]]
    assert positions == sorted(positions)
    screen._filter_efforts = ["s", "l"]
    screen._filter_fitnesses = ["solo"]
    assert screen._has_active_filters()
    screen._prune_stale_filters()
    assert screen._filter_efforts == ["s"]
    screen._filter_sentinels = ["sentinel"]
    assert [i.number for i in screen.visible_issues()] == [1]


@pytest.mark.parametrize("empty", [False, True])
def test_initial_resize_waits_for_first_refresh(tmp_path, empty):
    issues, store, annotations = _ranked_backlog(tmp_path)
    screen = _DispatchTestScreen(
        config=_make_config(), github_cache=MagicMock(), triage_store=store
    )
    app = _DispatchTestApp(screen)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            table = screen.query_one(DataTable)
            await pilot.resize_terminal(80, 40)
            await pilot.pause()
            assert table.row_count == 0  # Initial fetch has not returned, not an empty result.
            screen._apply_refresh([] if empty else issues, set(), {} if empty else annotations)
            await pilot.pause()
            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            assert "RANK" in [str(c.label) for c in table.columns.values()]
            if empty:
                assert table.row_count == 1
                assert "No issues" in str(table.get_row_at(0))
            else:
                assert table.row_count > 1
                assert "o/r#1" in screen._row_issue_keys

    asyncio.run(exercise())


def test_issue_table_readability_preserves_literal_titles(tmp_path):
    issues, store, annotations = _ranked_backlog(tmp_path)
    issues[0].title = "Literal [red] title"
    screen = _DispatchTestScreen(
        config=_make_config(), github_cache=MagicMock(), triage_store=store
    )
    app = _DispatchTestApp(screen)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            screen._selection.selected.add(issues[0].key)
            screen._apply_refresh(issues, {issues[0].key}, annotations)
            await pilot.pause()
            table = screen.query_one(DataTable)
            headers = [
                table.get_row_at(n)[4]
                for n, key in enumerate(screen._row_issue_keys)
                if key is None and table.get_row_at(n)[4]
            ]
            assert [h.plain for h in headers] == [
                "─ Pinned (1) ─",
                "─ Ranked (2) ─",
                "─ Untriaged (2) ─",
            ]
            assert screen._row_issue_keys == [
                None,
                "o/r#3",
                None,
                None,
                "o/r#2",
                "o/r#1",
                None,
                None,
                "o/r#4",
                "o/r#5",
            ]
            for n in (2, 6):
                assert all(cell == "" for cell in table.get_row_at(n))
            table.move_cursor(row=1)
            await pilot.press("down")
            assert table.cursor_row == 4
            await pilot.press("up")
            assert table.cursor_row == 1
            row = table.get_row(issues[0].key)
            assert row[0].style == "bold cyan"
            assert "Literal [red] title" in row[4].plain
            assert row[4].style == "bold"
            assert row[-1].style == "bold green"
            await pilot.resize_terminal(80, 40)
            await pilot.pause()
            assert "Literal [red] title" in table.get_row(issues[0].key)[3].plain

    asyncio.run(exercise())


def test_autopilot_rows_get_violet_glyph(tmp_path):
    # #302 follow-up: flagged issues carry the ⏵ autopilot glyph in the
    # autopilot violet; unflagged rows carry neither.
    from agent_hq.issues_hub import annotate
    from agent_hq.screens.dispatch_screen import AUTOPILOT_TUI_COLOR
    from agent_hq.triage_store import TriageRecord

    issues, store, _ = _ranked_backlog(tmp_path)
    store.upsert(TriageRecord("o/r#2", autopilot=True), actor="human")
    rows = build_issue_rows(issues, set(), [], set(), annotations=annotate(issues, store))
    by_key = {r["issue_key"]: r for r in rows if not r["is_separator"]}
    assert f"[{AUTOPILOT_TUI_COLOR}]⏵[/]" in by_key["o/r#2"]["issue"]
    assert "⏵" not in by_key["o/r#1"]["issue"]


def test_palette_autopilot_group_filters(tmp_path):
    from agent_hq.issues_hub import annotate
    from agent_hq.triage_store import TriageRecord

    issues, store, _ = _ranked_backlog(tmp_path)
    store.upsert(TriageRecord("o/r#2", autopilot=True), actor="human")
    annotations = annotate(issues, store)
    screen = _DispatchTestScreen(_make_config(), MagicMock(), triage_store=store)
    app = _DispatchTestApp(screen)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            screen._apply_refresh(issues, set(), annotations)
            await pilot.press("F")
            await pilot.pause()
            ids = [w.id for w in app.screen.query(SelectionList)]
            assert ids[-1] == "filter-autopilot"
            app.screen.query_one("#filter-autopilot", SelectionList).select("on")
            await pilot.pause()
            assert [i.number for i in screen.visible_issues()] == [2]

    asyncio.run(exercise())
