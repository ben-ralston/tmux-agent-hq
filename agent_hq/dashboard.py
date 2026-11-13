"""Textual TUI dashboard for Agent HQ."""

import os
import subprocess
import textwrap
import time as _time
from typing import Callable

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Input, RichLog

from .actions import jump_to_pane, kill_agent, rename_session
from .cache import get_agents as get_agents_cached
from .capabilities import LauncherCapabilities, probe_launcher_capabilities
from .daemon import is_daemon_running, start_daemon
from .enrichment import (
    compute_content_fingerprint,
    get_pane_content,
)
from .grouping import filter_agents, group_by_worktree, sort_agents_by_priority
from .models import Agent, AgentState, DevPhase, RepoGroup
from .pinned_store import PinnedSessionsStore, migrate_legacy_pins
from .screens._dispatch_common import SkipRowTable
from .state import load_access_times, load_inactive_state, toggle_inactive
from .transcript_history import (
    HistoryPage,
    TranscriptTurn,
    locate_transcript,
    read_history_page,
)
from .ui_prefs import UiPrefsStore

# NOTE: This module hosts the live `AgentDashboard(App)` that `--dashboard`
# launches. `agent_hq/screens/{app,agents_screen}.py` contains a parallel
# `AgentHQApp(App)` + `AgentsScreen(Screen)` pair that is NOT wired up by
# the CLI. Make UI/feature changes here unless you also update
# `cli.py:run_dashboard` to switch entry points.

# State color mapping for Rich markup
STATE_COLORS = {
    "running": "green",
    "waiting": "yellow",
    "idle": "dim",
    "unknown": "dim",
}

# ANSI dim for muted agents in Rich markup
MUTED_STYLE = "dim"

# Repo column is rendered at a fixed width (see AgentDashboard._COL_FIXED_WIDTH);
# repo-group header text is fit to it so the trailing count stays visible.
_REPO_HEADER_NAME_WIDTH = 14

# Preview panel width steps (fr units for right panel)
_PREVIEW_WIDTHS = ["0fr", "1fr", "2fr", "3fr"]
_DEFAULT_PREVIEW_IDX = 1  # 1fr = ~33% of width


def format_state_cell(state: AgentState) -> str:
    """Format an agent state as a Rich markup string with colored emoji only."""
    color = STATE_COLORS.get(state.value, "dim")
    return f"[{color}]{state.emoji}[/{color}]"


# Phase color mapping for Rich markup
PHASE_COLORS = {
    "design": "magenta",
    "implement": "green",
    "qa": "yellow",
    "review": "blue",
    "ready": "bright_cyan",
    "done": "dim",
    "adhoc": "dim",
}


def format_phase_cell(phase: "DevPhase | None") -> str:
    """Render a DevPhase as a Rich-markup string, or '' when None."""
    if phase is None:
        return ""
    color = PHASE_COLORS.get(phase.value, "white")
    return f"[{color}]{phase.emoji} {phase.label}[/{color}]"


def _agent_repo_name(agent: Agent) -> str:
    """Get display repo name for an agent, falling back to cwd basename."""
    return agent.repo_name or agent.cwd.rsplit("/", 1)[-1] or "—"


def group_agents_by_phase(agents: list[Agent]) -> dict[DevPhase, list[Agent]]:
    """Group agents by DevPhase. Agents with no phase bucket into ADHOC.

    Returned dict preserves the DevPhase enum's declaration order
    (ADHOC, DESIGN, IMPLEMENT, QA, REVIEW, READY, DONE),
    which matches the order used by the kanban view (ad-hoc first,
    then lifecycle phases).
    """
    groups: dict[DevPhase, list[Agent]] = {p: [] for p in DevPhase}
    for agent in agents:
        key = agent.phase if agent.phase is not None else DevPhase.ADHOC
        groups[key].append(agent)
    return groups


def _repo_header_text(name: str, count: int) -> str:
    """Section-header label for a repo group: bold name + agent count.

    The name is truncated to fit the fixed-width Repo column (where the
    header is rendered) so the trailing count is never clipped off.
    """
    from .display import truncate

    suffix = f"  ({count})"
    name_fit = truncate(name, max(1, _REPO_HEADER_NAME_WIDTH - len(suffix)))
    return f"[bold]{name_fit}[/bold]{suffix}"


def _phase_header_text(phase: "DevPhase", count: int) -> str:
    """Section-header label for a phase group: colored emoji + label + count.

    Like ``_repo_header_text``, the label is truncated to fit the fixed-width
    Repo column where the header renders, so the trailing count stays visible
    for long labels (e.g. Implement). The emoji still disambiguates the
    phase when its label is shortened.
    """
    from .display import truncate

    suffix = f"  ({count})"
    color = PHASE_COLORS.get(phase.value, "white")
    # Reserve the emoji (rendered ~2 cells) + a separating space + the suffix.
    budget = max(1, _REPO_HEADER_NAME_WIDTH - 2 - 1 - len(suffix))
    label = phase.label if len(phase.label) <= budget else truncate(phase.label, budget)
    return f"[{color}]{phase.emoji} {label}[/{color}]{suffix}"


def build_table_rows(groups: list[RepoGroup]) -> list[dict]:
    """Build a flat list of table row dicts from grouped agents.

    Each repo group is preceded by a non-selectable header row (bold repo
    name + agent count), mirroring the phase view's section headers. A
    separator row spaces consecutive groups. For ungrouped agents, the per-row
    repo cell falls back to the cwd basename.
    """
    rows = []
    for i, repo_group in enumerate(groups):
        if i > 0:
            rows.append({"is_separator": True})
        agent_count = sum(len(wt.agents) for wt in repo_group.worktrees)
        rows.append(
            {
                "is_separator": False,
                "is_header": True,
                "header_text": _repo_header_text(repo_group.repo_name, agent_count),
                "pane_id": None,
            }
        )
        for worktree in repo_group.worktrees:
            for agent in worktree.agents:
                if repo_group.repo_name == "[ungrouped]":
                    repo = agent.cwd.rsplit("/", 1)[-1] or "—"
                else:
                    repo = repo_group.repo_name
                rows.append(
                    {
                        "state": format_state_cell(agent.state),
                        "repo": repo,
                        "branch": agent.branch or "—",
                        "name": agent.session_slug or "",
                        "type": agent.agent_type.value,
                        "phase": format_phase_cell(agent.phase),
                        "pane_id": agent.pane_id,
                        "is_separator": False,
                        "is_header": False,
                    }
                )
    return rows


def _agent_row(agent: Agent, is_muted: bool) -> dict:
    """Build one standard multi-column agent row dict.

    Shared by ``build_priority_rows`` and ``build_phase_rows`` so both views
    render through ``_row_values`` identically. Muted agents are dimmed.
    """
    repo = _agent_repo_name(agent)
    branch = agent.branch or "—"
    name = agent.session_slug or ""
    return {
        "state": format_state_cell(agent.state),
        "repo": f"[{MUTED_STYLE}]{repo}[/{MUTED_STYLE}]" if is_muted else repo,
        "branch": (f"[{MUTED_STYLE}]{branch}[/{MUTED_STYLE}]" if is_muted else branch),
        "name": (f"[{MUTED_STYLE}]{name}[/{MUTED_STYLE}]" if is_muted else name),
        "type": (
            f"[{MUTED_STYLE}]{agent.agent_type.value}[/{MUTED_STYLE}]"
            if is_muted
            else agent.agent_type.value
        ),
        "phase": format_phase_cell(agent.phase),
        "pane_id": agent.pane_id,
        "is_separator": False,
        "is_header": False,
    }


