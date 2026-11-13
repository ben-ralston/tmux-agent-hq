"""Dispatch screen — cross-repo issue backlog with selection and dispatch."""

import webbrowser
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from rich.markup import escape
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Select,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from ..capabilities import LauncherCapabilities, probe_launcher_capabilities, supports_harness
from ..config import EFFORT_ORDER, Config
from ..filters import (
    SORT_MODES,
    available_filter_options,
    filter_issues,
    format_relative_age,
    format_stale_notice,
    sort_issues,
)
from ..github import (
    GitHubCache,
    close_issue_not_planned,
    detect_active_worktrees,
    invalidate_worktree_cache,
)
from ..issues_hub import (
    IssueAnnotation,
    annotate,
    dimension_filter_options,
    filter_by_dimensions,
    merge_issue_lists,
    queue_partition,
)
from ..models import Issue
from ..triage_store import TriageStore, load_rank_weights
from ._dispatch_common import (
    DispatchGuard,
    GgChordTracker,
    SelectionState,
    SkipRowTable,
    failed_unit_keys,
)

# The autopilot glyph's violet (#302). The neon theme's ``--autopilot`` value,
# not the default theme's #a78bfa: the brighter shade reads better on dark
# terminals.
AUTOPILOT_TUI_COLOR = "#b98cff"

# One order for the palette, chip bar and filter state:
# (dimension, screen attribute, palette label, short label, options key).
_FILTER_GROUPS = (
    ("repo", "_filter_repos", "Repo", "Repo", "repos"),
    ("sentinel", "_filter_sentinels", "Source", "Src", "sentinels"),
    ("status", "_filter_statuses", "Status", "Status", "statuses"),
    ("effort", "_filter_efforts", "Effort", "Eff", "efforts"),
    ("fitness", "_filter_fitnesses", "Fitness", "Fit", "fitnesses"),
    ("autopilot", "_filter_autopilots", "Autopilot", "Auto", "autopilots"),
)


@dataclass
class DispatchBatch:
    """A batch of issues to dispatch together."""

    issues: list[Issue]
    branch: str


class DispatchComplete(Message):
    """Posted when dispatch completes, to trigger agent refresh."""

    pass