def _pinned_header_text(count: int) -> str:
    """Section-header label for the pinned group: 📌 + label + count.

    Rendered in the fixed-width Repo column like the other section headers,
    so the trailing count stays visible (#141)."""
    from .display import truncate

    suffix = f"  ({count})"
    label = "📌 Pinned"
    budget = max(1, _REPO_HEADER_NAME_WIDTH - len(suffix))
    if len(label) > budget:
        label = truncate(label, budget)
    return f"[bold yellow]{label}[/bold yellow]{suffix}"


def build_pinned_rows(
    agents: list[Agent],
    inactive_pane_ids: set[str] | None,
    pinned_keys: set[str],
) -> list[dict]:
    """Build the pinned top-section rows: a '📌 Pinned' header followed by the
    pinned agents in the same priority order as every other section (#141).

    Keyed on ``Agent.stable_key`` so a pin survives a tmux pane renumber.
    Also matches ``Agent.legacy_stable_key`` (#205 Task 7) so a pin written
    before a codex agent's thread UUID was captured still applies once
    ``stable_key`` upgrades. Returns ``[]`` when nothing in ``agents`` is
    pinned. Each pinned data row carries a 📌 glyph in its state cell so it
    reads as pinned when scrolling.
    """
    if inactive_pane_ids is None:
        inactive_pane_ids = set()
    pinned = [a for a in agents if a.is_pinned_in(pinned_keys)]
    if not pinned:
        return []

    access_times = load_access_times()
    ordered = sort_agents_by_priority(pinned, access_times, inactive_pane_ids)

    rows: list[dict] = [
        {
            "is_separator": False,
            "is_header": True,
            "header_text": _pinned_header_text(len(ordered)),
            "pane_id": None,
        }
    ]
    for agent in ordered:
        row = _agent_row(agent, agent.pane_id in inactive_pane_ids)
        row["state"] = f"📌 {row['state']}"
        rows.append(row)
    return rows


def _muted_header_text(count: int) -> str:
    """Section-header label for the muted group: 🔇 + label + count (#278).

    Rendered in the fixed-width Repo column like the other section headers,
    so the trailing count stays visible."""
    from .display import truncate

    suffix = f"  ({count})"
    label = "🔇 Muted"
    budget = max(1, _REPO_HEADER_NAME_WIDTH - len(suffix))
    if len(label) > budget:
        label = truncate(label, budget)
    return f"[bold {MUTED_STYLE}]{label}[/bold {MUTED_STYLE}]{suffix}"


def build_muted_rows(
    agents: list[Agent],
    inactive_pane_ids: set[str],
) -> list[dict]:
    """Build the muted bottom-section rows: a '🔇 Muted' header followed by
    the muted agents, dimmed, in the same priority order as every other
    section (#278). The inverse of ``build_pinned_rows``. Returns ``[]``
    when nothing in ``agents`` is muted.
    """
    muted = [a for a in agents if a.pane_id in inactive_pane_ids]
    if not muted:
        return []

    access_times = load_access_times()
    ordered = sort_agents_by_priority(muted, access_times, inactive_pane_ids)

    rows: list[dict] = [
        {
            "is_separator": False,
            "is_header": True,
            "header_text": _muted_header_text(len(ordered)),
            "pane_id": None,
        }
    ]
    for agent in ordered:
        rows.append(_agent_row(agent, True))
    return rows


def cursor_target_row(
    row_pane_ids: list[str | None],
    prev_pane_id: str | None,
    prev_row: int,
) -> int | None:
    """Pick the row the cursor should land on after the table is rebuilt.

    Prefer the previously-selected agent's *new* position so the highlight
    follows an agent that moved — e.g. pin/unpin floats it into (or out of)
    the top section, so pressing P again lands on the same agent and undoes
    the toggle (#141). Fall back to the old row index only when that agent is
    gone from the list (killed/filtered); returns ``None`` when even the old
    index no longer exists (empty or shrunken table).
    """
    if prev_pane_id is not None and prev_pane_id in row_pane_ids:
        return row_pane_ids.index(prev_pane_id)
    if prev_row < len(row_pane_ids):
        return prev_row
    return None


def _tui_session_name() -> str | None:
    """The tmux session the TUI itself runs in, via ``$TMUX_PANE`` (#176).

    ``display-popup`` jobs get ``$TMUX`` but not ``$TMUX_PANE`` (tmux 3.6b),
    so when only ``$TMUX`` is set fall back to a no-target ``display-message``,
    which resolves to the attached client's active session — the one the
    popup was opened over. Returns None outside tmux or when tmux can't be
    queried.
    """
    pane_id = os.environ.get("TMUX_PANE")
    if pane_id:
        cmd = ["tmux", "display-message", "-t", pane_id, "-p", "#{session_name}"]
    elif os.environ.get("TMUX"):
        cmd = ["tmux", "display-message", "-p", "#{session_name}"]
    else:
        return None
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def initial_focus_pane_id(
    agents: list[Agent],
    session_name: str | None,
    access_times: dict[str, float],
    row_pane_ids: list[str | None],
) -> str | None:
    """Pick the agent the cursor should open on in phase view (#176).

    Candidates are agents living in ``session_name`` — the tmux session the
    TUI was launched from — that are actually rendered (present in
    ``row_pane_ids``). The most recently accessed wins; ties fall to table
    order. Returns None when there's no usable signal, leaving the cursor
    at the top as before.
    """
    if session_name is None:
        return None
    candidates = [a for a in agents if a.session_name == session_name and a.pane_id in row_pane_ids]
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda a: (access_times.get(a.pane_id, 0.0), -row_pane_ids.index(a.pane_id)),
    )
    return best.pane_id


def build_priority_rows(
    agents: list[Agent],
    inactive_pane_ids: set[str] | None = None,
) -> list[dict]:
    """Build a flat priority-sorted list of table row dicts.

    Matches fzf sort order: waiting first, then by recent access, inactive last.
    A separator row is inserted between active and muted agents.
    Muted agents are dimmed.
    """
    if inactive_pane_ids is None:
        inactive_pane_ids = set()

    access_times = load_access_times()
    sorted_agents = sort_agents_by_priority(agents, access_times, inactive_pane_ids)

    rows = []
    added_mute_separator = False
    for agent in sorted_agents:
        is_muted = agent.pane_id in inactive_pane_ids

        # Insert separator before first muted agent
        if is_muted and not added_mute_separator and rows:
            rows.append({"is_separator": True})
            added_mute_separator = True

        rows.append(_agent_row(agent, is_muted))
    return rows


def build_phase_rows(
    agents: list[Agent],
    inactive_pane_ids: set[str] | None = None,
) -> list[dict]:
    """Build phase-grouped rows: a non-selectable header row per non-empty
    phase, followed by that phase's agents in priority order.

    Same multi-column row shape as ``build_priority_rows`` so the preview
    panel and responsive columns work identically — the only difference from
    list mode is grouping + header rows (mirrors the web phase view, which
    buckets by ``DevPhase`` after the shared priority sort).
    """
    if inactive_pane_ids is None:
        inactive_pane_ids = set()

    access_times = load_access_times()
    ordered = sort_agents_by_priority(agents, access_times, inactive_pane_ids)
    groups = group_agents_by_phase(ordered)

    rows: list[dict] = []
    first = True
    for phase, members in groups.items():
        if not members:
            continue
        if not first:
            rows.append({"is_separator": True})
        first = False
        rows.append(
            {
                "is_separator": False,
                "is_header": True,
                "header_text": _phase_header_text(phase, len(members)),
                "pane_id": None,
            }
        )
        for agent in members:
            rows.append(_agent_row(agent, agent.pane_id in inactive_pane_ids))
    return rows


# Left bar drawn down the side of user turns (web parity: border-left).
_BAR = "▌ "


def _wrap_user_lines(text: str, width: int) -> list[str]:
    """Pre-wrap a user turn so every physical line can carry the left bar.

    The preview's RichLog wraps by itself, but a wrap done there would put
    continuation lines outside the prefix we prepend — the bar would only
    mark the first line. Wrapping here makes the block's height known, so
    each line gets its own bar segment (matching the web view's
    ``border-left`` spanning the whole message).

    ``> `` rides the first line via ``initial_indent``, so continuation
    lines start at the marker's column exactly as they do on the web.
    Blank lines are preserved so the bar runs through paragraph breaks.
    """
    limit = max(8, width)
    lines: list[str] = []
    for index, logical in enumerate(text.split("\n")):
        indent = "> " if index == 0 else ""
        if not logical.strip():
            lines.append(indent.rstrip())
            continue
        lines.extend(
            textwrap.wrap(logical, width=limit, initial_indent=indent) or [indent.rstrip()]
        )
    return lines or ["> "]