class ConfirmDispatchModal(ModalScreen[tuple[bool, str, str | None, str | None]]):
    """Modal showing dispatch plan for confirmation, plus harness/effort/model
    pickers (#205 Stage 1B Task 16).

    Dismisses with ``(confirmed, harness, model, effort)``. Harness/effort/
    model defaults and codex availability are computed once at construction
    from ``config.dispatch`` and an injected capabilities probe -- never
    re-probed mid-modal, and never shelling out in tests thanks to the
    injectable ``capabilities_probe`` seam (mirrors the web's
    ``app.state.capabilities_probe``, see ``web/routes/dispatch.py``
    ``_dispatch_form_context``).
    """

    CSS = """
    ConfirmDispatchModal {
        align: center middle;
    }

    #modal-container {
        width: 60;
        max-height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }

    #modal-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    .batch-info {
        margin-bottom: 1;
    }

    #harness-select, #effort-select, #model-input {
        margin-bottom: 1;
    }

    #button-row {
        margin-top: 1;
        align: center middle;
    }

    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        batches: list[DispatchBatch],
        config: Config | None,
        capabilities_probe: Callable[[], LauncherCapabilities] = probe_launcher_capabilities,
    ):
        super().__init__()
        self._batches = batches
        self._config = config

        self._codex_available = supports_harness(capabilities_probe(), "codex")

        default_harness = config.dispatch.default_harness if config is not None else "claude"
        if default_harness == "codex" and not self._codex_available:
            default_harness = "claude"
        self._default_harness = default_harness

        harness_defaults = (
            config.dispatch.harnesses.get(default_harness) if config is not None else None
        )
        self._default_effort = harness_defaults.effort if harness_defaults else None
        self._default_model = harness_defaults.model if harness_defaults else None

        self._harness_options: list[tuple[str, str]] = [("Claude", "claude")]
        if self._codex_available:
            self._harness_options.append(("Codex", "codex"))

    def compose(self) -> ComposeResult:
        total_wt = len(self._batches)
        with Vertical(id="modal-container"):
            yield Static(
                f"Dispatch {sum(len(b.issues) for b in self._batches)} issues via Claude?",
                id="modal-title",
            )
            for idx, batch in enumerate(self._batches):
                issue_nums = "/".join(f"#{i.number}" for i in batch.issues)
                repo = batch.issues[0].repo_short
                wt_nums = "/".join(str(i.number) for i in batch.issues)
                yield Static(
                    f"  {repo} {issue_nums}\n    Will send /worktree-create {wt_nums}",
                    classes="batch-info",
                    id=f"batch-{idx}",
                )
            yield Static(f"  Total: {total_wt} worktrees via Claude")
            yield Select(
                self._harness_options,
                value=self._default_harness,
                allow_blank=False,
                id="harness-select",
            )
            # Effort/model do NOT pre-fill as explicit values (multi-review
            # fix wave, Fix 5): an explicit value becomes an override that
            # survives a harness switch, fighting the server-side per-
            # harness default resolution an empty field already gets. The
            # config default is surfaced only via the model input's
            # placeholder; the harness select above is the one safe prefill
            # since it's a genuine harness choice, not a value that competes
            # with per-harness resolution.
            yield Select(
                [(effort, effort) for effort in EFFORT_ORDER],
                value=Select.NULL,
                id="effort-select",
            )
            yield Input(
                value="",
                placeholder=(
                    f"model (default: {self._default_model})"
                    if self._default_model
                    else "model (default)"
                ),
                id="model-input",
            )
            with Horizontal(id="button-row"):
                yield Button("Confirm", variant="success", id="confirm-btn")
                yield Button("Cancel", variant="default", id="cancel-btn")

    def _selection(self) -> tuple[str, str | None, str | None]:
        harness = self.query_one("#harness-select", Select).value
        effort_val = self.query_one("#effort-select", Select).value
        effort = effort_val if effort_val not in (None, Select.NULL) else None
        model_val = self.query_one("#model-input", Input).value.strip()
        model = model_val or None
        return harness, model, effort

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-btn":
            harness, model, effort = self._selection()
            self.dismiss((True, harness, model, effort))
        else:
            self.dismiss((False, self._default_harness, None, None))

    def action_cancel(self) -> None:
        self.dismiss((False, self._default_harness, None, None))


class ConfirmModal(ModalScreen[bool]):
    """Generic yes/no confirmation. Dismisses with True (confirm) or False."""

    CSS = """
    ConfirmModal {
        align: center middle;
    }

    #modal-container {
        width: 60;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }

    #modal-title {
        text-align: center;
        margin-bottom: 1;
    }

    #button-row {
        margin-top: 1;
        align: center middle;
    }

    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, message: str):
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-container"):
            yield Static(self._message, id="modal-title")
            with Horizontal(id="button-row"):
                yield Button("Confirm", variant="warning", id="confirm-btn")
                yield Button("Cancel", variant="default", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-btn")

    def action_cancel(self) -> None:
        self.dismiss(False)


class FilterModal(ModalScreen[None]):
    """Chip-filter palette (#86): one multi-select list per dimension.

    Toggles apply live via ``on_change(dimension, values)`` so the table
    behind the modal narrows as chips flip; closing just dismisses. The
    chip set comes from ``available_filter_options`` over the full
    backlog — same dynamic rule as the web filter bar (#40).
    """

    CSS = """
    FilterModal {
        align: center middle;
    }

    #modal-container {
        width: 60;
        max-height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
        /* Three chip groups can outgrow a short terminal — scroll rather
           than clip, so tabbing into the Repo group stays visible. */
        overflow-y: auto;
    }

    #modal-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    .filter-group-label {
        text-style: bold;
    }

    FilterModal SelectionList {
        height: auto;
        max-height: 8;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("c", "clear_all", "Clear all"),
    ]

    # Disable the stock auto-focus: it scrolls the focused group into view
    # during compose, which pushes the title off on short terminals.
    # ``on_mount`` focuses the first group without scrolling instead.
    AUTO_FOCUS = ""

    def __init__(self, options: dict, active: dict, on_change) -> None:
        super().__init__()
        self._options = options
        self._active = active
        self._on_change = on_change

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-container"):
            yield Static("Filter issues", id="modal-title")
            for dimension, _, label, _, options_key in _FILTER_GROUPS:
                if not self._options.get(options_key):
                    continue
                yield Static(label, classes="filter-group-label")
                yield SelectionList(
                    *[
                        Selection(
                            f"{chip['label']} ({chip['count']})",
                            chip["id"],
                            chip["id"] in self._active.get(dimension, []),
                        )
                        for chip in self._options.get(options_key, [])
                    ],
                    id=f"filter-{dimension}",
                )
            yield Static("[dim]space toggle · tab next group · c clear · esc close[/dim]")

    def on_mount(self) -> None:
        lists = self.query(SelectionList)
        if lists:
            # scroll_visible=False: auto-scrolling the focused first group
            # into view would scroll the title off on short terminals.
            lists.first().focus(scroll_visible=False)

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        dimension = (event.selection_list.id or "").removeprefix("filter-")
        self._on_change(dimension, list(event.selection_list.selected))

    def action_clear_all(self) -> None:
        for selection_list in self.query(SelectionList):
            selection_list.deselect_all()

    def action_close(self) -> None:
        self.dismiss(None)


def build_issue_rows(
    issues: list[Issue],
    selected_keys: set[str],
    grouped_batches: list[set[str]],
    active_issues: set[str],
    configured_repos: set[str] | None = None,
    sort_mode: str = "default",
    descending: bool = True,
    annotations: dict[str, IssueAnnotation] | None = None,
) -> list[dict]:
    """Build Queue-ranked rows, or a flat date/storeless list."""
    section_starts = {}
    if sort_mode == "default" and annotations is not None:
        sections = [
            (label, items)
            for label, items in zip(
                ("Pinned", "Ranked", "Untriaged"), queue_partition(issues, annotations)
            )
            if items
        ]
        sorted_issues = [issue for _, items in sections for issue in items]
        if len(sections) > 1:
            section_starts = {items[0].key: (label, len(items)) for label, items in sections}
    else:
        sorted_issues = sort_issues(issues, sort_by=sort_mode, descending=descending)
    rows = []
    for issue in sorted_issues:
        if issue.key in section_starts:
            label, count = section_starts[issue.key]
            rows.append({"is_separator": True, "label": label, "count": count})
        ann = (annotations or {}).get(issue.key, IssueAnnotation())
        chips = " · ".join(label for label, _ in ann.breakdown) if ann.score is not None else ""
        if ann.pinned:
            chips = f"⌖ {chips}".rstrip()

        sel = ""
        if issue.key in selected_keys:
            sel = "*"
            for idx, batch in enumerate(grouped_batches):
                if issue.key in batch:
                    sel = f"G{idx + 1}"
                    break

        repo_display = issue.repo_short
        if configured_repos is not None and issue.repo not in configured_repos:
            repo_display = f"[dim]{issue.repo_short} (no clone)[/dim]"

        issue_cell = f"#{issue.number} {escape(issue.title)}"
        if ann.autopilot:
            # Opted in to the nightly autopilot (#302) — the violet ⏵.
            issue_cell = f"[{AUTOPILOT_TUI_COLOR}]⏵[/] {issue_cell}"
        if issue.is_sentinel:
            # Machine-filed by the log sentinel, untriaged (#235) — display only.
            issue_cell = f"[dark_orange]⚑ {issue_cell}[/dark_orange]"

        rows.append(
            {
                "is_separator": False,
                "issue_key": issue.key,
                "sel": sel,
                "score": str(ann.score) if ann.score is not None else "—",
                "chips": chips,
                "repo": repo_display,
                "issue": issue_cell,
                "status": issue.status or "—",
                # Relative age of the active sort field — rendered in the date
                # column only in date sort modes (see ``_update_table``).
                # Empty for undated issues / non-date modes.
                "date": (
                    format_relative_age(issue.timestamp_for(sort_mode))
                    if sort_mode in ("created", "updated")
                    else ""
                ),
                "active": "WT" if issue.key in active_issues else "",
            }
        )
    return rows


def group_batches_by_repo(batches: list[DispatchBatch]) -> list[list[DispatchBatch]]:
    """Group dispatch batches by repo, preserving insertion order.

    Each inner list contains batches that share a repo and should be sent
    to one Claude session via a single /worktree-create command. Empty
    batches are skipped.
    """
    repo_groups: dict[str, list[DispatchBatch]] = {}
    for batch in batches:
        if not batch.issues:
            continue
        repo_groups.setdefault(batch.issues[0].repo, []).append(batch)
    return list(repo_groups.values())


def _batch_keys(batches: list[DispatchBatch]) -> set[str]:
    """Every issue key across ``batches``."""
    return {issue.key for batch in batches for issue in batch.issues}


class DispatchScreen(Screen):
    """Issue backlog for dispatching agents to worktrees."""

    # Identity of the last GitHub-unreachable notice shown (#254). Class-level
    # so the announcement path is safe before/without __init__. Deliberately
    # NOT the rendered string: that embeds relative durations ("from 12m ago",
    # "resets in 5m") which advance every refresh, so comparing it would toast
    # on every tick — the stacking this dedupe exists to prevent. A
    # recovery-then-relapse still notifies, because health is part of the key.
    _last_fetch_key: tuple | None = None

    CSS = """
    Screen {
        layout: horizontal;
    }

    #dispatch-panel {
        width: 1fr;
        height: 100%;
    }

    DataTable {
        height: 1fr;
    }

    #filter-bar {
        height: auto;
        padding: 0 1;
        display: none;
    }

    #search-input {
        dock: bottom;
        display: none;
    }

    """

    BINDINGS = [
        Binding("l", "switch_tab", "Agents"),
        Binding("L", "switch_tab", "Agents", show=False),
        Binding("slash", "start_search", "Search"),
        Binding("F", "open_filters", "Filter", show=True),
        Binding("r", "refresh", "Refresh"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("G", "goto_bottom", show=False),
        Binding("space", "toggle_select", "Select", show=False),
        Binding("b", "group", "Batch", show=True),
        Binding("u", "ungroup_sel", "Ungroup", show=True),
        Binding("enter", "dispatch", "Dispatch", show=False),
        Binding("S", "toggle_sort", "Sort", show=True),
        Binding("f", "flip_sort_dir", "Flip dir", show=True),
        Binding("s", "toggle_status", "Status", show=True),
        Binding("d", "mark_done", "Done", show=True),
        Binding("D", "close_not_planned", "Won't Do", show=True),
        Binding("o", "open_issue", "Open", show=True),
    ]

    # Sort cycle for the S key (issue #101). Shared with the web view via
    # ``filters.SORT_MODES`` so both surfaces honour the same contract.
    _SORT_CYCLE = SORT_MODES
    _WIDTH_NORMAL = 100

    def __init__(
        self,
        config: Config | None,
        github_cache: GitHubCache,
        capabilities_probe: Callable[[], LauncherCapabilities] = probe_launcher_capabilities,
        triage_store: TriageStore | None = None,
    ):
        super().__init__()
        self._triage_store = triage_store
        self._annotations: dict[str, IssueAnnotation] | None = None
        self._table_labels: tuple[str, ...] = ()
        self._config = config
        self._github_cache = github_cache
        # Injectable probe (#205 Stage 1B Task 16), forwarded to
        # ConfirmDispatchModal so it never has to shell out itself — and so
        # tests can stub it without touching the real launch.sh.
        self._capabilities_probe = capabilities_probe
        self._issues: list[Issue] = []
        self._loaded = False
        self._selection = SelectionState()
        self._dispatch_guard = DispatchGuard()
        self._active_issues: set[str] = set()
        self._row_issue_keys: list[str | None] = []
        self._search_query = ""
        # Chip filter state (#86) — chip ids per dimension, session-scoped
        # (the web persists its copy in the dispatch_state cookie).
        self._filter_repos: list[str] = []
        self._filter_statuses: list[str] = []
        self._filter_sentinels: list[str] = []
        self._filter_efforts: list[str] = []
        self._filter_fitnesses: list[str] = []
        self._filter_autopilots: list[str] = []
        self._sort_mode = "default"
        # Per-field sort direction (#156): each date field remembers its own
        # "desc" (↓ newest first) / "asc" (↑ oldest first) across S cycles.
        self._created_dir = "desc"
        self._updated_dir = "desc"
        self._gg = GgChordTracker()

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="dispatch-panel"):
            yield Static(id="filter-bar")
            yield SkipRowTable(
                id="issue-table",
                cursor_type="row",
                zebra_stripes=True,
                is_data_row=self._is_data_row,
            )
            yield Input(id="search-input", placeholder="Search issues...")
        yield Footer()

    def _active_dir(self) -> str:
        """Direction of the active date field; 'desc' when not a date sort."""
        if self._sort_mode == "created":
            return self._created_dir
        if self._sort_mode == "updated":
            return self._updated_dir
        return "desc"

    def _date_header(self) -> str:
        """Header text for the date column, e.g. 'CREATED ↓' / 'UPDATED ↑'."""
        arrow = "↓" if self._active_dir() == "desc" else "↑"
        name = "CREATED" if self._sort_mode == "created" else "UPDATED"
        return f"{name} {arrow}"

    def _column_labels(self) -> list[str]:
        """Column set for the current sort mode.

        The date column (CREATED/UPDATED + direction arrow) is present only in
        the date sort modes — it shows the value the rows are ordered by, and is
        hidden in default mode to keep the table compact (#156). It sits at
        position 1 (right after SEL) so the date reads alongside the selector
        instead of trailing the wider title/status columns.
        """
        cols = ["SEL", "SCORE", "REPO", "ISSUE", "STATUS", "ACTIVE"]
        if self._annotations is not None and self.size.width >= self._WIDTH_NORMAL:
            cols.insert(2, "RANK")
        if self._sort_mode in ("created", "updated"):
            cols.insert(1, self._date_header())
        return cols

    def on_mount(self) -> None:
        table = self.query_one("#issue-table", DataTable)
        table.add_columns(*self._column_labels())

        if self._config is None:
            self._show_empty_state(
                "No projects configured. Create ~/.config/agent-hq/projects.yaml to get started."
            )
            return

        self._check_gh_auth()
        self._refresh_data()
        self.set_interval(300.0, self._refresh_data)

    def on_screen_resume(self) -> None:
        """Update subtitle when switching back to this screen."""
        self._update_title()

    def _blank_cells(self, message: str | Text = "") -> list[str | Text]:
        """A full-width row of empty cells with ``message`` in the ISSUE
        column — used for separators and empty/placeholder rows so the cell
        count always matches the current column layout."""
        labels = self._column_labels()
        cells = ["" for _ in labels]
        if message:
            # ISSUE's index shifts when the UPDATED column is inserted, so
            # look it up rather than hard-coding a position.
            cells[labels.index("ISSUE")] = message
        return cells

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted and self._config is not None and self._loaded:
            self.call_after_refresh(self._rebuild_table)

    def _show_empty_state(self, message: str) -> None:
        """Show an empty state message in the table."""
        table = self.query_one("#issue-table", DataTable)
        table.clear()
        self._row_issue_keys = []
        table.add_row(*self._blank_cells(f"[dim]{message}[/dim]"))
        self._row_issue_keys.append(None)

    @work(thread=True)
    def _check_gh_auth(self) -> None:
        """Check if gh CLI is authenticated."""
        import subprocess

        try:
            subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            self.app.call_from_thread(
                self.notify,
                "GitHub CLI not authenticated. Run: gh auth login",
                severity="error",
                timeout=10,
            )
        except FileNotFoundError:
            self.app.call_from_thread(
                self.notify,
                "GitHub CLI (gh) not found. Install from https://cli.github.com",
                severity="error",
                timeout=10,
            )

    # --- Data refresh ---

    @work(exclusive=True, thread=True)
    def _refresh_data(self, force: bool = False) -> None:
        """Fetch issues from all configured projects in a thread.

        ``force`` is the explicit-refresh half of the #255 degrade rule: the
        mount and the 300 s interval leave it False so they stay subject to
        the GraphQL budget gate, while ``r`` passes True because a user at
        the keyboard is deliberately spending budget. Without it a low-
        headroom window refused the one refresh the user actually asked for.
        """
        if self._config is None:
            return

        store = self._triage_store
        if store is not None and not store.data_dir.is_dir():
            self.app.call_from_thread(
                self.notify, "triage.data_dir must be an existing directory", severity="error"
            )
            store = None

        def _fetch_project(proj):
            return self._github_cache.get_issues(proj.number, proj.owner, force)

        fetched = []
        if self._config.projects:
            with ThreadPoolExecutor(max_workers=len(self._config.projects)) as pool:
                futures = [pool.submit(_fetch_project, proj) for proj in self._config.projects]
                fetched = [future.result() for future in as_completed(futures)]
        all_issues = merge_issue_lists(fetched)

        # Same `force` as the boards: `r` must drop an issue closed on GitHub
        # now, not after the closed-keys TTL (#255 review).
        closed_keys = self._github_cache.get_closed_keys({i.repo for i in all_issues}, force)
        all_issues = [i for i in all_issues if i.key not in closed_keys]

        active_issues = detect_active_worktrees(self._config.repos)
        annotations = (
            annotate(all_issues, store, load_rank_weights(store.data_dir)) if store else None
        )
        self.app.call_from_thread(self._apply_refresh, all_issues, active_issues, annotations)

    def _apply_refresh(
        self,
        issues: list[Issue],
        active_issues: set[str],
        annotations: dict[str, IssueAnnotation] | None = None,
    ) -> None:
        """Store issues and rebuild the table (runs on main thread)."""
        self._loaded = True
        self._issues = issues
        self._annotations = annotations
        self._active_issues = active_issues
        self._prune_stale_filters()
        self._rebuild_table()
        self._announce_fetch_status()

    def _announce_fetch_status(self) -> None:
        """Surface a preserved-cache state as a warning toast (#254).

        Without this the table just shows yesterday's backlog (or nothing)
        with no hint that GitHub is down — the failure mode that made a
        rate-limit exhaustion take a manual ``gh api rate_limit`` to
        diagnose. Never raises: a status probe must not break the refresh.
        """
        try:
            status = self._github_cache.fetch_status()
            notice = format_stale_notice(status)
        except Exception:  # pragma: no cover - defensive
            return
        key = (status.ok, status.rate_limited, status.message)
        if key == self._last_fetch_key:
            return
        self._last_fetch_key = key
        if notice:
            self.notify(notice, severity="warning", timeout=10)

    def _prune_stale_filters(self) -> None:
        """Drop active chip values that left the backlog on a refresh.

        A vanished value would keep filtering invisibly — no chip renders
        for it (#40's dynamic-chip rule) and the palette can't untoggle a
        value that is no longer an option, leaving the list stuck on "No
        matches" (the web has the same trap, tracked as #80). An empty
        backlog (transient fetch failure) leaves filters untouched.
        """
        if not self._issues:
            return
        options = self._filter_options()
        for _, attr, _, _, options_key in _FILTER_GROUPS:
            valid = {chip["id"] for chip in options.get(options_key, [])}
            current: list[str] = getattr(self, attr)
            kept = [v for v in current if v in valid]
            if len(kept) != len(current):
                setattr(self, attr, kept)

    # --- Chip filters (#86) ---

    def _filter_options(self) -> dict[str, list[dict]]:
        options = available_filter_options(self._issues)
        if self._annotations is not None:
            options.update(dimension_filter_options(self._issues, self._annotations))
        return options

    def visible_issues(self) -> list[Issue]:
        """Intersect repo/source/status, annotation dimensions, and search."""
        issues = filter_issues(
            self._issues,
            repos=self._filter_repos,
            statuses=self._filter_statuses,
            sentinels=self._filter_sentinels,
            query=self._search_query,
        )
        if self._annotations is not None:
            issues = filter_by_dimensions(
                issues,
                self._annotations,
                efforts=self._filter_efforts,
                fitnesses=self._filter_fitnesses,
                autopilots=self._filter_autopilots,
            )
        return issues

    def _has_active_filters(self) -> bool:
        return any(getattr(self, attr) for _, attr, _, _, _ in _FILTER_GROUPS)

    def _empty_message(self) -> str:
        """Placeholder for an empty table: filtered-empty vs backlog-empty."""
        if self._search_query or self._has_active_filters():
            return "No matches"
        return "No issues found — waiting..."

    def _chip_bar_markup(self) -> str:
        """Render the chip bar: group tags + ``label count`` chips, active
        chips in reverse video. Chip set reflects the full backlog (never a
        chip whose toggle would yield zero issues — #40), so it doubles as
        the visible record of which filters are on. Empty backlog → ``""``.
        """
        options = self._filter_options()
        parts = []
        for _, attr, _, tag, options_key in _FILTER_GROUPS:
            chips = options.get(options_key, [])
            active = getattr(self, attr)
            if not chips:
                continue
            rendered = []
            for chip in chips:
                text = f"{escape(chip['label'])} {chip['count']}"
                rendered.append(f"[reverse] {text} [/reverse]" if chip["id"] in active else text)
            parts.append(f"[b]{tag}[/b] " + "  ".join(rendered))
        return "  [dim]│[/dim]  ".join(parts)

    def _update_chip_bar(self) -> None:
        bar = self.query_one("#filter-bar", Static)
        markup = self._chip_bar_markup()
        bar.display = bool(markup)
        bar.update(markup)

    def action_open_filters(self) -> None:
        """Open the chip-filter palette (F)."""
        self.app.push_screen(
            FilterModal(
                self._filter_options(),
                {
                    dimension: list(getattr(self, attr))
                    for dimension, attr, _, _, _ in _FILTER_GROUPS
                },
                self._apply_filter_values,
            )
        )

    def _apply_filter_values(self, dimension: str, values: list[str]) -> None:
        """Live-apply a palette change: replace one dimension's chip set.

        Prunes afterwards — a refresh landing while the palette is open
        leaves it holding stale options, and applying one would re-arm an
        invisible filter (see ``_prune_stale_filters``).
        """
        attr = next((attr for dim, attr, _, _, _ in _FILTER_GROUPS if dim == dimension), None)
        if attr is None:
            return
        setattr(self, attr, list(values))
        self._prune_stale_filters()
        self._rebuild_table()

    def _rebuild_table(self) -> None:
        """Rebuild the table from current issues, applying chip filters
        and search (#86)."""
        issues = self.visible_issues()

        configured_repos = set(self._config.repos.keys()) if self._config else set()
        rows = build_issue_rows(
            issues,
            selected_keys=self._selection.selected,
            grouped_batches=self._selection.batches,
            active_issues=self._active_issues,
            configured_repos=configured_repos,
            sort_mode=self._sort_mode,
            descending=self._active_dir() == "desc",
            annotations=self._annotations,
        )

        self._update_table(rows)
        self._update_chip_bar()
        self._update_title()

    def action_toggle_sort(self) -> None:
        """Cycle the sort field (default → created → updated → default).

        Keeps the highlighted issue under the cursor across the reorder so
        ``S`` never loses the user's place (handled in ``_update_table``).
        """
        i = self._SORT_CYCLE.index(self._sort_mode)
        self._sort_mode = self._SORT_CYCLE[(i + 1) % len(self._SORT_CYCLE)]
        self._rebuild_table()

    def action_flip_sort_dir(self) -> None:
        """Flip the active date field's sort direction (no-op in default).

        Each date field remembers its own direction (#156), so flipping
        ``created`` doesn't disturb ``updated`` and vice versa.
        """
        if self._sort_mode == "created":
            self._created_dir = "asc" if self._created_dir == "desc" else "desc"
        elif self._sort_mode == "updated":
            self._updated_dir = "asc" if self._updated_dir == "desc" else "desc"
        else:
            return
        self._rebuild_table()

    def _update_title(self) -> None:
        """Update subtitle with issue count and selection state."""
        if self.app.screen is not self:
            return
        parts = ["Dispatch"]
        total = len(self._issues)
        selected = len(self._selection.selected)

        if selected:
            wt = self.worktree_count()
            parts.append(f"Selected: {selected} issues ({wt} worktrees)")
        else:
            parts.append(f"{total} issues")

        if self._search_query:
            parts.append(f'"{self._search_query}"')

        if self._sort_mode != "default":
            arrow = "↓" if self._active_dir() == "desc" else "↑"
            parts.append(f"sort: {self._sort_mode} {arrow}")

        self.app.sub_title = " | ".join(parts)

    def _update_table(self, rows: list[dict]) -> None:
        """Update the DataTable with new row data (runs on main thread)."""
        table = self.query_one("#issue-table", DataTable)
        try:
            current_row = table.cursor_row
        except Exception:
            current_row = 0
        # Remember the highlighted issue so a reorder (e.g. a sort toggle)
        # keeps the cursor on the same issue, not the same row index.
        prev_key = (
            self._row_issue_keys[current_row]
            if 0 <= current_row < len(self._row_issue_keys)
            else None
        )

        # Rebuild columns only when the date column's header (presence, field,
        # or direction arrow) changes — a full column rebuild (the only way to
        # add/remove or relabel a column) also drops the rows, so reuse the
        # cheaper row-only clear otherwise.
        date_mode = self._sort_mode in ("created", "updated")
        labels = tuple(self._column_labels())
        if labels != self._table_labels:
            table.clear(columns=True)
            table.add_columns(*labels)
            self._table_labels = labels
        else:
            table.clear()
        self._row_issue_keys = []

        if not rows:
            table.add_row(*self._blank_cells(f"[dim]{self._empty_message()}[/dim]"))
            self._row_issue_keys.append(None)
            return

        for row_data in rows:
            if row_data["is_separator"]:
                if self._row_issue_keys:
                    table.add_row(*self._blank_cells())
                    self._row_issue_keys.append(None)
                label = row_data.get("label", "")
                color = {"Pinned": "yellow", "Ranked": "cyan", "Untriaged": "magenta"}.get(
                    label, "white"
                )
                header = Text(f"─ {label} ({row_data.get('count', 0)}) ─", style=f"bold {color}")
                table.add_row(*self._blank_cells(header))
                self._row_issue_keys.append(None)
            else:
                cells = [Text(row_data["sel"], style="bold cyan")]
                if date_mode:
                    # The date column slots in right after SEL — see
                    # ``_column_labels``.
                    cells.append(Text(row_data["date"], style="dim"))
                cells.append(
                    Text(row_data["score"], style="bold" if row_data["score"] != "—" else "dim")
                )
                if "RANK" in labels:
                    cells.append(Text(row_data["chips"], style="dim"))
                issue_text = Text.from_markup(row_data["issue"], style="bold")
                issue_text.highlight_regex(r"#\d+", "dim")
                status_color = {"Todo": "dim", "In Progress": "yellow", "Done": "green"}.get(
                    row_data["status"], "white"
                )
                cells.extend(
                    [
                        row_data["repo"],
                        issue_text,
                        Text(row_data["status"], style=status_color),
                        Text(row_data["active"], style="bold green"),
                    ]
                )
                table.add_row(*cells, key=row_data["issue_key"])
                self._row_issue_keys.append(row_data["issue_key"])

        # Prefer landing on the same issue (survives reorders); fall back to
        # the prior row index when that issue is gone (e.g. filtered out).
        target_row = current_row
        if prev_key is not None and prev_key in self._row_issue_keys:
            target_row = self._row_issue_keys.index(prev_key)
        if target_row < table.row_count:
            table.move_cursor(row=target_row)
            self._skip_separator()

    # --- Selection helpers ---

    def _selected_issue_key(self) -> str | None:
        """Get the issue_key of the currently highlighted row."""
        table = self.query_one("#issue-table", DataTable)
        try:
            row_idx = table.cursor_row
            if 0 <= row_idx < len(self._row_issue_keys):
                return self._row_issue_keys[row_idx]
        except Exception:
            pass
        return None

    def _is_data_row(self, row: int) -> bool:
        """True when ``row`` holds an issue (not a separator row)."""
        return 0 <= row < len(self._row_issue_keys) and self._row_issue_keys[row] is not None

    def _skip_separator(self, downward: bool = True) -> None:
        """Move the cursor off a separator row (shared table rule, #261)."""
        self.query_one("#issue-table", SkipRowTable).skip_to_data_row(downward=downward)

    # --- Selection / grouping ---

    def toggle_selection(self, key: str) -> None:
        """Toggle selection state for an issue by key."""
        self._selection.toggle(key)

    def group_selected(self) -> str:
        """Group currently selected (ungrouped) issues into a batch.

        Returns empty string on success, or an error message on failure.
        The cross-repo veto is GitHub-specific, so it stays here and rides
        in as the shared helper's validator.
        """

        def _same_repo(ungrouped: set[str]) -> str:
            repos = {issue.repo for issue in self._issues if issue.key in ungrouped}
            if len(repos) > 1:
                return "Cannot batch: selected issues are from different repos"
            return ""

        return self._selection.group_ungrouped(validate=_same_repo)

    def ungroup(self, key: str) -> None:
        """Dissolve the group containing key. Selections remain."""
        self._selection.ungroup(key)

    def worktree_count(self) -> int:
        """Count how many worktrees will be created from current selections."""
        return len(self._selection.batches) + len(self._selection.ungrouped_selected())

    # --- Navigation ---

    def action_cursor_down(self) -> None:
        # The table action already skips separator rows (#261).
        self.query_one("#issue-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#issue-table", DataTable).action_cursor_up()

    def action_goto_bottom(self) -> None:
        """Go to last row (G in vim)."""
        table = self.query_one("#issue-table", DataTable)
        if table.row_count > 0:
            table.move_cursor(row=table.row_count - 1)
            self._skip_separator(downward=False)

    def _goto_top(self) -> None:
        """Go to first row (gg in vim)."""
        table = self.query_one("#issue-table", DataTable)
        if table.row_count > 0:
            table.move_cursor(row=0)
            self._skip_separator()

    # --- Search ---

    def action_start_search(self) -> None:
        """Show search input and focus it."""
        search = self.query_one("#search-input", Input)
        search.display = True
        search.focus()

    def _close_search(self) -> None:
        """Hide search input and return focus to table."""
        search = self.query_one("#search-input", Input)
        search.display = False
        table = self.query_one("#issue-table", DataTable)
        table.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter table as user types in search."""
        self._search_query = event.value
        self._rebuild_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Close search on Enter — keep the filter active."""
        self._close_search()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row triggers dispatch if issues are selected.

        Stop the event so it does not bubble to AgentDashboard, whose own
        RowSelected handler calls self.exit() and would close the app.
        """
        event.stop()
        self.action_dispatch()

    # --- Key handler ---

    def on_key(self, event: events.Key) -> None:
        """Handle Escape and gg chord."""
        search = self.query_one("#search-input", Input)

        # Escape closes search
        if event.key == "escape" and search.display:
            self._search_query = ""
            search.value = ""
            self._close_search()
            self._rebuild_table()
            event.prevent_default()
            return

        # Skip remaining key handlers when search is active
        if search.display:
            return

        # gg chord: double-tap g within timeout → go to top
        if event.key == "g":
            if self._gg.saw_g():
                self._goto_top()
            event.prevent_default()
            return

    # --- Actions ---

    def action_toggle_select(self) -> None:
        """Toggle-select the current issue."""
        key = self._selected_issue_key()
        if key is None:
            return
        if self._config:
            issue = next((i for i in self._issues if i.key == key), None)
            if issue and issue.repo not in self._config.repos:
                self.notify("Cannot select: repo not configured", severity="warning")
                return
        self.toggle_selection(key)
        self._rebuild_table()

    def action_group(self) -> None:
        """Group selected issues into a batch."""
        error = self.group_selected()
        if not error:
            self._rebuild_table()
            self.notify("Batched selected issues", severity="information", timeout=2)
        else:
            self.notify(error, severity="warning", timeout=3)

    def action_ungroup_sel(self) -> None:
        """Ungroup the currently highlighted issue's batch."""
        key = self._selected_issue_key()
        if key:
            self.ungroup(key)
            self._rebuild_table()

    def _owner_for_project(self, project_number: int) -> str:
        """Look up the owner for a project number from config."""
        if self._config:
            for proj in self._config.projects:
                if proj.number == project_number:
                    return proj.owner
        return "@me"

    def _get_current_issue(self) -> Issue | None:
        """Get the Issue object for the currently highlighted row."""
        key = self._selected_issue_key()
        if key is None:
            return None
        issue = next((i for i in self._issues if i.key == key), None)
        if issue and not issue.item_id:
            self.notify("No project item ID for this issue", severity="warning", timeout=3)
            return None
        return issue

    # --- Status actions ---

    def action_toggle_status(self) -> None:
        """Toggle status between Todo and In Progress."""
        issue = self._get_current_issue()
        if not issue:
            return
        new_status = "In Progress" if issue.status != "In Progress" else "Todo"
        self._set_status(issue, new_status)

    def action_mark_done(self) -> None:
        """Mark the current issue as Done."""
        issue = self._get_current_issue()
        if not issue:
            return
        self._set_status(issue, "Done")

    def _set_status(self, issue: Issue, new_status: str) -> None:
        """Set status on an issue with optimistic update."""
        old_status = issue.status
        if old_status == new_status:
            return
        issue.status = new_status
        self._rebuild_table()
        self._move_cursor_to(issue.key)
        self._sync_status_to_github(issue, old_status, new_status)

    @work(thread=True)
    def _sync_status_to_github(self, issue: Issue, old_status: str | None, new_status: str) -> None:
        """Sync status change to GitHub in background."""
        owner = self._owner_for_project(issue.project_number)
        success = self._github_cache.set_status(issue, owner, new_status)
        if success:
            self.app.call_from_thread(
                self.notify,
                f"#{issue.number} → {new_status}",
                severity="information",
                timeout=2,
            )
        else:
            self.app.call_from_thread(self._revert_status, issue, old_status)

    def _revert_status(self, issue: Issue, old_status: str | None) -> None:
        """Revert status on failure."""
        issue.status = old_status
        self._rebuild_table()
        self._move_cursor_to(issue.key)
        self.notify(f"Failed to update #{issue.number}", severity="error", timeout=3)

    # --- Close as not planned ---

    def action_close_not_planned(self) -> None:
        """Close the current issue with GitHub's `not planned` reason.

        Distinct from `d`/Done: `d` only flips the project board Status field
        and leaves the issue open. This closes the underlying issue (visibly
        different from finished work) and hides it from the backlog.
        """
        issue = self._get_current_issue()
        if not issue:
            return
        self.app.push_screen(
            ConfirmModal(f'Close #{issue.number} as "not planned"?'),
            callback=lambda confirmed: self._on_close_confirmed(confirmed, issue),
        )

    def _on_close_confirmed(self, confirmed: bool, issue: Issue) -> None:
        """Optimistically drop the row, then close on GitHub in a worker."""
        if not confirmed:
            return
        if issue in self._issues:
            self._issues.remove(issue)
        self._rebuild_table()
        self._close_issue_to_github(issue)

    @work(thread=True)
    def _close_issue_to_github(self, issue: Issue) -> None:
        """Close the issue on GitHub in the background."""
        ok = close_issue_not_planned(issue.repo, issue.number)
        self.app.call_from_thread(self._apply_close_result, issue, ok)

    def _apply_close_result(self, issue: Issue, ok: bool) -> None:
        """Confirm the close, or revert the optimistic removal on failure."""
        if ok:
            self._github_cache.mark_closed(issue.repo, issue.number)
            self.notify(
                f"#{issue.number} closed (not planned)",
                severity="information",
                timeout=2,
            )
        else:
            if issue not in self._issues:
                self._issues.append(issue)
            self._rebuild_table()
            self._move_cursor_to(issue.key)
            self.notify(f"Failed to close #{issue.number}", severity="error", timeout=3)

    def action_open_issue(self) -> None:
        """Open the currently highlighted issue on github.com."""
        key = self._selected_issue_key()
        if key is None:
            return
        issue = next((i for i in self._issues if i.key == key), None)
        if issue is None:
            return
        url = f"https://github.com/{issue.repo}/issues/{issue.number}"
        webbrowser.open(url)
        self.notify(f"Opening #{issue.number}", severity="information", timeout=2)

    # --- Cursor tracking ---

    def _move_cursor_to(self, issue_key: str) -> None:
        """Move cursor to the row matching issue_key after a table rebuild."""
        table = self.query_one("#issue-table", DataTable)
        for idx, key in enumerate(self._row_issue_keys):
            if key == issue_key:
                table.move_cursor(row=idx)
                return

    def action_dispatch(self) -> None:
        """Start dispatch flow for selected issues."""
        # Guard check FIRST: begin() has already emptied the selection, so
        # an empty-selection check here would answer a re-entry Enter with
        # "No issues selected" — telling the user nothing is running during
        # exactly the window #59 is about.
        #
        # The modal only blocks re-entry while it is up: once dismissed the
        # screen regains focus, so Enter used to re-open it and a second
        # confirm fired a second concurrent dispatch of the same batches.
        if self._dispatch_guard.active:
            self.notify("Dispatch already running", severity="warning", timeout=2)
            return
        if not self._selection.selected:
            self.notify("No issues selected", severity="warning", timeout=2)
            return

        batches = self._prepare_dispatch()
        if not batches:
            # Every selected key is stale (issue closed / filtered out of the
            # backlog). Say so — a silent return leaves a dead Enter and no
            # visible row to deselect.
            self._selection.clear()
            self._rebuild_table()
            self.notify(
                "Selected issues are no longer in the backlog — selection cleared",
                severity="warning",
                timeout=4,
            )
            return

        # Clear now, not on completion — the modal itself shows what is being
        # dispatched, and a cancel/failure puts the selection back.
        self._dispatch_guard.begin(self._selection)
        self._rebuild_table()
        self._show_dispatch_modal(batches)

    def _prepare_dispatch(self) -> list[DispatchBatch]:
        """Build batches from selections. Pure; runs on the main thread.

        This was a thread worker until #59. ``generate_branch_name`` is plain
        string manipulation and ``_build_batches`` reads ``self._issues`` /
        ``self._selection``, so doing it off-thread bought nothing and raced
        a concurrent ``_refresh_data``.
        """
        from ..dispatch import generate_branch_name

        return [
            DispatchBatch(issues=batch_issues, branch=generate_branch_name(batch_issues))
            for batch_issues in self._build_batches()
        ]

    def _build_batches(self) -> list[list[Issue]]:
        """Convert selections + groups into dispatch batches."""
        batches: list[list[Issue]] = []
        grouped_keys: set[str] = set()

        # Add explicit groups
        for batch_keys in self._selection.batches:
            batch_issues = [i for i in self._issues if i.key in batch_keys]
            if batch_issues:
                batches.append(batch_issues)
                grouped_keys |= batch_keys

        # Add ungrouped selections as individual batches
        for key in self._selection.selected - grouped_keys:
            issue = next((i for i in self._issues if i.key == key), None)
            if issue:
                batches.append([issue])

        return batches

    def _show_dispatch_modal(self, batches: list[DispatchBatch]) -> None:
        """Show the confirmation modal."""
        modal = ConfirmDispatchModal(
            batches, self._config, capabilities_probe=self._capabilities_probe
        )
        self.app.push_screen(
            modal,
            callback=lambda result: self._on_dispatch_confirmed(result, batches),
        )

    def _on_dispatch_confirmed(
        self,
        result: tuple[bool, str, str | None, str | None],
        batches: list[DispatchBatch],
    ) -> None:
        """Handle modal result: (confirmed, harness, model, effort)."""
        try:
            confirmed, harness, model, effort = result
        except (TypeError, ValueError):
            # begin() already cleared the selection and latched the guard, so
            # a callback that dies here would strand dispatch off for the
            # rest of the session. Nothing was launched, so hand the whole
            # selection back — then let the error surface.
            self._release_guard(self._dispatch_guard.snapshot_keys())
            raise
        if not confirmed:
            # Nothing launched — give the whole selection back. Restore from
            # the guard's snapshot, not from ``batches``: those were rebuilt
            # off live issues, so a batch that lost a member would come back
            # too small and be dropped, dissolving the user's grouping.
            self._release_guard(self._dispatch_guard.snapshot_keys())
            return
        self.notify(f"Dispatching {len(batches)} agent(s)…", timeout=3)
        self._execute_all_dispatches(batches, harness, model, effort)

    def _release_guard(self, restore_keys: set[str] | None = None) -> None:
        """Release the in-flight latch, restoring ``restore_keys``, and redraw.

        Restored keys are re-validated against the live backlog: the guard's
        snapshot lives outside ``SelectionState``, so nothing prunes it while
        a dispatch runs and it can otherwise resurrect an issue that has since
        been closed or filtered away.
        """
        self._dispatch_guard.finish(self._selection, restore_keys=restore_keys)
        self._selection.retain({issue.key for issue in self._issues})
        self._rebuild_table()

    @work(thread=True)
    def _execute_all_dispatches(
        self,
        batches: list[DispatchBatch],
        harness: str,
        model: str | None,
        effort: str | None,
    ) -> None:
        """Execute dispatches, one Claude session per repo.

        ``harness``/``model``/``effort`` are resolved values captured at
        dispatch time (from the modal's dismiss result) and passed in
        directly -- this worker thread never reads widget state itself.
        ``capabilities_probe`` (final-review fix, #205 Stage 1B) is threaded
        through to ``dispatch_via_claude``'s own inner capability gate so it
        reuses this screen's already-injected probe (``self._capabilities_probe``)
        instead of re-shelling to launch.sh -- one probe path, deterministic
        tests.
        """
        from ..dispatch import DispatchResult, dispatch_via_claude

        groups = group_batches_by_repo(batches)
        results = []
        for repo_batches in groups:
            try:
                result = dispatch_via_claude(
                    [batch.issues for batch in repo_batches],
                    self._config,
                    harness=harness,
                    model=model,
                    effort=effort,
                    capabilities_probe=self._capabilities_probe,
                )
            except Exception as exc:  # pragma: no cover - covered via monkeypatch
                # One result per group, always: the completion handler
                # releases the in-flight guard, and a crash that skipped it
                # would latch dispatch off for the rest of the session (#59).
                result = DispatchResult(success=False, error=f"Dispatch crashed: {exc}")
            results.append(result)

        if any(r.success or r.launched for r in results):
            # Worktrees were just created — bust the detection cache (#226)
            # so their "active" badges show on the next refresh, not after
            # the 15s TTL. ``launched`` counts too (#59): a failed-but-
            # launched dispatch has already created its worktree.
            invalidate_worktree_cache()
        self.app.call_from_thread(self._on_dispatches_complete, results, groups)

    def _on_dispatches_complete(
        self,
        results: list,
        groups: list[list[DispatchBatch]] | None = None,
    ) -> None:
        """Report dispatch results and refresh."""
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]

        if successes:
            branches = ", ".join(r.branch for r in successes)
            self.notify(
                f"Dispatched {len(successes)} agents: {branches}",
                severity="information",
                timeout=5,
            )
        warnings = [r.warning for r in successes if r.warning]
        if warnings:
            self.notify("; ".join(warnings), severity="warning", timeout=8)
        if failures:
            errors = "; ".join(r.error for r in failures)
            self.notify(f"Failed: {errors}", severity="error", timeout=8)

        # Put back only the repo groups that failed without launching (#59).
        self._release_guard(
            failed_unit_keys(groups or [], results, keys_of=_batch_keys),
        )

        # Trigger agent refresh on the agents screen
        self.post_message(DispatchComplete())

    def action_switch_tab(self) -> None:
        # l / L: Dispatch → Agents. With two tabs forward and back are the
        # same move: back to the default screen at the stack bottom.
        self.app.pop_screen()

    def action_refresh(self) -> None:
        if self._config is not None:
            # Explicit user action: always spend the budget (#255).
            self._refresh_data(force=True)