class PreviewPanel(RichLog):
    """Right panel showing selected agent's pane content with ANSI colors."""

    class TopReached(Message):
        """Posted when a mouse wheel-up lands at (or near) the top of the
        scrollback — the history mode's cue to page in earlier turns (#171)."""

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        super()._on_mouse_scroll_up(event)
        if self.scroll_target_y <= 0:
            self.post_message(self.TopReached())

    def __init__(
        self,
        *,
        max_lines: int | None = None,
        min_width: int = 1,
        wrap: bool = False,
        highlight: bool = False,
        markup: bool = False,
        auto_scroll: bool = True,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        # RichLog defaults min_width to 78 columns. In a narrower preview pane,
        # that causes wrapped content to be rendered too wide and then cropped.
        super().__init__(
            max_lines=max_lines,
            min_width=min_width,
            wrap=wrap,
            highlight=highlight,
            markup=markup,
            auto_scroll=auto_scroll,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )


class AgentDashboard(App):
    """Agent HQ persistent TUI dashboard."""

    CSS = """
    Screen {
        layout: horizontal;
    }

    #left-panel {
        width: 2fr;
        height: 100%;
    }

    DataTable {
        height: 1fr;
    }

    #search-input {
        dock: bottom;
        display: none;
    }

    #right-panel {
        width: 1fr;
        height: 100%;
        border-left: solid $surface-lighten-2;
        padding: 0 1;
        overflow-x: hidden;
        overflow-y: auto;
    }
    """

    TITLE = "Agent HQ"

    # Actions only available on the agents view (hidden when dispatch screen is active)
    _AGENTS_ONLY_ACTIONS = frozenset(
        {
            "switch_to_dispatch",
            "jump",
            "toggle_sort",
            "toggle_mute",
            "toggle_pin",
            "confirm_kill",
            "toggle_preview",
            "toggle_history",
            "rename_session",
            "preview_scroll_up",
            "preview_scroll_down",
        }
    )

    BINDINGS = [
        Binding("l", "switch_to_dispatch", "Dispatch"),
        Binding("slash", "start_search", "Search"),
        Binding("enter", "jump", "Jump"),
        Binding("s", "toggle_sort", "Sort"),
        Binding("m", "toggle_mute", "Mute"),
        Binding("P", "toggle_pin", "Pin"),
        Binding("K", "confirm_kill", "Kill"),
        Binding("p", "toggle_preview", "Preview"),
        Binding("h", "toggle_history", "History"),
        Binding("R", "rename_session", "Rename"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("G", "goto_bottom", show=False),
        Binding("ctrl+u", "preview_scroll_up", "Scroll Up", show=False),
        Binding("ctrl+d", "preview_scroll_down", "Scroll Down", show=False),
    ]

    # Timeout for gg chord (seconds)
    _GG_TIMEOUT = 0.4

    # Kill-confirmation window (seconds) — shared by the toast and the
    # auto-clear timer so the visible prompt and the pending state can't
    # drift apart (#14).
    _KILL_CONFIRM_TIMEOUT = 5.0

    # Class-level defaults so partially-constructed instances (tests build
    # via __new__ without __init__) read the safe "no initial focus" state.
    _initial_focus_pending = False
    _own_session_name: str | None = None
    _own_session_resolved = False

    def __init__(
        self,
        agent_filter: str | None = None,
        repo_filter: str | None = None,
        ui_prefs_store: UiPrefsStore | None = None,
        capabilities_probe: Callable[[], LauncherCapabilities] = probe_launcher_capabilities,
    ):
        super().__init__()
        self._agent_filter = agent_filter
        self._repo_filter = repo_filter
        # Injectable probe (Fix 7, multi-review wave), threaded into the
        # Dispatch screen so the whole TUI process shares one capabilities
        # source; warmed off the UI thread on mount (see
        # ``_warm_capabilities_cache``) so ConfirmDispatchModal's own
        # synchronous probe calls hit a warm
        # cache instead of shelling out to launch.sh on the main thread.
        self._capabilities_probe = capabilities_probe
        self._row_pane_ids: list[str | None] = []
        self._agents: list[Agent] = []
        # Sort mode cycled by `s`: restored from the shared prefs store, else
        # phase (default, #127). Persisted on every toggle (#176).
        self._ui_prefs_store = ui_prefs_store or UiPrefsStore()
        self._sort_mode = (
            self._ui_prefs_store.get_tui_sort_mode() or "phase"
        )  # "priority" | "phase" | "repo"
        # One-shot: on the first refresh that actually finds agents, land the
        # cursor on the agent in the TUI's own tmux session — phase view only
        # (#176). Cleared by the first consuming refresh or a manual sort
        # toggle. The tmux lookup is resolved lazily in the refresh worker.
        self._initial_focus_pending = self._sort_mode == "phase"
        self._own_session_name: str | None = None
        self._own_session_resolved = False
        self._inactive_pane_ids: set[str] = set()
        # Pins float to a top section; keyed on Agent.stable_key, shared with
        # the web service via the on-disk store (#141).
        self._pinned_store = PinnedSessionsStore()
        self._pinned_keys: set[str] = set()
        # Monotonic guard: drop _apply_refresh calls whose snapshot timestamp
        # is older than what's already on screen. See issue #74.
        self._last_applied_ts: float = 0.0
        self._search_query = ""
        self._last_g_time: float = 0.0
        self._preview_idx = _DEFAULT_PREVIEW_IDX
        self._pending_kill: str | None = None  # pane_id awaiting kill confirmation
        self._pending_kill_timer: Timer | None = None  # auto-clears it on toast expiry (#14)
        self._preview_pane_id: str | None = None  # pane_id currently shown in preview
        # History mode (#171): preview shows turns paged locally from the
        # agent's transcript JSONL instead of the live capture tail.
        self._history_pane_id: str | None = None  # pane in history mode (None = live)
        self._history_path: str | None = None
        self._history_turns: list[TranscriptTurn] = []
        self._history_cursor: int | None = None
        self._history_has_more = False
        self._history_loading = False
        # Sticky mode (#171 follow-up): h flips this and persists it, and
        # the cursor carries history mode from agent to agent instead of
        # dropping back to Live on every row change. Restored at launch
        # like the sort mode above.
        self._history_sticky = self._ui_prefs_store.get_tui_preview_mode() == "history"
        self._current_cols: tuple[str, ...] = (
            "",
            "Repo",
            "Branch",
            "Phase",
            "Name",
            "Type",
            "Pane",
        )

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left-panel"):
                yield SkipRowTable(
                    id="agent-table",
                    cursor_type="row",
                    zebra_stripes=True,
                    is_data_row=self._is_data_row,
                )
                yield Input(id="search-input", placeholder="Search agents...")
            yield PreviewPanel(id="right-panel", wrap=True, auto_scroll=True, markup=False)
        yield Footer()

    # Width thresholds for responsive layout
    _WIDTH_COMPACT = 60  # Hide Type + Pane columns, no preview
    _WIDTH_NARROW = 80  # Hide preview, show all columns
    _WIDTH_NORMAL = 100  # Show preview, all columns

    def on_mount(self) -> None:
        self._apply_responsive_layout()
        self._refresh_data()
        self.set_interval(2.0, self._refresh_data)

        # Fire the capabilities warmup as early as possible (Fix 7,
        # multi-review wave): it runs in its own thread, so it races
        # harmlessly against the rest of this synchronous setup, but the
        # earlier it starts the more likely the launch.sh probe has already
        # completed (populating the shared in-process TTL cache in
        # capabilities.py) by the time the dispatch confirm dialog makes its
        # own synchronous probe call.
        self._warm_capabilities_cache()

        # Install the dispatch screen for tab switching
        from .config import ConfigError, load_config
        from .github import GitHubCache
        from .screens.dispatch_screen import DispatchScreen

        try:
            config = load_config()
        except ConfigError as e:
            # Don't abort the dashboard — agents view still works without
            # config. The dispatch screen renders an empty-state when config is None.
            config = None
            self.notify(
                f"Config error: {e}. Dispatch tab disabled.",
                severity="error",
                timeout=15,
            )
        github_cache = GitHubCache(graphql_floor=config.github.graphql_floor if config else None)
        from .triage_store import TriageStore

        triage_store = TriageStore(config.triage.data_dir) if config and config.triage else None
        self._dispatch_screen = DispatchScreen(
            config=config,
            github_cache=github_cache,
            triage_store=triage_store,
            capabilities_probe=self._capabilities_probe,
        )
        self.install_screen(self._dispatch_screen, name="dispatch")

    @work(thread=True)
    def _warm_capabilities_cache(self) -> None:
        """Pre-populate the shared launch.sh ``--capabilities`` probe cache
        (Fix 7, multi-review wave) off the main/UI thread.

        ``probe_launcher_capabilities`` caches its result in-process (keyed
        by resolved launcher path, TTL-bound) regardless of which callable
        reference invokes it, so one call here warms the cache for every
        later synchronous call this process makes -- notably
        ``ConfirmDispatchModal.__init__`` (called synchronously whenever the
        user opens the dispatch confirm dialog), which intentionally keeps
        its sync call; only the
        first-ever probe of the process needs to not block a thread the
        user is waiting on.
        """
        self._capabilities_probe()

    def on_resize(self, event: events.Resize) -> None:
        """Adapt layout when terminal is resized."""
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        """Adjust columns and preview based on terminal width.

        The column set depends only on width — every sort mode (priority,
        phase, repo) uses the same columns + preview, so switching sort
        never rebuilds columns (no flash, no lag; issue #97).
        """
        width = self.size.width
        table = self.query_one("#agent-table", DataTable)
        preview = self.query_one("#right-panel", PreviewPanel)

        # Determine column set based on width
        cols: tuple[str, ...]
        if width < self._WIDTH_COMPACT:
            cols = ("", "Repo", "Branch", "Phase")
        elif width < self._WIDTH_NARROW:
            cols = ("", "Repo", "Branch", "Phase", "Name", "Type")
        else:
            cols = ("", "Repo", "Branch", "Phase", "Name", "Type", "Pane")

        # Only rebuild columns if they changed
        current_cols = tuple(col.label.plain for col in table.columns.values())
        if current_cols != cols:
            table.clear(columns=True)
            for label in cols:
                fixed = self._COL_FIXED_WIDTH.get(label)
                if fixed is not None:
                    table.add_column(label, width=fixed)
                else:
                    table.add_column(label)
            self._current_cols = cols
            self._rebuild_table()

        # Preview hidden on narrow terminals (unless user manually toggled)
        hide_preview = width < self._WIDTH_NARROW and self._preview_idx == _DEFAULT_PREVIEW_IDX
        if hide_preview:
            preview.display = False
        elif width >= self._WIDTH_NARROW and not preview.display and self._preview_idx > 0:
            preview.display = True

    # --- Data refresh ---

    @work(exclusive=True, thread=True)
    def _refresh_data(self) -> None:
        """Re-discover agents in a thread, then update UI on main thread."""
        agents, snapshot_ts = get_agents_cached()
        agents = filter_agents(agents, agent_type=self._agent_filter, repo=self._repo_filter)
        # Mute is sticky (#303): only an explicit unmute clears it.
        inactive_records = load_inactive_state()

        inactive_pane_ids = set(inactive_records.keys())
        # Disk I/O for pins stays off the UI thread (mirrors inactive state).
        # Re-key any legacy-form pin whose agent now has a captured stable id
        # (#205 Codex review) before reading the set used for this render.
        migrate_legacy_pins(self._pinned_store, agents)
        pinned_keys = self._pinned_store.keys()
        # The tmux shell-out for the initial focus stays off the UI thread
        # too; cached (including a None result) so it runs at most once.
        if self._initial_focus_pending and not self._own_session_resolved:
            self._own_session_name = _tui_session_name()
            self._own_session_resolved = True
        self.call_from_thread(
            self._apply_refresh, agents, inactive_pane_ids, pinned_keys, snapshot_ts
        )

    def _apply_refresh(
        self,
        agents: list[Agent],
        inactive_pane_ids: set[str],
        pinned_keys: set[str],
        snapshot_ts: float,
    ) -> None:
        """Store agents and rebuild the table (runs on main thread).

        Monotonic guard: ``@work(exclusive=True)`` prevents two refresh
        workers from running concurrently but does NOT prevent reordered
        completion when refreshes are dispatched back-to-back. Drop any
        snapshot whose timestamp is *strictly older* than the last applied
        so a slow earlier refresh can't stomp a fresh later one (issue
        #74). Equal timestamps still apply: action handlers (mute, kill,
        rename) trigger an immediate refresh that often hits the same
        cache timestamp as a queued interval refresh, and we want the
        action's freshly-loaded inactive_records to land.
        """
        if snapshot_ts < self._last_applied_ts:
            return
        self._last_applied_ts = snapshot_ts
        self._agents = agents
        self._inactive_pane_ids = inactive_pane_ids
        self._pinned_keys = pinned_keys
        self._rebuild_table()
        # One-shot initial focus (#176): consumed only by a refresh that found
        # agents, so the pre-discovery placeholder build can't burn it.
        if self._initial_focus_pending and agents:
            self._initial_focus_pending = False
            if self._sort_mode == "phase":
                target = initial_focus_pane_id(
                    agents, self._own_session_name, load_access_times(), self._row_pane_ids
                )
                if target is not None:
                    table = self.query_one("#agent-table", DataTable)
                    table.move_cursor(row=self._row_pane_ids.index(target))
        # Refresh preview for the currently selected agent (skip if hidden)
        preview = self.query_one("#right-panel", PreviewPanel)
        pane_id = self._selected_pane_id()
        if pane_id and preview.display:
            self._load_preview(pane_id)

    def _rebuild_table(self) -> None:
        """Rebuild the table from current agents, applying search filter and sort mode."""
        self._update_table(self._rows_for_display())
        self._update_title()

    def _rows_for_display(self) -> list[dict]:
        """Compute the full row list: a pinned top section (if any) above the
        sort-mode body, a muted bottom section (if any) below it — Done
        included — with both sets removed from the body so they're never
        duplicated (#141, #278). Pinned wins over muted for placement. Pure
        — no DOM access — so it's unit-testable."""
        agents = self._agents

        # Apply search filter
        if self._search_query:
            q = self._search_query.lower()
            agents = [
                a
                for a in agents
                if q in _agent_repo_name(a).lower()
                or q in (a.branch or "").lower()
                or q in a.agent_type.value.lower()
                or q in a.pane_id.lower()
            ]

        pinned_rows = build_pinned_rows(agents, self._inactive_pane_ids, self._pinned_keys)
        rest = (
            [a for a in agents if not a.is_pinned_in(self._pinned_keys)]
            if self._pinned_keys
            else agents
        )
        muted_rows = build_muted_rows(rest, self._inactive_pane_ids)
        rest = [a for a in rest if a.pane_id not in self._inactive_pane_ids]

        if self._sort_mode == "phase":
            body = build_phase_rows(rest, self._inactive_pane_ids)
        elif self._sort_mode == "repo":
            groups = group_by_worktree(rest)
            body = build_table_rows(groups)
        else:
            body = build_priority_rows(rest, self._inactive_pane_ids)

        sections = [rows for rows in (pinned_rows, body, muted_rows) if rows]
        rows: list[dict] = []
        for section in sections:
            if rows:
                rows.append({"is_separator": True})
            rows.extend(section)
        return rows

    _SORT_MODE_LABELS = {
        "priority": "priority",
        "phase": "phase",
        "repo": "grouped",
    }

    def _update_title(self) -> None:
        """Update the title bar with agent count and sort mode."""
        total = len(self._agents)
        mode = self._SORT_MODE_LABELS[self._sort_mode]
        muted = len(self._inactive_pane_ids & {a.pane_id for a in self._agents})
        parts = [f"{total} agents", mode]
        if muted:
            parts.append(f"{muted} muted")
        if self._search_query:
            parts.append(f'"{self._search_query}"')
        self.sub_title = " | ".join(parts)

    # Max display widths per column (truncate cell text with ellipsis beyond this)
    _COL_MAX_WIDTH = {
        "Repo": 14,
        "Branch": 24,
        "Name": 28,
    }

    # Fixed column widths (cells; padding adds ~2 chars). Without these,
    # DataTable auto-sizes to the widest content seen and never shrinks back,
    # so a transiently-long repo name leaves the column wastefully wide.
    _COL_FIXED_WIDTH = {
        "Repo": 14,
    }

    def _row_values(self, row_data: dict) -> tuple[str, ...]:
        """Build a row tuple matching the current column set."""
        from .display import truncate

        # Column key mapping: header label → row_data key
        col_map = {
            "": "state",
            "Repo": "repo",
            "Branch": "branch",
            "Phase": "phase",
            "Name": "name",
            "Type": "type",
            "Pane": "pane_id",
        }
        values = []
        for c in self._current_cols:
            val = row_data.get(col_map.get(c, ""), "")
            max_w = self._COL_MAX_WIDTH.get(c)
            if max_w and isinstance(val, str) and len(val) > max_w:
                # Strip Rich markup before truncating, re-apply if present
                if val.startswith("[dim]") and val.endswith("[/dim]"):
                    inner = val[5:-6]
                    val = f"[dim]{truncate(inner, max_w)}[/dim]"
                else:
                    val = truncate(val, max_w)
            values.append(val)
        return tuple(values)

    def _empty_row(self) -> tuple[str, ...]:
        """Build an empty row matching the current column count."""
        return ("",) * len(self._current_cols)

    def _header_row(self, text: str) -> tuple[str, ...]:
        """Build a section-header row (phase or repo) for the current columns.

        DataTable has no column span, so the styled header text is placed in
        the "Repo" slot (falling back to the first column), with the rest
        blank — analogous to the empty-state message row.
        """
        cells = list(self._empty_row())
        try:
            idx = self._current_cols.index("Repo")
        except ValueError:
            idx = 0
        cells[idx] = text
        return tuple(cells)

    def _update_table(self, rows: list[dict]) -> None:
        """Update the DataTable with new row data (runs on main thread)."""
        table = self.query_one("#agent-table", DataTable)
        try:
            current_row = table.cursor_row
        except Exception:
            current_row = 0
        # Capture the selected agent's identity (not just its row index) so the
        # cursor can follow it if the rebuild moves it — e.g. pin/unpin (#141).
        prev_pane_id = self._selected_pane_id()

        table.clear()
        self._row_pane_ids = []

        if not rows:
            msg = (
                "[dim]No matches[/dim]"
                if self._search_query
                else "[dim]No agents found — waiting...[/dim]"
            )
            empty = self._empty_row()
            # Put message in second column (Repo) if available, else first
            msg_row = list(empty)
            msg_row[min(1, len(msg_row) - 1)] = msg
            table.add_row(*msg_row)
            self._row_pane_ids.append(None)
            return

        for row_data in rows:
            if row_data["is_separator"]:
                table.add_row(*self._empty_row())
                self._row_pane_ids.append(None)
            elif row_data.get("is_header"):
                table.add_row(*self._header_row(row_data["header_text"]))
                self._row_pane_ids.append(None)
            else:
                table.add_row(
                    *self._row_values(row_data),
                    key=row_data["pane_id"],
                )
                self._row_pane_ids.append(row_data["pane_id"])

        target = cursor_target_row(self._row_pane_ids, prev_pane_id, current_row)
        if target is not None:
            table.move_cursor(row=target)
            self._skip_to_selectable(downward=True)

    # --- Selection helpers ---

    def _selected_pane_id(self) -> str | None:
        """Get the pane_id of the currently highlighted row."""
        table = self.query_one("#agent-table", DataTable)
        try:
            row_idx = table.cursor_row
            if 0 <= row_idx < len(self._row_pane_ids):
                return self._row_pane_ids[row_idx]
        except Exception:
            pass
        return None

    def _is_data_row(self, row: int) -> bool:
        """True when ``row`` holds an agent (not a separator/header row)."""
        return 0 <= row < len(self._row_pane_ids) and self._row_pane_ids[row] is not None

    def _skip_to_selectable(self, downward: bool = True) -> None:
        """Move the cursor off any non-selectable (separator/header) row.

        Delegates to the table (#261) so ``j``/``k``, the arrow keys, page
        keys, ``gg``/``G`` and post-rebuild placement all share one rule.
        """
        self.query_one("#agent-table", SkipRowTable).skip_to_data_row(downward=downward)

    # --- Events ---

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Jump to agent when Enter is pressed on a row."""
        pane_id = self._selected_pane_id()
        if pane_id:
            self.exit(result=pane_id)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Update preview when a row is highlighted."""
        pane_id = self._selected_pane_id()
        # Moving to a different agent re-targets history mode (#171): when
        # sticky, the new agent's history loads (no h re-press per agent);
        # otherwise — or when the new agent has no transcript — the preview
        # falls back to the live capture. The sticky flag itself survives
        # the fallback, so a later transcript agent re-enters history.
        if self._history_pane_id is not None and pane_id != self._history_pane_id:
            self._reset_history_state()
        if pane_id:
            if (
                self._history_sticky
                and pane_id != self._history_pane_id
                and self._try_enter_history(pane_id)
            ):
                return
            self._load_preview(pane_id)
        else:
            preview = self.query_one("#right-panel", PreviewPanel)
            preview.clear()
            preview.write(Text("No agent selected", style="dim"))

    @work(exclusive=True, thread=True)
    def _load_preview(self, pane_id: str) -> None:
        """Fetch pane content in a thread, then update preview on main thread."""
        if pane_id == self._history_pane_id:
            return  # history mode owns the panel; skip the capture entirely
        content = get_pane_content(pane_id, lines=1000, ansi=True)
        display = Text.from_ansi(content) if content else Text(f"No content for {pane_id}")
        self.call_from_thread(self._show_preview, display, pane_id)

    def _show_preview(self, content: Text, pane_id: str) -> None:
        """Update the preview panel (runs on main thread)."""
        if pane_id == self._history_pane_id:
            # A straggler capture finished after history mode was entered —
            # dropping it keeps the transcript view from being clobbered.
            return
        preview = self.query_one("#right-panel", PreviewPanel)
        same_agent = pane_id == self._preview_pane_id
        if same_agent:
            # Preserve scroll position when refreshing the same agent.
            # Must defer scroll_to until after refresh because clear()+write()
            # triggers a layout recomputation — scroll_y limits aren't valid
            # until the next frame.
            at_bottom = preview.scroll_y >= preview.max_scroll_y - 1
            saved_y = preview.scroll_y
            preview.clear()
            preview.write(content)
            if not at_bottom:
                preview.call_after_refresh(preview.scroll_to, y=saved_y, animate=False)
        else:
            # New agent: reset to bottom
            self._preview_pane_id = pane_id
            preview.clear()
            preview.write(content)
            preview.call_after_refresh(preview.scroll_end, animate=False)

    # --- Navigation ---

    def action_cursor_down(self) -> None:
        # The table action already skips non-selectable rows (#261).
        self.query_one("#agent-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#agent-table", DataTable).action_cursor_up()

    def action_goto_bottom(self) -> None:
        """Go to last row (G in vim)."""
        table = self.query_one("#agent-table", DataTable)
        if table.row_count > 0:
            table.move_cursor(row=table.row_count - 1)
            self._skip_to_selectable(downward=False)

    def _goto_top(self) -> None:
        """Go to first row (gg in vim)."""
        table = self.query_one("#agent-table", DataTable)
        if table.row_count > 0:
            table.move_cursor(row=0)
            self._skip_to_selectable(downward=True)

    # --- Preview scrolling ---

    def action_preview_scroll_up(self) -> None:
        """Scroll preview up by half a page."""
        preview = self.query_one("#right-panel", PreviewPanel)
        half_page = max(1, preview.size.height // 2)
        target = max(0, preview.scroll_y - half_page)
        preview.scroll_to(y=target, animate=False)
        self._maybe_load_earlier(target)

    def action_preview_scroll_down(self) -> None:
        """Scroll preview down by half a page."""
        preview = self.query_one("#right-panel", PreviewPanel)
        half_page = max(1, preview.size.height // 2)
        target = min(preview.max_scroll_y, preview.scroll_y + half_page)
        preview.scroll_to(y=target, animate=False)

    def on_preview_panel_top_reached(self, message: "PreviewPanel.TopReached") -> None:
        """Mouse wheel hit the top of the preview → page in earlier history."""
        self._maybe_load_earlier(0)

    # --- History mode (#171) ---

    def _reset_history_state(self) -> None:
        self._history_pane_id = None
        self._history_path = None
        self._history_turns = []
        self._history_cursor = None
        self._history_has_more = False
        self._history_loading = False

    def action_toggle_history(self) -> None:
        """Toggle the preview between live capture and transcript history.

        History is rendered locally from the agent's Claude transcript
        JSONL — zero pane interaction (Claude's fullscreen renderer keeps
        scrollback off-tmux; remote-controlling the pane was rejected
        hands-on in PR #152). Scrolling to the top pages in earlier turns.
        The choice is sticky (#171 follow-up): it persists to the prefs
        store and follows the cursor across agents; a press on a
        transcript-less agent just notifies, leaving the mode unchanged.
        """
        pane_id = self._selected_pane_id()
        if not pane_id:
            return
        if self._history_pane_id == pane_id:
            # Back to live: reset and re-capture from the pane.
            self._history_sticky = False
            self._persist_preview_mode()
            self._reset_history_state()
            self._preview_pane_id = None  # force scroll-to-bottom on reload
            self._load_preview(pane_id)
            return
        if self._try_enter_history(pane_id, notify=True):
            self._history_sticky = True
            self._persist_preview_mode()

    def _try_enter_history(self, pane_id: str, *, notify: bool = False) -> bool:
        """Enter history mode for ``pane_id`` if its transcript is locatable.

        Shared by the h toggle (notify=True — a press on a transcript-less
        agent deserves feedback) and the sticky cursor-follow path
        (notify=False — silently fall back to the live capture).
        """
        agent = next((a for a in self._agents if a.pane_id == pane_id), None)
        if agent is None:
            return False
        path = locate_transcript(agent.cwd, agent.claude_transcript_session_id)
        if path is None:
            if notify:
                self.notify("No transcript for this agent", severity="warning", timeout=3)
            return False
        self._reset_history_state()
        self._history_pane_id = pane_id
        self._history_path = path
        self._history_loading = True
        self._load_history_page(path, None, pane_id)
        return True

    @work(thread=True, exclusive=True, group="ui-preview-pref")
    def _persist_preview_mode(self) -> None:
        """Write the sticky Live|History mode to the shared prefs store.

        Mirrors ``_persist_sort_mode``: reads ``self._history_sticky`` at
        execution time so rapid h presses persist whatever the TUI last
        settled on, in its own exclusive group so it neither races itself
        nor cancels the sort-mode writer.
        """
        mode = "history" if self._history_sticky else "live"
        self._ui_prefs_store.set_tui_preview_mode(mode)

    def _maybe_load_earlier(self, target_y: float) -> None:
        """Infinite upward scroll: at the top with more history → next page."""
        if (
            self._history_pane_id is not None
            and self._history_path is not None
            and self._history_has_more
            and not self._history_loading
            and target_y <= 0
        ):
            self._history_loading = True
            self._load_history_page(self._history_path, self._history_cursor, self._history_pane_id)

    @work(thread=True, group="history-page")
    def _load_history_page(self, path: str, before: int | None, pane_id: str) -> None:
        """Read one history page off the UI thread (PR #152 lesson: never
        block the main thread on I/O). Args are captured at dispatch time."""
        page = read_history_page(path, before)
        self.call_from_thread(self._apply_history_page, page, pane_id)

    def _apply_history_page(self, page: HistoryPage, pane_id: str) -> None:
        """Merge a loaded page and re-render (runs on main thread)."""
        if self._history_pane_id != pane_id:
            return  # user left history mode while the read ran
        self._history_loading = False
        initial = not self._history_turns
        self._history_turns = list(page.turns) + self._history_turns
        self._history_cursor = page.next_cursor
        self._history_has_more = page.has_more
        self._render_history(initial=initial)

    def _history_text(self, width: int) -> Text:
        """Render accumulated turns in Claude Code's terminal idiom."""
        out = Text()
        if self._history_has_more:
            out.append("… scroll up for earlier …\n\n", style="dim")
        else:
            out.append("— beginning of session —\n\n", style="dim")
        if not self._history_turns:
            out.append("No conversation found in transcript.\n", style="dim")
        for turn in self._history_turns:
            if turn.kind == "user":
                # Left bar spanning the whole block, mirroring the web view's
                # border-left — the ``>`` alone was hard to pick out in a
                # dense pane. ``_BAR`` costs 2 columns of text width.
                for line in _wrap_user_lines(turn.text, width - 2):
                    out.append(_BAR, style="dim")
                    if line.startswith("> "):
                        out.append("> ", style="bold cyan")
                        out.append(line[2:] + "\n", style="bold")
                    else:
                        out.append(line + "\n", style="bold")
                out.append("\n")
            elif turn.kind == "assistant":
                out.append("⏺ ", style="green")
                out.append(turn.text + "\n\n")
            elif turn.kind == "system":
                # Injected notices (task/teammate/interrupt/compact divider)
                out.append("✻ " + turn.text + "\n\n", style="dim italic")
            else:
                out.append("⏺ " + turn.text + "\n\n", style="dim")
        return out

    def _render_history(self, *, initial: bool) -> None:
        """Write history into the preview. RichLog is append-only, so a
        prepend is a full re-render; the scroll position is restored by
        the content-height delta (post-layout, hence call_after_refresh)."""
        preview = self.query_one("#right-panel", PreviewPanel)
        saved_y = preview.scroll_y
        prev_max = preview.max_scroll_y
        preview.clear()
        # User blocks are pre-wrapped, so the width must match what RichLog
        # actually renders into — otherwise it re-wraps the overflow onto
        # lines that carry no bar. The scrollbar gutter is reserved
        # unconditionally: history nearly always scrolls, and this very write
        # is what makes the scrollbar appear, so measuring before it exists
        # overshoots by its width. Fall back before the first layout pass.
        # ``scrollbar_size_vertical`` reads 0 until the scrollbar is actually
        # shown, so take the configured style (default 2 columns) instead.
        gutter = preview.styles.scrollbar_size_vertical or 2
        width = max(20, (preview.content_size.width or 80) - gutter)
        preview.write(self._history_text(width))
        if initial:
            preview.call_after_refresh(preview.scroll_end, animate=False)
        else:

            def _restore() -> None:
                delta = max(0, preview.max_scroll_y - prev_max)
                preview.scroll_to(y=saved_y + delta, animate=False)

            preview.call_after_refresh(_restore)

    # --- Actions ---

    def action_jump(self) -> None:
        pane_id = self._selected_pane_id()
        if pane_id:
            self.exit(result=pane_id)

    def action_toggle_mute(self) -> None:
        """Toggle mute/unmute for the selected agent."""
        pane_id = self._selected_pane_id()
        if pane_id:
            self._do_toggle_mute(pane_id)

    @work(thread=True)
    def _do_toggle_mute(self, pane_id: str) -> None:
        """Toggle mute in a thread (reads pane content for fingerprint)."""
        fingerprint = compute_content_fingerprint(pane_id)
        toggle_inactive(pane_id, fingerprint)
        self._refresh_data()

    def action_toggle_pin(self) -> None:
        """Pin/unpin the selected agent to the top section (#141)."""
        pane_id = self._selected_pane_id()
        if pane_id:
            self._do_toggle_pin(pane_id)

    @work(thread=True)
    def _do_toggle_pin(self, pane_id: str) -> None:
        """Flip the agent's pin in the disk store (keyed on stable_key), in a
        thread, then refresh so the card floats into / out of the pinned
        section.

        Pinned-state is checked via ``is_pinned_in`` (#205 Task 7) so a codex
        agent pinned before its thread UUID was captured — under the
        pre-upgrade ``legacy_stable_key`` — still reads as pinned. Pinning
        writes ONLY the current ``stable_key`` (#205 Codex review): also
        writing the legacy form left a ``tool:cwd:pane_id`` entry that a
        future unrelated same-pane/same-cwd session would collide with. A
        pre-capture pin naturally lands in legacy form (that IS its
        ``stable_key`` then) and ``migrate_legacy_pins`` re-keys it once the
        UUID is captured. Unpinning still clears both forms so pre-upgrade
        dual-written pins can be removed cleanly.
        """
        agent = next((a for a in self._agents if a.pane_id == pane_id), None)
        if agent is None:
            return
        key, legacy_key = agent.stable_key, agent.legacy_stable_key
        if agent.is_pinned_in(self._pinned_store.keys()):
            self._pinned_store.remove(key)
            self._pinned_store.remove(legacy_key)
        else:
            self._pinned_store.add(key)
        self._refresh_data()

    _SORT_CYCLE = ("priority", "phase", "repo")

    def action_toggle_sort(self) -> None:
        """Cycle sort mode: priority → phase → grouped-by-repo → priority.

        All three modes share the same columns + preview, so only rows are
        rebuilt — no column churn, no preview flash (issue #97).
        """
        i = self._SORT_CYCLE.index(self._sort_mode)
        self._sort_mode = self._SORT_CYCLE[(i + 1) % len(self._SORT_CYCLE)]
        # The user took control of the cursor context — drop any pending
        # initial focus (#176).
        self._initial_focus_pending = False
        self._persist_sort_mode()
        self._rebuild_table()

    @work(thread=True, exclusive=True, group="ui-prefs")
    def _persist_sort_mode(self) -> None:
        """Write the current sort mode to the shared prefs store in a thread.

        Deliberately reads ``self._sort_mode`` at execution time (not a value
        captured at dispatch) and runs exclusive in its own group: rapid ``s``
        presses would otherwise race stale writes, and whichever worker writes
        last must persist the mode the TUI actually shows (#176 review). The
        dedicated group keeps it from cross-cancelling the default-group
        workers (e.g. ``_refresh_data``).
        """
        self._ui_prefs_store.set_tui_sort_mode(self._sort_mode)

    def action_confirm_kill(self) -> None:
        """Start kill confirmation for the selected agent."""
        pane_id = self._selected_pane_id()
        if pane_id:
            self._pending_kill = pane_id
            # Re-arm the auto-clear so the pending state dies with the toast
            # instead of leaking into the next unrelated keypress (#14).
            if self._pending_kill_timer is not None:
                self._pending_kill_timer.stop()
            self._pending_kill_timer = self.set_timer(
                self._KILL_CONFIRM_TIMEOUT, self._clear_pending_kill
            )
            self.notify(
                f"Kill agent in {pane_id}? Press y to confirm, any other key to cancel.",
                severity="warning",
                timeout=self._KILL_CONFIRM_TIMEOUT,
            )

    def _clear_pending_kill(self) -> None:
        """Drop an unanswered kill confirmation once its toast expires."""
        self._pending_kill = None
        self._pending_kill_timer = None

    def action_rename_session(self) -> None:
        """Rename the tmux session for the selected agent."""
        pane_id = self._selected_pane_id()
        if pane_id:
            self._do_rename(pane_id)

    @work(thread=True)
    def _do_rename(self, pane_id: str) -> None:
        """Rename session in a thread, then refresh."""
        success = rename_session(pane_id)
        if success:
            self.call_from_thread(
                self.notify, "Session rename triggered", severity="information", timeout=3
            )
        else:
            self.call_from_thread(
                self.notify, "Could not rename session", severity="warning", timeout=3
            )
        self._refresh_data()

    @work(thread=True)
    def _kill_agent(self, pane_id: str) -> None:
        """Kill agent in a thread (contains time.sleep), then refresh."""
        kill_agent(pane_id)
        self.call_from_thread(self._on_kill_complete, pane_id)

    def _on_kill_complete(self, pane_id: str) -> None:
        self.notify(f"Killed agent in {pane_id}", severity="information", timeout=3)
        self._refresh_data()

    # --- Preview resize ---

    def action_toggle_preview(self) -> None:
        """Toggle preview panel visibility."""
        if self._preview_idx == 0:
            self._preview_idx = _DEFAULT_PREVIEW_IDX
        else:
            self._preview_idx = 0
        self._apply_preview_size()

    def action_shrink_preview(self) -> None:
        """Shrink preview panel (])."""
        if self._preview_idx > 0:
            self._preview_idx -= 1
            self._apply_preview_size()

    def action_grow_preview(self) -> None:
        """Grow preview panel ([)."""
        if self._preview_idx < len(_PREVIEW_WIDTHS) - 1:
            self._preview_idx += 1
            self._apply_preview_size()

    def _apply_preview_size(self) -> None:
        """Apply the current preview width setting."""
        preview = self.query_one("#right-panel", PreviewPanel)
        width = _PREVIEW_WIDTHS[self._preview_idx]
        preview.styles.width = width
        preview.display = self._preview_idx > 0

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
        table = self.query_one("#agent-table", DataTable)
        table.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter table as user types in search."""
        self._search_query = event.value
        self._rebuild_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Close search on Enter — keep the filter active."""
        self._close_search()

    # --- Key handler ---

    def on_key(self, event: events.Key) -> None:
        """Handle Escape, gg chord, and kill confirmation."""
        # Skip agent-specific key handling whenever a non-agents screen
        # (dispatch) is active so its bindings own the input.
        if not self._on_agents_screen():
            return

        search = self.query_one("#search-input", Input)

        # Kill confirmation: y confirms, anything else cancels
        if self._pending_kill is not None:
            if event.key == "y":
                self._kill_agent(self._pending_kill)
            else:
                self.notify("Kill cancelled", severity="information", timeout=2)
            self._pending_kill = None
            if self._pending_kill_timer is not None:
                self._pending_kill_timer.stop()
                self._pending_kill_timer = None
            event.prevent_default()
            return

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
            now = _time.monotonic()
            if now - self._last_g_time < self._GG_TIMEOUT:
                self._goto_top()
                self._last_g_time = 0.0
            else:
                self._last_g_time = now
            return

        # [ and ] to resize preview
        if event.character == "[":
            self.action_grow_preview()
            return
        if event.character == "]":
            self.action_shrink_preview()
            return

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide agent-specific bindings when a non-agents screen is active."""
        if action in self._AGENTS_ONLY_ACTIONS:
            return self._on_agents_screen()
        return True

    def _on_agents_screen(self) -> bool:
        """True when the agents view is the active screen.

        Agents is the App's default screen, never installed; dispatch is
        installed and pushed on top. So "on agents" is "the active screen is
        not the installed one". Returns True before ``on_mount`` (no
        installed screen yet, no Textual screen stack).
        """
        if not hasattr(self, "_dispatch_screen"):
            return True
        try:
            active = self.screen
        except Exception:
            return True
        if hasattr(self, "_dispatch_screen") and active is self._dispatch_screen:
            return False
        return True

    def action_refresh(self) -> None:
        self._refresh_data()

    def action_switch_to_dispatch(self) -> None:
        """Switch to the dispatch screen."""
        self.push_screen("dispatch")


def run_dashboard(agent_filter: str | None = None, repo_filter: str | None = None) -> None:
    """Launch the Textual dashboard."""
    try:
        if not is_daemon_running():
            start_daemon(silent=True)
    except Exception:
        # Dashboard launch should continue even if daemon auto-start fails.
        pass

    app = AgentDashboard(agent_filter=agent_filter, repo_filter=repo_filter)
    result = app.run()
    if result:
        jump_to_pane(result)
