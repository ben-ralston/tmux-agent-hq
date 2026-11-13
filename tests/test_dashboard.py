"""Tests for dashboard data formatting helpers."""

import asyncio
import time
from unittest.mock import patch

import pytest

from agent_hq.capabilities import CLAUDE_ONLY
from agent_hq.dashboard import (
    AgentDashboard,
    build_muted_rows,
    build_phase_rows,
    build_pinned_rows,
    build_priority_rows,
    build_table_rows,
    cursor_target_row,
    format_phase_cell,
    format_state_cell,
)
from agent_hq.models import (
    Agent,
    AgentState,
    AgentType,
    DevPhase,
    InputsObserved,
    PhaseSnapshot,
    RepoGroup,
    WorktreeGroup,
)


def _phase_snapshot(phase: DevPhase | None) -> PhaseSnapshot | None:
    """Minimal snapshot wrapper so ``Agent.phase`` returns ``phase``."""
    if phase is None:
        return None
    return PhaseSnapshot(
        detected_phase=phase,
        effective_phase=phase,
        phase_source="rule",
        phase_session_id=None,
        feedback=None,
        inputs_observed=InputsObserved(overrides_revision=0, hook_state_revision=0),
        producer="daemon",
        producer_epoch=0.0,
        sequence=0,
        monotonic_ns=0,
        timestamp=0.0,
    )


def make_agent(
    pane_id: str = "%1",
    agent_type: AgentType = AgentType.CLAUDE,
    branch: str | None = "main",
    worktree_name: str | None = None,
    repo_name: str | None = "project",
    state: AgentState = AgentState.RUNNING,
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
        state=state,
    )


def make_group(repo_name: str, worktrees: list[tuple[str | None, str, list[Agent]]]) -> RepoGroup:
    """Helper to create a RepoGroup.

    worktrees = [(wt_name_or_None, branch, agents)].
    Use None for wt_name to match production main-worktree behavior.
    """
    wt_groups = []
    for wt_name, branch, agents in worktrees:
        wt_groups.append(
            WorktreeGroup(
                repo_name=repo_name,
                repo_root=f"/{repo_name}",
                worktree_path=f"/{repo_name}" if wt_name is None else f"/{repo_name}-{wt_name}",
                worktree_name=wt_name,
                branch=branch,
                agents=agents,
            )
        )
    return RepoGroup(repo_name=repo_name, repo_root=f"/{repo_name}", worktrees=wt_groups)


def make_phased_agent(
    pane_id: str = "%1",
    phase: DevPhase | None = DevPhase.IMPLEMENT,
    state: AgentState = AgentState.RUNNING,
    repo_name: str | None = "project",
    branch: str | None = "feat",
) -> Agent:
    """Helper to create a test agent carrying a phase snapshot."""
    return Agent(
        pane_id=pane_id,
        session_name="test",
        window_index=1,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd="/project",
        repo_root="/project",
        repo_name=repo_name,
        branch=branch,
        state=state,
        raw_command="claude",
        phase_snapshot=_phase_snapshot(phase),
    )


class TestFormatStateCell:
    def test_running_returns_green_emoji(self):
        result = format_state_cell(AgentState.RUNNING)
        assert "🟢" in result
        assert "green" in result

    def test_waiting_returns_yellow_emoji(self):
        result = format_state_cell(AgentState.WAITING)
        assert "🟡" in result
        assert "yellow" in result

    def test_idle_returns_dim_emoji(self):
        result = format_state_cell(AgentState.IDLE)
        assert "⚪" in result
        assert "dim" in result


class TestBuildTableRows:
    def test_single_repo_single_agent(self):
        agent = make_agent(pane_id="%5", repo_name="myrepo", branch="main")
        groups = [make_group("myrepo", [(None, "main", [agent])])]
        rows = build_table_rows(groups)
        # A repo header row precedes the single agent row.
        assert len(rows) == 2
        assert rows[0]["is_header"] is True
        row = rows[1]
        assert row["repo"] == "myrepo"
        assert row["branch"] == "main"
        assert row["pane_id"] == "%5"
        assert row["is_separator"] is False
        assert row["is_header"] is False

    def test_repo_header_shows_name_and_count(self):
        a1 = make_agent(pane_id="%5", repo_name="myrepo", branch="main")
        a2 = make_agent(pane_id="%8", repo_name="myrepo", branch="dev")
        groups = [make_group("myrepo", [(None, "main", [a1]), (None, "dev", [a2])])]
        rows = build_table_rows(groups)
        header = rows[0]
        assert header["is_header"] is True
        assert header["pane_id"] is None
        assert "myrepo" in header["header_text"]
        assert "(2)" in header["header_text"]

    def test_long_repo_name_keeps_count_visible(self):
        agent = make_agent(pane_id="%5", repo_name="gizmos", branch="main")
        groups = [make_group("gizmos", [(None, "main", [agent])])]
        header = build_table_rows(groups)[0]
        # Even when the name is truncated to fit the 14-wide Repo column,
        # the count must survive.
        assert "(1)" in header["header_text"]

    def test_two_repos_have_header_then_separator(self):
        a1 = make_agent(pane_id="%5", repo_name="repo-a", branch="main")
        a2 = make_agent(pane_id="%8", repo_name="repo-b", branch="dev")
        groups = [
            make_group("repo-a", [(None, "main", [a1])]),
            make_group("repo-b", [(None, "dev", [a2])]),
        ]
        rows = build_table_rows(groups)
        # header, agent, separator, header, agent
        assert len(rows) == 5
        assert rows[0]["is_header"] is True
        assert rows[1]["is_separator"] is False and rows[1]["is_header"] is False
        assert rows[2]["is_separator"] is True
        assert rows[3]["is_header"] is True
        assert rows[4]["is_separator"] is False and rows[4]["is_header"] is False

    def test_no_state_text_only_emoji(self):
        agent = make_agent(state=AgentState.RUNNING)
        groups = [make_group("project", [(None, "main", [agent])])]
        rows = build_table_rows(groups)
        state_cell = rows[1]["state"]
        assert "🟢" in state_cell
        assert "RUNNING" not in state_cell

    def test_empty_groups_returns_empty_list(self):
        rows = build_table_rows([])
        assert rows == []

    def test_agent_with_none_branch_shows_dash(self):
        agent = make_agent(branch=None)
        groups = [make_group("project", [(None, "main", [agent])])]
        rows = build_table_rows(groups)
        assert rows[1]["branch"] == "—"

    def test_ungrouped_agents_show_cwd_basename(self):
        agent = make_agent(pane_id="%5", repo_name=None, branch=None)
        # Simulate ungrouped: repo_name is "[ungrouped]", agent.cwd provides the name
        agent.cwd = "/Users/dev/Desktop"
        group = RepoGroup(
            repo_name="[ungrouped]",
            repo_root="",
            worktrees=[
                WorktreeGroup(
                    repo_name="[ungrouped]",
                    repo_root="",
                    worktree_path="",
                    worktree_name="—",
                    branch="—",
                    agents=[agent],
                )
            ],
        )
        rows = build_table_rows([group])
        assert rows[1]["repo"] == "Desktop"


class TestBuildPriorityRows:
    def test_no_muted_has_no_separators(self):
        agents = [
            make_agent(pane_id="%5", state=AgentState.IDLE),
            make_agent(pane_id="%8", state=AgentState.RUNNING),
        ]
        rows = build_priority_rows(agents)
        assert all(not r["is_separator"] for r in rows)

    def test_separator_between_active_and_muted(self):
        agents = [
            make_agent(pane_id="%5", state=AgentState.RUNNING),
            make_agent(pane_id="%8", state=AgentState.IDLE),
        ]
        rows = build_priority_rows(agents, inactive_pane_ids={"%8"})
        # Should be: active row, separator, muted row
        assert len(rows) == 3
        assert rows[0]["is_separator"] is False
        assert rows[1]["is_separator"] is True
        assert rows[2]["is_separator"] is False

    def test_all_muted_no_separator(self):
        agents = [
            make_agent(pane_id="%5", state=AgentState.IDLE),
        ]
        rows = build_priority_rows(agents, inactive_pane_ids={"%5"})
        # No separator when there are no active agents before muted
        assert len(rows) == 1
        assert rows[0]["is_separator"] is False

    def test_muted_agents_are_dimmed(self):
        agents = [
            make_agent(pane_id="%5", state=AgentState.IDLE),
        ]
        rows = build_priority_rows(agents, inactive_pane_ids={"%5"})
        assert "dim" in rows[0]["repo"]

    def test_unmuted_agents_not_dimmed(self):
        agents = [
            make_agent(pane_id="%5", state=AgentState.IDLE, repo_name="myrepo"),
        ]
        rows = build_priority_rows(agents, inactive_pane_ids=set())
        assert rows[0]["repo"] == "myrepo"
        assert "dim" not in rows[0]["repo"]

    def test_empty_agents_returns_empty(self):
        rows = build_priority_rows([])
        assert rows == []

    def test_uses_cwd_basename_when_no_repo_name(self):
        agent = make_agent(pane_id="%5", repo_name=None)
        agent.cwd = "/Users/dev/scratch"
        rows = build_priority_rows([agent])
        assert "scratch" in rows[0]["repo"]


class TestBuildPinnedRows:
    """The pinned top section (#141): a '📌 Pinned' header + priority-sorted
    pinned rows, keyed on ``Agent.stable_key``."""

    def _pin(self, pane: str, key: str, **kw) -> Agent:
        a = make_agent(pane, **kw)
        a.claude_transcript_session_id = key
        return a

    def test_empty_when_nothing_pinned(self):
        assert build_pinned_rows([make_agent("%1")], set(), set()) == []

    def test_header_then_data_row(self):
        rows = build_pinned_rows([self._pin("%1", "s1")], set(), {"s1"})
        assert rows[0]["is_header"] is True
        assert "Pinned" in rows[0]["header_text"]
        data = [r for r in rows if not r.get("is_header") and not r.get("is_separator")]
        assert [r["pane_id"] for r in data] == ["%1"]

    def test_only_pinned_keys_included(self):
        a1 = self._pin("%1", "s1")
        a2 = self._pin("%2", "s2")
        rows = build_pinned_rows([a1, a2], set(), {"s2"})
        ids = [r["pane_id"] for r in rows if not r.get("is_header")]
        assert ids == ["%2"]

    def test_pinned_row_carries_pin_glyph(self):
        rows = build_pinned_rows([self._pin("%1", "s1")], set(), {"s1"})
        data = next(r for r in rows if not r.get("is_header"))
        assert "📌" in data["state"]

    def test_fallback_key_for_non_claude_agent(self):
        a = make_agent("%1", agent_type=AgentType.CODEX)
        rows = build_pinned_rows([a], set(), {a.stable_key})
        assert any(r.get("pane_id") == "%1" for r in rows)

    def test_matches_pre_upgrade_legacy_key(self):
        """A codex agent whose thread UUID upgraded its ``stable_key`` (#205
        Tasks 1-3) must still be found by a pin written under the old
        ``codex:cwd:pane`` form (#205 Task 7)."""
        a = make_agent("%1", agent_type=AgentType.CODEX)
        a.codex_thread_id = "uuid-7"
        rows = build_pinned_rows([a], set(), {a.legacy_stable_key})
        assert any(r.get("pane_id") == "%1" for r in rows)


class TestRowsForDisplayPartition:
    """``_rows_for_display`` floats pinned agents above the body and removes
    them from their normal group (no duplication)."""

    def _pin(self, pane: str, key: str, **kw) -> Agent:
        a = make_agent(pane, **kw)
        a.claude_transcript_session_id = key
        return a

    def _app(self, agents, pinned_keys, sort_mode="phase") -> AgentDashboard:
        app = AgentDashboard.__new__(AgentDashboard)
        app._sort_mode = sort_mode
        app._search_query = ""
        app._inactive_pane_ids = set()
        app._agents = agents
        app._pinned_keys = pinned_keys
        return app

    def test_pinned_floats_above_body_and_not_duplicated(self):
        a1 = self._pin("%1", "s1", repo_name="r", branch="b1")
        a2 = make_agent("%2", repo_name="r", branch="b2")
        a2.phase_snapshot = _phase_snapshot(DevPhase.IMPLEMENT)
        rows = self._app([a1, a2], {"s1"})._rows_for_display()
        assert rows[0]["is_header"] and "Pinned" in rows[0]["header_text"]
        data_ids = [
            r["pane_id"] for r in rows if not r.get("is_header") and not r.get("is_separator")
        ]
        assert data_ids[0] == "%1"
        assert data_ids.count("%1") == 1  # not duplicated in its phase group

    def test_no_pinned_section_when_none_pinned(self):
        app = self._app([make_agent("%1")], set(), sort_mode="priority")
        rows = app._rows_for_display()
        assert not any(r.get("is_header") and "Pinned" in r.get("header_text", "") for r in rows)

    def test_legacy_key_pin_removed_from_body_too(self):
        """A pin held under the pre-upgrade legacy key must still pull the
        agent out of its normal sort-mode section, not just into the pinned
        band (#205 Task 7)."""
        a = make_agent("%1", agent_type=AgentType.CODEX, repo_name="r", branch="b1")
        a.codex_thread_id = "uuid-7"
        rows = self._app([a], {a.legacy_stable_key})._rows_for_display()
        assert rows[0]["is_header"] and "Pinned" in rows[0]["header_text"]
        data_ids = [
            r["pane_id"] for r in rows if not r.get("is_header") and not r.get("is_separator")
        ]
        assert data_ids.count("%1") == 1


class TestBuildMutedRows:
    """The muted bottom section (#278): a '🔇 Muted' header + priority-sorted,
    dimmed muted rows — the inverse of ``build_pinned_rows``."""

    def test_empty_when_nothing_muted(self):
        assert build_muted_rows([make_agent("%1")], set()) == []

    def test_header_then_dimmed_rows(self):
        rows = build_muted_rows([make_agent("%1"), make_agent("%2")], {"%2"})
        assert rows[0]["is_header"] is True
        assert "Muted" in rows[0]["header_text"]
        data = [r for r in rows if not r.get("is_header") and not r.get("is_separator")]
        assert [r["pane_id"] for r in data] == ["%2"]
        assert "dim" in data[0]["repo"]

    def test_header_count_matches(self):
        rows = build_muted_rows([make_agent("%1"), make_agent("%2")], {"%1", "%2"})
        assert "(2)" in rows[0]["header_text"]


class TestRowsForDisplayMutedSection:
    """``_rows_for_display`` sinks muted agents into a Muted section below
    everything else — including Done — in every sort mode (#278). Pinned wins
    over muted for placement, mirroring the web route."""

    def _app(self, agents, inactive, pinned_keys=None, sort_mode="phase") -> AgentDashboard:
        app = AgentDashboard.__new__(AgentDashboard)
        app._sort_mode = sort_mode
        app._search_query = ""
        app._inactive_pane_ids = inactive
        app._agents = agents
        app._pinned_keys = pinned_keys or set()
        return app

    @staticmethod
    def _seq(rows):
        out = []
        for r in rows:
            if r.get("is_header"):
                out.append(("H", r["header_text"]))
            elif r["is_separator"]:
                out.append(("S",))
            else:
                out.append(("A", r["pane_id"]))
        return out

    def test_muted_below_done_in_phase_mode(self):
        active = make_phased_agent(pane_id="%1", phase=DevPhase.IMPLEMENT)
        done = make_phased_agent(pane_id="%2", phase=DevPhase.DONE)
        muted = make_phased_agent(pane_id="%3", phase=DevPhase.IMPLEMENT)
        rows = self._app([active, done, muted], {"%3"})._rows_for_display()
        seq = self._seq(rows)
        headers = [s[1] for s in seq if s[0] == "H"]
        assert "Muted" in headers[-1]
        # The muted agent is listed once, after the Done agent, under the Muted header.
        ids = [s[1] for s in seq if s[0] == "A"]
        assert ids.index("%2") < ids.index("%3")
        assert ids.count("%3") == 1
        muted_header_idx = next(i for i, s in enumerate(seq) if s[0] == "H" and "Muted" in s[1])
        assert seq[muted_header_idx - 1] == ("S",)
        assert seq[muted_header_idx + 1] == ("A", "%3")
        # And not inside the Implement group.
        impl_header_idx = next(
            i for i, s in enumerate(seq) if s[0] == "H" and DevPhase.IMPLEMENT.emoji in s[1]
        )
        assert seq[impl_header_idx + 1] == ("A", "%1")
        assert seq[impl_header_idx + 2] != ("A", "%3")

    def test_muted_and_done_lands_in_muted_section(self):
        done_muted = make_phased_agent(pane_id="%2", phase=DevPhase.DONE)
        rows = self._app([done_muted], {"%2"})._rows_for_display()
        seq = self._seq(rows)
        assert seq[0][0] == "H" and "Muted" in seq[0][1]
        assert not any(s[0] == "H" and DevPhase.DONE.emoji in s[1] for s in seq)

    def test_muted_section_last_in_priority_mode(self):
        rows = self._app(
            [make_agent("%1"), make_agent("%2")], {"%1"}, sort_mode="priority"
        )._rows_for_display()
        seq = self._seq(rows)
        assert seq == [("A", "%2"), ("S",), ("H", seq[2][1]), ("A", "%1")]
        assert "Muted" in seq[2][1]

    def test_muted_section_last_in_repo_mode(self):
        a = make_agent("%1", repo_name="r", branch="b1")
        b = make_agent("%2", repo_name="r", branch="b2")
        rows = self._app([a, b], {"%2"}, sort_mode="repo")._rows_for_display()
        seq = self._seq(rows)
        ids = [s[1] for s in seq if s[0] == "A"]
        assert ids == ["%1", "%2"]
        assert seq[-2][0] == "H" and "Muted" in seq[-2][1]

    def test_pinned_wins_over_muted(self):
        a = make_agent("%1")
        a.claude_transcript_session_id = "s1"
        rows = self._app([a], {"%1"}, pinned_keys={"s1"})._rows_for_display()
        seq = self._seq(rows)
        assert seq[0][0] == "H" and "Pinned" in seq[0][1]
        assert not any(s[0] == "H" and "Muted" in s[1] for s in seq)
        assert [s for s in seq if s[0] == "A"] == [("A", "%1")]

    def test_no_muted_section_when_none_muted(self):
        rows = self._app([make_agent("%1")], set(), sort_mode="priority")._rows_for_display()
        assert not any(r.get("is_header") and "Muted" in r.get("header_text", "") for r in rows)


class TestTogglePin:
    """``_do_toggle_pin`` flips the agent's stable_key in the injected store."""

    def test_toggle_adds_then_removes(self, tmp_path):
        from agent_hq.pinned_store import PinnedSessionsStore

        app = AgentDashboard.__new__(AgentDashboard)
        a = make_agent("%1")
        a.claude_transcript_session_id = "s1"
        app._agents = [a]
        app._pinned_store = PinnedSessionsStore(tmp_path / "p.json")
        app._refresh_data = lambda: None  # skip the @work refresh
        app._do_toggle_pin.__wrapped__(app, "%1")
        assert app._pinned_store.is_pinned("s1")
        app._do_toggle_pin.__wrapped__(app, "%1")
        assert not app._pinned_store.is_pinned("s1")

    def test_toggle_unknown_pane_is_noop(self, tmp_path):
        from agent_hq.pinned_store import PinnedSessionsStore

        app = AgentDashboard.__new__(AgentDashboard)
        app._agents = []
        app._pinned_store = PinnedSessionsStore(tmp_path / "p.json")
        app._refresh_data = lambda: None
        app._do_toggle_pin.__wrapped__(app, "%999")
        assert app._pinned_store.keys() == set()

    def test_toggle_unpins_a_pre_upgrade_legacy_pin(self, tmp_path):
        """A codex agent pinned before its thread UUID was captured is
        pinned under ``legacy_stable_key``. Toggling it off must clear that
        key (and the current ``stable_key``, defensively) so it doesn't
        stay pinned forever (#205 Task 7)."""
        from agent_hq.pinned_store import PinnedSessionsStore

        app = AgentDashboard.__new__(AgentDashboard)
        a = make_agent("%1", agent_type=AgentType.CODEX)
        a.codex_thread_id = "uuid-7"
        app._agents = [a]
        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add(a.legacy_stable_key)
        app._pinned_store = store
        app._refresh_data = lambda: None
        app._do_toggle_pin.__wrapped__(app, "%1")
        assert not store.is_pinned(a.legacy_stable_key)
        assert not store.is_pinned(a.stable_key)

    def test_toggle_pin_writes_only_stable_key(self, tmp_path):
        """Pinning writes ONLY the current ``stable_key`` (#205 Codex
        review). Dual-writing ``legacy_stable_key`` alongside left a
        ``tool:cwd:pane_id`` entry that outlived the session -- an unrelated
        future session in the same pane and cwd would collide with it and
        render pinned."""
        from agent_hq.pinned_store import PinnedSessionsStore

        app = AgentDashboard.__new__(AgentDashboard)
        a = make_agent("%1", agent_type=AgentType.CODEX)
        a.codex_thread_id = "uuid-7"
        app._agents = [a]
        store = PinnedSessionsStore(tmp_path / "p.json")
        app._pinned_store = store
        app._refresh_data = lambda: None
        app._do_toggle_pin.__wrapped__(app, "%1")
        assert store.keys() == {"uuid-7"}

        # The successor-collision scenario Codex flagged: session uuid-7
        # exits, an unrelated codex session reuses the same pane + cwd.
        successor = make_agent("%1", agent_type=AgentType.CODEX)
        successor.codex_thread_id = None
        assert not successor.is_pinned_in(store.keys())

    def test_pin_hides_during_codex_thread_id_flap_and_returns(self, tmp_path):
        """The approved tradeoff of single-key pins (#205 Codex review):
        when a captured codex UUID flaps back to ``None`` (dedupe / 3-day
        rollout cutoff), the pin display drops temporarily -- the store
        still holds the UUID key, so it self-heals when capture returns,
        instead of a persistent legacy key transferring the pin to
        strangers."""
        from agent_hq.pinned_store import PinnedSessionsStore

        app = AgentDashboard.__new__(AgentDashboard)
        a = make_agent("%1", agent_type=AgentType.CODEX)
        a.codex_thread_id = "uuid-7"
        app._agents = [a]
        store = PinnedSessionsStore(tmp_path / "p.json")
        app._pinned_store = store
        app._refresh_data = lambda: None
        app._do_toggle_pin.__wrapped__(app, "%1")

        a.codex_thread_id = None
        assert not a.is_pinned_in(store.keys())  # hidden during the flap
        a.codex_thread_id = "uuid-7"
        assert a.is_pinned_in(store.keys())  # self-heals on recapture


class TestCapabilitiesWarmup:
    """Fix 7 (multi-review wave): a mount-time ``@work(thread=True)`` warmup
    worker calls the injected probe once, off the main/UI thread, so its
    in-process TTL cache (``capabilities._cache``) is already warm by the
    time ``ConfirmDispatchModal`` constructs and
    makes its own synchronous probe call -- those stay sync (a cache hit
    is effectively instant), only the FIRST-ever probe of the process
    needs to not block the UI."""

    def test_warmup_worker_calls_injected_probe(self):
        app = AgentDashboard.__new__(AgentDashboard)
        calls = []

        def probe():
            calls.append(1)
            return CLAUDE_ONLY

        app._capabilities_probe = probe
        app._warm_capabilities_cache.__wrapped__(app)
        assert calls == [1]


class TestCursorTargetRow:
    """After a rebuild the cursor follows the previously-selected agent to its
    new row (so pin/unpin, or any reorder, keeps the highlight on the same
    agent — pressing P again undoes the toggle). Falls back to the old row
    index only when that agent is gone (#141 follow-up)."""

    def test_follows_moved_agent_to_new_row(self):
        # %1 was at row 1; after a pin it floats up to row 3.
        rows = [None, "%2", None, "%1"]
        assert cursor_target_row(rows, "%1", 1) == 3

    def test_falls_back_to_prev_row_when_agent_gone(self):
        rows = [None, "%2", "%3"]
        assert cursor_target_row(rows, "%9", 2) == 2

    def test_uses_prev_row_when_no_prev_pane(self):
        rows = ["%1", "%2"]
        assert cursor_target_row(rows, None, 1) == 1

    def test_returns_none_when_prev_row_out_of_range_and_agent_gone(self):
        rows = ["%2"]
        assert cursor_target_row(rows, None, 5) is None

    def test_identity_wins_even_when_prev_row_still_in_range(self):
        # Old index 0 is valid, but the agent actually moved to index 2.
        rows = ["%2", None, "%1"]
        assert cursor_target_row(rows, "%1", 0) == 2


class TestCursorFollowsPinnedAgent:
    """End-to-end (real DataTable): pinning the highlighted agent floats it to
    the top section and the cursor follows it there, so a second P undoes the
    pin (#141 follow-up)."""

    def test_pilot_cursor_follows_pin_and_unpin(self, tmp_path):
        from unittest.mock import patch

        from textual.widgets import DataTable

        from agent_hq.pinned_store import PinnedSessionsStore

        a1 = make_agent("%1", repo_name="r", branch="b1")
        a1.claude_transcript_session_id = "s1"
        a2 = make_agent("%2", repo_name="r", branch="b2")
        a2.claude_transcript_session_id = "s2"

        async def run_test() -> None:
            with (
                patch("agent_hq.dashboard.is_daemon_running", return_value=True),
                patch("agent_hq.dashboard.get_agents_cached", return_value=([a1, a2], 0.0)),
            ):
                app = AgentDashboard()
                # Isolate the pin store so the test never touches real config.
                app._pinned_store = PinnedSessionsStore(tmp_path / "p.json")
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    # Deterministic flat roster, nothing pinned yet.
                    app._agents = [a1, a2]
                    app._inactive_pane_ids = set()
                    app._pinned_keys = set()
                    app._search_query = ""
                    app._sort_mode = "priority"
                    app._rebuild_table()
                    await pilot.pause()

                    table = app.query_one("#agent-table", DataTable)
                    table.move_cursor(row=app._row_pane_ids.index("%2"))
                    await pilot.pause()
                    assert app._selected_pane_id() == "%2"

                    # Pin the selected agent: it floats up; cursor must follow.
                    app._pinned_store.add("s2")
                    app._pinned_keys = app._pinned_store.keys()
                    app._rebuild_table()
                    await pilot.pause()
                    assert app._selected_pane_id() == "%2"
                    assert app._row_pane_ids.index("%2") < app._row_pane_ids.index("%1")

                    # Unpin it: it drops back into the body; cursor still follows.
                    app._pinned_store.remove("s2")
                    app._pinned_keys = app._pinned_store.keys()
                    app._rebuild_table()
                    await pilot.pause()
                    assert app._selected_pane_id() == "%2"

        asyncio.run(run_test())


class TestPreviewPanel:
    def test_preview_panel_is_richlog_subclass(self):
        """PreviewPanel should extend RichLog for scrollable ANSI rendering."""
        from textual.widgets import RichLog

        from agent_hq.dashboard import PreviewPanel

        assert issubclass(PreviewPanel, RichLog)

    def test_preview_panel_defaults_to_narrow_min_width(self):
        """PreviewPanel should render to the available width, not RichLog's 78-column default."""
        from agent_hq.dashboard import PreviewPanel

        assert PreviewPanel().min_width == 1

    def test_preview_panel_wraps_long_ansi_lines_in_narrow_layout(self):
        """Long ANSI lines should wrap to the preview width instead of rendering at 78 columns."""
        from rich.text import Text
        from textual.app import App, ComposeResult

        from agent_hq.dashboard import PreviewPanel

        class PreviewTestApp(App):
            def compose(self) -> ComposeResult:
                yield PreviewPanel(id="preview", wrap=True, auto_scroll=True, markup=False)

        async def run_test() -> None:
            app = PreviewTestApp()
            async with app.run_test(size=(40, 10)) as pilot:
                preview = app.query_one("#preview", PreviewPanel)
                preview.write(Text.from_ansi("\x1b[32m" + ("x" * 100) + "\x1b[0m"))
                await pilot.pause()

                assert preview.scrollable_content_region.width < 78
                assert preview.virtual_size.width == preview.scrollable_content_region.width

        asyncio.run(run_test())

    def test_load_preview_requests_ansi_content(self):
        """_load_preview should call get_pane_content with ansi=True and lines=1000."""
        from unittest.mock import patch

        from agent_hq.dashboard import AgentDashboard

        app = AgentDashboard.__new__(AgentDashboard)
        app._preview_pane_id = None
        app._history_pane_id = None

        with patch("agent_hq.dashboard.get_pane_content", return_value="content") as mock_get:
            with patch.object(app, "call_from_thread"):
                # Call the inner logic directly (skip @work decorator)
                app._load_preview.__wrapped__(app, "%5")

        mock_get.assert_called_once_with("%5", lines=1000, ansi=True)

    def test_ansi_content_converted_with_from_ansi(self):
        """ANSI escape sequences should be converted via Text.from_ansi()."""
        from rich.text import Text

        ansi_content = "\x1b[32mgreen\x1b[0m normal"
        styled = Text.from_ansi(ansi_content)

        # Verify Text.from_ansi produces styled spans
        assert styled.plain == "green normal"
        assert len(styled._spans) > 0  # Has style information

    def test_show_preview_tracks_pane_id(self):
        """_show_preview should update _preview_pane_id when a new pane is shown."""
        from unittest.mock import MagicMock, patch

        from rich.text import Text

        from agent_hq.dashboard import AgentDashboard

        app = AgentDashboard.__new__(AgentDashboard)
        app._preview_pane_id = None
        app._history_pane_id = None

        mock_preview = MagicMock()
        with patch.object(app, "query_one", return_value=mock_preview):
            app._show_preview(Text("content"), "%5")

        assert app._preview_pane_id == "%5"

    def test_show_preview_updates_pane_id_on_switch(self):
        """Switching to a different agent should update _preview_pane_id."""
        from unittest.mock import MagicMock, patch

        from rich.text import Text

        from agent_hq.dashboard import AgentDashboard

        app = AgentDashboard.__new__(AgentDashboard)
        app._preview_pane_id = "%5"
        app._history_pane_id = None

        mock_preview = MagicMock()
        with patch.object(app, "query_one", return_value=mock_preview):
            app._show_preview(Text("new content"), "%8")

        assert app._preview_pane_id == "%8"

    def test_show_preview_scrolls_to_bottom_for_new_agent(self):
        """New agent preview should explicitly scroll to end after layout."""
        from unittest.mock import MagicMock, patch

        from rich.text import Text

        from agent_hq.dashboard import AgentDashboard

        app = AgentDashboard.__new__(AgentDashboard)
        app._preview_pane_id = None
        app._history_pane_id = None  # No previous agent

        mock_preview = MagicMock()
        with patch.object(app, "query_one", return_value=mock_preview):
            app._show_preview(Text("content"), "%5")

        mock_preview.call_after_refresh.assert_called_once_with(
            mock_preview.scroll_end, animate=False
        )


class TestScrollBindings:
    def test_ctrl_u_binding_exists(self):
        """Ctrl+U binding should exist for preview scroll up."""
        from agent_hq.dashboard import AgentDashboard

        binding_keys = [b.key for b in AgentDashboard.BINDINGS]
        assert "ctrl+u" in binding_keys

    def test_ctrl_d_binding_exists(self):
        """Ctrl+D binding should exist for preview scroll down."""
        from agent_hq.dashboard import AgentDashboard

        binding_keys = [b.key for b in AgentDashboard.BINDINGS]
        assert "ctrl+d" in binding_keys

    def test_scroll_up_action_exists(self):
        """action_preview_scroll_up should be defined."""
        from agent_hq.dashboard import AgentDashboard

        assert hasattr(AgentDashboard, "action_preview_scroll_up")
        assert callable(AgentDashboard.action_preview_scroll_up)

    def test_scroll_down_action_exists(self):
        """action_preview_scroll_down should be defined."""
        from agent_hq.dashboard import AgentDashboard

        assert hasattr(AgentDashboard, "action_preview_scroll_down")
        assert callable(AgentDashboard.action_preview_scroll_down)


class TestAutoRefreshPreview:
    def test_apply_refresh_triggers_preview_reload(self):
        """_apply_refresh should reload preview when preview is visible."""
        from unittest.mock import MagicMock, patch

        from agent_hq.dashboard import AgentDashboard

        app = AgentDashboard.__new__(AgentDashboard)
        app._agents = []
        app._inactive_pane_ids = set()
        app._last_applied_ts = 0.0
        app._sort_mode = "priority"
        app._search_query = ""
        app._preview_pane_id = "%5"
        app._history_pane_id = None
        app._row_pane_ids = []
        app._current_cols = ("", "Repo", "Branch", "Type", "Pane")

        mock_preview = MagicMock()
        mock_preview.display = True

        with (
            patch.object(app, "_rebuild_table"),
            patch.object(app, "_selected_pane_id", return_value="%5"),
            patch.object(app, "query_one", return_value=mock_preview),
            patch.object(app, "_load_preview") as mock_load,
        ):
            app._apply_refresh([], set(), set(), 1.0)

        mock_load.assert_called_once_with("%5")

    def test_apply_refresh_skips_preview_when_hidden(self):
        """_apply_refresh should NOT reload preview when preview is hidden."""
        from unittest.mock import MagicMock, patch

        from agent_hq.dashboard import AgentDashboard

        app = AgentDashboard.__new__(AgentDashboard)
        app._agents = []
        app._inactive_pane_ids = set()
        app._last_applied_ts = 0.0
        app._sort_mode = "priority"
        app._search_query = ""
        app._preview_pane_id = "%5"
        app._history_pane_id = None
        app._row_pane_ids = []
        app._current_cols = ("", "Repo", "Branch", "Type", "Pane")

        mock_preview = MagicMock()
        mock_preview.display = False

        with (
            patch.object(app, "_rebuild_table"),
            patch.object(app, "_selected_pane_id", return_value="%5"),
            patch.object(app, "query_one", return_value=mock_preview),
            patch.object(app, "_load_preview") as mock_load,
        ):
            app._apply_refresh([], set(), set(), 1.0)

        mock_load.assert_not_called()


class TestApplyRefreshMonotonic:
    """_apply_refresh must drop snapshots whose timestamp is older than the
    last applied — guards against out-of-order worker completions stomping
    fresh state with stale data (issue #74)."""

    @staticmethod
    def _make_app():
        from agent_hq.dashboard import AgentDashboard

        app = AgentDashboard.__new__(AgentDashboard)
        app._agents = []
        app._inactive_pane_ids = set()
        app._pinned_keys = set()
        app._last_applied_ts = 0.0
        app._sort_mode = "priority"
        app._search_query = ""
        app._preview_pane_id = None
        app._history_pane_id = None
        app._row_pane_ids = []
        app._current_cols = ("", "Repo", "Branch", "Type", "Pane")
        return app

    def test_drops_stale_timestamp(self):
        """A later apply with an older timestamp must not overwrite agents."""
        from unittest.mock import MagicMock, patch

        app = self._make_app()
        agents_v1 = [make_agent(pane_id="%1")]
        agents_v2 = [make_agent(pane_id="%2")]

        mock_preview = MagicMock()
        mock_preview.display = False

        with (
            patch.object(app, "_rebuild_table") as mock_rebuild,
            patch.object(app, "_selected_pane_id", return_value=None),
            patch.object(app, "query_one", return_value=mock_preview),
            patch.object(app, "_load_preview"),
        ):
            app._apply_refresh(agents_v2, set(), set(), 2.0)
            app._apply_refresh(agents_v1, set(), set(), 1.0)

        assert app._agents is agents_v2
        assert mock_rebuild.call_count == 1

    def test_accepts_newer_timestamp(self):
        """A later apply with a newer timestamp replaces previous agents."""
        from unittest.mock import MagicMock, patch

        app = self._make_app()
        agents_v1 = [make_agent(pane_id="%1")]
        agents_v2 = [make_agent(pane_id="%2")]

        mock_preview = MagicMock()
        mock_preview.display = False

        with (
            patch.object(app, "_rebuild_table") as mock_rebuild,
            patch.object(app, "_selected_pane_id", return_value=None),
            patch.object(app, "query_one", return_value=mock_preview),
            patch.object(app, "_load_preview"),
        ):
            app._apply_refresh(agents_v1, set(), set(), 1.0)
            app._apply_refresh(agents_v2, set(), set(), 2.0)

        assert app._agents is agents_v2
        assert mock_rebuild.call_count == 2

    def test_accepts_equal_timestamp(self):
        """Apply with timestamp equal to last applied still applies.

        Action handlers (mute/kill/rename) trigger an immediate refresh
        that often hits the same cache timestamp as a queued interval
        refresh — we need that second apply to land so the action's
        freshly-loaded inactive_records are reflected without waiting
        for the next daemon cache write (~2s).
        """
        from unittest.mock import MagicMock, patch

        app = self._make_app()
        agents_first = [make_agent(pane_id="%1")]
        agents_second = [make_agent(pane_id="%2")]

        mock_preview = MagicMock()
        mock_preview.display = False

        with (
            patch.object(app, "_rebuild_table") as mock_rebuild,
            patch.object(app, "_selected_pane_id", return_value=None),
            patch.object(app, "query_one", return_value=mock_preview),
            patch.object(app, "_load_preview"),
        ):
            app._apply_refresh(agents_first, set(), set(), 5.0)
            app._apply_refresh(agents_second, set(), set(), 5.0)

        assert app._agents is agents_second
        assert mock_rebuild.call_count == 2


class TestDashboardRefreshData:
    def test_refresh_data_uses_cached_agents(self):
        """_refresh_data should load agents via cache accessor and pass timestamp through."""
        from unittest.mock import MagicMock, patch

        from agent_hq.dashboard import AgentDashboard

        agent = make_agent(pane_id="%5")
        app = AgentDashboard.__new__(AgentDashboard)
        app._agent_filter = None
        app._repo_filter = None
        app._pinned_store = MagicMock()
        app._pinned_store.keys.return_value = set()

        with (
            patch(
                "agent_hq.dashboard.get_agents_cached", return_value=([agent], 12345.0)
            ) as mock_get,
            patch("agent_hq.dashboard.load_inactive_state", return_value={}),
            patch.object(app, "call_from_thread") as mock_call,
        ):
            app._refresh_data.__wrapped__(app)

        mock_get.assert_called_once_with()
        mock_call.assert_called_once_with(app._apply_refresh, [agent], set(), set(), 12345.0)

    def test_refresh_data_migrates_legacy_pins(self, tmp_path):
        """#205 Codex review: the refresh worker re-keys a legacy-form pin
        on the captured stable id BEFORE reading the pinned set for the
        render, so the migrated key is what the UI sees."""
        from unittest.mock import patch

        from agent_hq.dashboard import AgentDashboard
        from agent_hq.pinned_store import PinnedSessionsStore

        agent = make_agent(pane_id="%5", agent_type=AgentType.CODEX)
        agent.codex_thread_id = "uuid-7"
        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add(agent.legacy_stable_key)

        app = AgentDashboard.__new__(AgentDashboard)
        app._agent_filter = None
        app._repo_filter = None
        app._pinned_store = store

        with (
            patch("agent_hq.dashboard.get_agents_cached", return_value=([agent], 12345.0)),
            patch("agent_hq.dashboard.load_inactive_state", return_value={}),
            patch.object(app, "call_from_thread") as mock_call,
        ):
            app._refresh_data.__wrapped__(app)

        assert store.keys() == {"uuid-7"}
        pinned_keys = mock_call.call_args.args[3]
        assert pinned_keys == {"uuid-7"}


class TestRunDashboard:
    def test_run_dashboard_is_callable(self):
        from agent_hq.dashboard import run_dashboard

        assert callable(run_dashboard)

    def test_auto_starts_daemon_when_not_running(self):
        """run_dashboard should start daemon silently before launching the app."""
        from unittest.mock import MagicMock, patch

        from agent_hq.dashboard import run_dashboard

        mock_app = MagicMock()
        mock_app.run.return_value = None

        with (
            patch("agent_hq.dashboard.is_daemon_running", return_value=False),
            patch("agent_hq.dashboard.start_daemon") as mock_start,
            patch("agent_hq.dashboard.AgentDashboard", return_value=mock_app),
        ):
            run_dashboard()

        mock_start.assert_called_once_with(silent=True)
        mock_app.run.assert_called_once_with()

    def test_skips_auto_start_when_daemon_already_running(self):
        """run_dashboard should not start daemon when one is already live."""
        from unittest.mock import MagicMock, patch

        from agent_hq.dashboard import run_dashboard

        mock_app = MagicMock()
        mock_app.run.return_value = None

        with (
            patch("agent_hq.dashboard.is_daemon_running", return_value=True),
            patch("agent_hq.dashboard.start_daemon") as mock_start,
            patch("agent_hq.dashboard.AgentDashboard", return_value=mock_app),
        ):
            run_dashboard()

        mock_start.assert_not_called()
        mock_app.run.assert_called_once_with()

    def test_auto_start_failure_does_not_block_dashboard(self):
        """Dashboard should still launch if daemon auto-start raises."""
        from unittest.mock import MagicMock, patch

        from agent_hq.dashboard import run_dashboard

        mock_app = MagicMock()
        mock_app.run.return_value = None

        with (
            patch("agent_hq.dashboard.is_daemon_running", return_value=False),
            patch("agent_hq.dashboard.start_daemon", side_effect=OSError("fork failed")),
            patch("agent_hq.dashboard.AgentDashboard", return_value=mock_app),
        ):
            run_dashboard()

        mock_app.run.assert_called_once_with()


class TestCheckAction:
    """check_action should hide agent-specific bindings when dispatch screen is active."""

    def _make_app(self):
        """Create an AgentDashboard with a mock dispatch screen."""
        from unittest.mock import MagicMock

        app = AgentDashboard.__new__(AgentDashboard)
        app._dispatch_screen = MagicMock()
        return app

    def test_agent_actions_enabled_on_default_screen(self):
        """Agent-only actions should be visible when on the agents view."""
        from unittest.mock import PropertyMock, patch

        app = self._make_app()
        # self.screen returns something other than _dispatch_screen (default screen)
        default_screen = object()
        with patch.object(
            type(app), "screen", new_callable=PropertyMock, return_value=default_screen
        ):
            for action in AgentDashboard._AGENTS_ONLY_ACTIONS:
                result = app.check_action(action, ())
                assert result is True, f"Expected True for {action} on default screen"

    def test_agent_actions_disabled_on_dispatch_screen(self):
        """Agent-only actions should be hidden when dispatch screen is active."""
        from unittest.mock import PropertyMock, patch

        app = self._make_app()
        with patch.object(
            type(app), "screen", new_callable=PropertyMock, return_value=app._dispatch_screen
        ):
            for action in AgentDashboard._AGENTS_ONLY_ACTIONS:
                result = app.check_action(action, ())
                assert result is False, f"Expected False for {action} on dispatch screen"

    def test_shared_actions_always_enabled(self):
        """Shared actions like quit should be visible regardless of screen."""
        from unittest.mock import PropertyMock, patch

        app = self._make_app()
        with patch.object(
            type(app), "screen", new_callable=PropertyMock, return_value=app._dispatch_screen
        ):
            assert app.check_action("quit", ()) is True
            assert app.check_action("start_search", ()) is True
            assert app.check_action("refresh", ()) is True

    def test_check_action_safe_before_mount(self):
        """check_action should return True before _dispatch_screen exists."""
        app = AgentDashboard.__new__(AgentDashboard)
        # No _dispatch_screen attribute
        assert app.check_action("toggle_sort", ()) is True

    def test_on_key_skips_when_dispatch_active(self):
        """on_key should return early when dispatch screen is active, never querying widgets."""
        from unittest.mock import MagicMock, PropertyMock, patch

        app = self._make_app()

        event = MagicMock()
        event.key = "left_square_bracket"
        event.character = "["

        with patch.object(
            type(app), "screen", new_callable=PropertyMock, return_value=app._dispatch_screen
        ):
            # query_one should never be called if on_key returns early
            with patch.object(app, "query_one") as mock_query:
                app.on_key(event)

            mock_query.assert_not_called()


class TestCliDashboardFlag:
    def test_parser_accepts_dashboard_flag(self):
        from agent_hq.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["--dashboard"])
        assert args.dashboard is True

    def test_parser_default_no_dashboard(self):
        from agent_hq.cli import create_parser

        parser = create_parser()
        args = parser.parse_args([])
        assert args.dashboard is False


class TestFormatPhaseCell:
    def test_renders_phase_with_color(self):
        cell = format_phase_cell(DevPhase.IMPLEMENT)
        assert "implement" in cell.lower() or "🔨" in cell
        # Rich markup with color
        assert cell.startswith("[") and cell.endswith("]")

    def test_renders_empty_for_none(self):
        assert format_phase_cell(None) == ""

    def test_design_phase_has_explicit_color(self):
        """#108: the `design` phase must have a real color, not the
        `white` fallback (and no stale brainstorm/plan keys linger)."""
        from agent_hq.dashboard import PHASE_COLORS

        assert PHASE_COLORS.get("design") == "magenta"
        assert "brainstorm" not in PHASE_COLORS
        assert "plan" not in PHASE_COLORS


class TestRowBuildersIncludePhase:
    def test_priority_row_has_phase_key(self):
        agent = Agent(
            pane_id="%1",
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/r",
            repo_root="/r",
            repo_name="r",
            branch="feat",
            raw_command="claude",
            state=AgentState.RUNNING,
            phase_snapshot=_phase_snapshot(DevPhase.IMPLEMENT),
        )
        rows = build_priority_rows([agent])
        assert rows[0]["phase"] == format_phase_cell(DevPhase.IMPLEMENT)

    def test_priority_row_has_is_header_false(self):
        agent = make_phased_agent(pane_id="%1", phase=DevPhase.IMPLEMENT)
        rows = build_priority_rows([agent])
        assert rows[0]["is_header"] is False

    def test_table_row_has_is_header_false(self):
        agent = make_agent(pane_id="%5", repo_name="r", branch="main")
        groups = [make_group("r", [(None, "main", [agent])])]
        rows = build_table_rows(groups)
        # rows[0] is the repo header; the agent row follows.
        assert rows[1]["is_header"] is False

    def test_phase_row_has_phase_key(self):
        agent = make_phased_agent(pane_id="%1", phase=DevPhase.IMPLEMENT)
        rows = build_phase_rows([agent])
        agent_rows = [r for r in rows if not r["is_separator"] and not r["is_header"]]
        assert agent_rows[0]["phase"] == format_phase_cell(DevPhase.IMPLEMENT)


class TestBuildPhaseRows:
    """build_phase_rows: in-place phase grouping that reuses the standard
    multi-column row shape + an extra non-selectable header row per phase."""

    def test_empty_agents_returns_empty(self):
        assert build_phase_rows([]) == []

    def test_header_row_per_nonempty_phase(self):
        agents = [
            make_phased_agent(pane_id="%1", phase=DevPhase.IMPLEMENT),
            make_phased_agent(pane_id="%2", phase=DevPhase.REVIEW),
        ]
        rows = build_phase_rows(agents)
        headers = [r for r in rows if r.get("is_header")]
        assert len(headers) == 2
        for h in headers:
            assert h["is_separator"] is False
            assert h["pane_id"] is None
            assert "header_text" in h

    def test_empty_phases_omitted(self):
        rows = build_phase_rows([make_phased_agent(pane_id="%1", phase=DevPhase.QA)])
        headers = [r for r in rows if r.get("is_header")]
        assert len(headers) == 1
        assert "🧪" in headers[0]["header_text"] or "qa" in headers[0]["header_text"].lower()

    def test_header_includes_count(self):
        agents = [
            make_phased_agent(pane_id="%1", phase=DevPhase.IMPLEMENT),
            make_phased_agent(pane_id="%2", phase=DevPhase.IMPLEMENT),
        ]
        rows = build_phase_rows(agents)
        header = next(r for r in rows if r.get("is_header"))
        assert "(2)" in header["header_text"]

    def test_phases_in_devphase_declaration_order(self):
        # Provide them out of order; expect DESIGN before IMPLEMENT before DONE.
        agents = [
            make_phased_agent(pane_id="%1", phase=DevPhase.DONE),
            make_phased_agent(pane_id="%2", phase=DevPhase.DESIGN),
            make_phased_agent(pane_id="%3", phase=DevPhase.IMPLEMENT),
        ]
        rows = build_phase_rows(agents)
        header_phases = [r["header_text"] for r in rows if r.get("is_header")]
        # Match on emoji (unique per phase) — labels may be fit-truncated.
        assert DevPhase.DESIGN.emoji in header_phases[0]
        assert DevPhase.IMPLEMENT.emoji in header_phases[1]
        assert DevPhase.DONE.emoji in header_phases[2]

    def test_agent_rows_grouped_under_their_header(self):
        a = make_phased_agent(pane_id="%1", phase=DevPhase.IMPLEMENT)
        b = make_phased_agent(pane_id="%2", phase=DevPhase.REVIEW)
        rows = build_phase_rows([a, b])
        # Find header indices, ensure the agent row directly follows its header.
        seq = [
            ("H", r["header_text"])
            if r.get("is_header")
            else ("S",)
            if r["is_separator"]
            else ("A", r["pane_id"])
            for r in rows
        ]
        # implement header → %1 agent, separator, review header → %2 agent
        assert seq[0][0] == "H" and DevPhase.IMPLEMENT.emoji in seq[0][1]
        assert seq[1] == ("A", "%1")
        assert seq[2] == ("S",)
        assert seq[3][0] == "H" and DevPhase.REVIEW.emoji in seq[3][1]
        assert seq[4] == ("A", "%2")

    def test_within_phase_waiting_first(self):
        running = make_phased_agent(
            pane_id="%1", phase=DevPhase.IMPLEMENT, state=AgentState.RUNNING
        )
        waiting = make_phased_agent(
            pane_id="%2", phase=DevPhase.IMPLEMENT, state=AgentState.WAITING
        )
        rows = build_phase_rows([running, waiting])
        agent_rows = [r for r in rows if not r["is_separator"] and not r["is_header"]]
        assert [r["pane_id"] for r in agent_rows] == ["%2", "%1"]

    def test_muted_agent_dimmed_and_last_within_phase(self):
        active = make_phased_agent(pane_id="%1", phase=DevPhase.IMPLEMENT, state=AgentState.RUNNING)
        muted = make_phased_agent(pane_id="%2", phase=DevPhase.IMPLEMENT, state=AgentState.IDLE)
        rows = build_phase_rows([active, muted], inactive_pane_ids={"%2"})
        agent_rows = [r for r in rows if not r["is_separator"] and not r["is_header"]]
        assert [r["pane_id"] for r in agent_rows] == ["%1", "%2"]
        assert "dim" in agent_rows[1]["repo"]

    def test_agent_row_dict_shape_matches_priority_rows(self):
        agent = make_phased_agent(pane_id="%1", phase=DevPhase.IMPLEMENT)
        prow = build_priority_rows([agent])[0]
        phase_agent_row = next(
            r for r in build_phase_rows([agent]) if not r["is_separator"] and not r.get("is_header")
        )
        assert set(phase_agent_row.keys()) == set(prow.keys())

    def test_no_separator_before_first_group(self):
        rows = build_phase_rows([make_phased_agent(pane_id="%1", phase=DevPhase.QA)])
        assert rows[0].get("is_header") is True

    def test_unphased_agent_buckets_to_adhoc(self):
        rows = build_phase_rows([make_phased_agent(pane_id="%1", phase=None)])
        header = next(r for r in rows if r.get("is_header"))
        assert "·" in header["header_text"] or "adhoc" in header["header_text"].lower()


class TestToggleSortCycle:
    """`s` cycles priority → phase → repo → priority. The `v` view binding
    and _view_mode/kanban single-column path are gone (issue #97)."""

    def test_v_binding_removed(self):
        binding_keys = [b.key for b in AgentDashboard.BINDINGS]
        assert "v" not in binding_keys

    def test_toggle_view_action_removed(self):
        assert not hasattr(AgentDashboard, "action_toggle_view")
        assert "toggle_view" not in AgentDashboard._AGENTS_ONLY_ACTIONS

    def test_kanban_builder_removed(self):
        assert not hasattr(AgentDashboard, "_build_kanban_rows")

    def test_default_sort_mode_is_phase(self):
        app = AgentDashboard.__new__(AgentDashboard)
        AgentDashboard.__init__(app)
        assert app._sort_mode == "phase"

    def test_toggle_sort_cycles_three_modes(self):
        from unittest.mock import patch

        app = AgentDashboard.__new__(AgentDashboard)
        AgentDashboard.__init__(app)
        # _persist_sort_mode is a @work worker — needs a running app (#176).
        with patch.object(app, "_rebuild_table"), patch.object(app, "_persist_sort_mode"):
            assert app._sort_mode == "phase"
            app.action_toggle_sort()
            assert app._sort_mode == "repo"
            app.action_toggle_sort()
            assert app._sort_mode == "priority"
            app.action_toggle_sort()
            assert app._sort_mode == "phase"

    def test_pilot_press_s_cycles_sort_mode(self):
        from unittest.mock import patch

        async def run_test() -> None:
            with patch("agent_hq.dashboard.is_daemon_running", return_value=True):
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    assert app._sort_mode == "phase"
                    await pilot.press("s")
                    await pilot.pause()
                    assert app._sort_mode == "repo"
                    await pilot.press("s")
                    await pilot.pause()
                    assert app._sort_mode == "priority"
                    await pilot.press("s")
                    await pilot.pause()
                    assert app._sort_mode == "phase"

        asyncio.run(run_test())


class TestInitialFocusHelper:
    """initial_focus_pane_id picks the agent in the TUI's own tmux session:
    highest access time wins, table order breaks ties (#176)."""

    def _agents(self):
        a1 = make_agent("%1")
        a2 = make_agent("%2")
        a2.session_name = "mysess"
        a3 = make_agent("%3")
        a3.session_name = "mysess"
        return a1, a2, a3

    def test_none_session_name_returns_none(self):
        from agent_hq.dashboard import initial_focus_pane_id

        a1, a2, a3 = self._agents()
        assert initial_focus_pane_id([a1, a2], None, {}, ["%1", "%2"]) is None

    def test_no_matching_session_returns_none(self):
        from agent_hq.dashboard import initial_focus_pane_id

        a1, *_ = self._agents()
        assert initial_focus_pane_id([a1], "mysess", {}, ["%1"]) is None

    def test_single_match_returns_its_pane(self):
        from agent_hq.dashboard import initial_focus_pane_id

        a1, a2, _ = self._agents()
        assert initial_focus_pane_id([a1, a2], "mysess", {}, ["%1", "%2"]) == "%2"

    def test_multi_match_most_recent_access_wins(self):
        from agent_hq.dashboard import initial_focus_pane_id

        a1, a2, a3 = self._agents()
        access = {"%2": 100.0, "%3": 200.0}
        rows = [None, "%1", "%2", "%3"]
        assert initial_focus_pane_id([a1, a2, a3], "mysess", access, rows) == "%3"

    def test_multi_match_tie_breaks_on_table_order(self):
        from agent_hq.dashboard import initial_focus_pane_id

        a1, a2, a3 = self._agents()
        rows = [None, "%3", "%2", "%1"]
        assert initial_focus_pane_id([a1, a2, a3], "mysess", {}, rows) == "%3"

    def test_match_missing_from_rows_is_skipped(self):
        from agent_hq.dashboard import initial_focus_pane_id

        a1, a2, a3 = self._agents()
        # %3 matches but was filtered out of the rendered rows.
        assert initial_focus_pane_id([a1, a2, a3], "mysess", {"%3": 200.0}, ["%1", "%2"]) == "%2"


class TestTuiSessionName:
    """_tui_session_name resolves the TUI's own tmux session via $TMUX_PANE."""

    def test_outside_tmux_returns_none_without_shelling_out(self, monkeypatch):
        from unittest.mock import patch

        from agent_hq.dashboard import _tui_session_name

        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.delenv("TMUX", raising=False)
        with patch("agent_hq.dashboard.subprocess.run") as run:
            assert _tui_session_name() is None
        run.assert_not_called()

    def test_popup_fallback_when_tmux_pane_unset(self, monkeypatch):
        # display-popup jobs get $TMUX but not $TMUX_PANE (tmux 3.6b) —
        # fall back to the attached client's active session via a
        # no-target display-message.
        from unittest.mock import MagicMock, patch

        from agent_hq.dashboard import _tui_session_name

        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1,0")
        result = MagicMock(returncode=0, stdout="popupsess\n")
        with patch("agent_hq.dashboard.subprocess.run", return_value=result) as run:
            assert _tui_session_name() == "popupsess"
        assert "-t" not in run.call_args[0][0]

    def test_popup_fallback_failure_returns_none(self, monkeypatch):
        from unittest.mock import patch

        from agent_hq.dashboard import _tui_session_name

        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1,0")
        with patch("agent_hq.dashboard.subprocess.run", side_effect=OSError):
            assert _tui_session_name() is None

    def test_resolves_session_name_via_tmux(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        from agent_hq.dashboard import _tui_session_name

        monkeypatch.setenv("TMUX_PANE", "%9")
        result = MagicMock(returncode=0, stdout="mysess\n")
        with patch("agent_hq.dashboard.subprocess.run", return_value=result) as run:
            assert _tui_session_name() == "mysess"
        assert "%9" in run.call_args[0][0]

    def test_tmux_failure_returns_none(self, monkeypatch):
        from unittest.mock import patch

        from agent_hq.dashboard import _tui_session_name

        monkeypatch.setenv("TMUX_PANE", "%9")
        with patch("agent_hq.dashboard.subprocess.run", side_effect=OSError):
            assert _tui_session_name() is None


class TestInitialFocusPilot:
    """On open in phase view, the cursor lands on the agent in the TUI's own
    tmux session — one shot, only in phase mode, surviving empty first
    refreshes (#176)."""

    def _agents(self):
        a1 = make_agent("%1", repo_name="r", branch="b1")
        a2 = make_agent("%2", repo_name="r", branch="b2")
        a2.session_name = "mysess"
        return a1, a2

    def test_pilot_focus_lands_on_same_session_agent(self):
        from unittest.mock import patch

        a1, a2 = self._agents()

        async def run_test() -> None:
            with (
                patch("agent_hq.dashboard.is_daemon_running", return_value=True),
                patch("agent_hq.dashboard.get_agents_cached", return_value=([a1, a2], 1.0)),
                patch("agent_hq.dashboard._tui_session_name", return_value="mysess"),
            ):
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    assert app._selected_pane_id() == "%2"
                    assert app._initial_focus_pending is False

        asyncio.run(run_test())

    def test_pilot_no_match_falls_back_to_first_selectable(self):
        from unittest.mock import patch

        a1, a2 = self._agents()

        async def run_test() -> None:
            with (
                patch("agent_hq.dashboard.is_daemon_running", return_value=True),
                patch("agent_hq.dashboard.get_agents_cached", return_value=([a1, a2], 1.0)),
                patch("agent_hq.dashboard._tui_session_name", return_value="elsewhere"),
            ):
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    assert app._selected_pane_id() == "%1"
                    assert app._initial_focus_pending is False

        asyncio.run(run_test())

    def test_pilot_focus_skipped_in_non_phase_mode(self, tmp_path):
        from unittest.mock import patch

        from agent_hq.ui_prefs import UiPrefsStore

        store = UiPrefsStore(path=tmp_path / "ui-prefs.json")
        store.set_tui_sort_mode("priority")
        a1, a2 = self._agents()

        async def run_test() -> None:
            with (
                patch("agent_hq.dashboard.is_daemon_running", return_value=True),
                patch("agent_hq.dashboard.get_agents_cached", return_value=([a1, a2], 1.0)),
                patch("agent_hq.dashboard._tui_session_name", return_value="mysess"),
            ):
                app = AgentDashboard(ui_prefs_store=store)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    assert app._selected_pane_id() == "%1"

        asyncio.run(run_test())

    def test_pilot_one_shot_survives_empty_first_refresh(self):
        from unittest.mock import patch

        a1, a2 = self._agents()

        async def run_test() -> None:
            with (
                patch("agent_hq.dashboard.is_daemon_running", return_value=True),
                patch("agent_hq.dashboard.get_agents_cached", return_value=([], 0.0)),
                patch("agent_hq.dashboard._tui_session_name", return_value="mysess"),
                # Keep the preview worker out of the way — not under test, and
                # its _show_preview would race the app shutdown.
                patch.object(AgentDashboard, "_load_preview"),
            ):
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    # Let the mount-time (empty-roster) refresh land.
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    # Nothing discovered yet — the one-shot must survive, and
                    # the worker already resolved the tmux session name.
                    assert app._initial_focus_pending is True
                    assert app._own_session_resolved is True
                    # Deliver the first non-empty refresh directly on the main
                    # thread (dispatching the exclusive _refresh_data worker
                    # here would race the 2s interval refresh's cancellation).
                    app._apply_refresh([a1, a2], set(), set(), 1.0)
                    await pilot.pause()
                    assert app._selected_pane_id() == "%2"
                    assert app._initial_focus_pending is False

        asyncio.run(run_test())

    def test_toggle_sort_clears_pending_focus(self):
        from unittest.mock import patch

        app = AgentDashboard.__new__(AgentDashboard)
        AgentDashboard.__init__(app)
        assert app._initial_focus_pending is True
        with patch.object(app, "_rebuild_table"), patch.object(app, "_persist_sort_mode"):
            app.action_toggle_sort()
        assert app._initial_focus_pending is False


async def _persisted_sort_mode(prefs_file, expected: str, timeout: float = 5.0):
    """Poll ``ui-prefs.json`` until the persist worker's write lands.

    Deliberately polls the file instead of ``app.workers.wait_for_complete()``.
    That waits on EVERY worker and re-raises any cancellation — including the
    by-design supersede of ``_refresh_data`` (``@work(exclusive=True)``, so a
    second refresh cancels the first). #205's on-mount ``launch.sh
    --capabilities`` probe shifted startup timing enough to make that overlap
    routine, which turned both tests below red.

    Filtering to the ``ui-prefs`` group is not a fix either: Textual prunes
    finished workers, so the group is usually already empty here — and
    ``wait_for_complete`` does ``workers or self``, treating an empty iterable
    as falsy and silently waiting on all workers again.

    The write reaching disk IS the contract these tests exist to pin, so wait
    on that directly.
    """
    from agent_hq.ui_prefs import UiPrefsStore

    deadline = time.monotonic() + timeout
    while True:
        actual = UiPrefsStore(path=prefs_file).get_tui_sort_mode()
        if actual == expected or time.monotonic() >= deadline:
            return actual
        await asyncio.sleep(0.01)


class TestSortModePersistence:
    """The sort mode survives restarts via UiPrefsStore (#176): restored in
    __init__, persisted on every `s` press."""

    def test_restores_sort_mode_from_store(self, tmp_path):
        from agent_hq.ui_prefs import UiPrefsStore

        store = UiPrefsStore(path=tmp_path / "ui-prefs.json")
        store.set_tui_sort_mode("repo")
        app = AgentDashboard.__new__(AgentDashboard)
        AgentDashboard.__init__(app, ui_prefs_store=store)
        assert app._sort_mode == "repo"

    def test_empty_store_falls_back_to_phase(self, tmp_path):
        from agent_hq.ui_prefs import UiPrefsStore

        store = UiPrefsStore(path=tmp_path / "ui-prefs.json")
        app = AgentDashboard.__new__(AgentDashboard)
        AgentDashboard.__init__(app, ui_prefs_store=store)
        assert app._sort_mode == "phase"

    def test_garbage_store_falls_back_to_phase(self, tmp_path):
        import json

        from agent_hq.ui_prefs import UiPrefsStore

        prefs_file = tmp_path / "ui-prefs.json"
        prefs_file.write_text(json.dumps({"tui_sort_mode": "bogus"}))
        app = AgentDashboard.__new__(AgentDashboard)
        AgentDashboard.__init__(app, ui_prefs_store=UiPrefsStore(path=prefs_file))
        assert app._sort_mode == "phase"

    def test_pilot_press_s_persists_new_mode(self, tmp_path):
        from unittest.mock import patch

        from agent_hq.ui_prefs import UiPrefsStore

        prefs_file = tmp_path / "ui-prefs.json"

        async def run_test() -> None:
            with patch("agent_hq.dashboard.is_daemon_running", return_value=True):
                app = AgentDashboard(ui_prefs_store=UiPrefsStore(path=prefs_file))
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("s")
                    assert await _persisted_sort_mode(prefs_file, "repo") == "repo"

        asyncio.run(run_test())

    def test_pilot_rapid_presses_persist_final_mode(self, tmp_path):
        """Racing persist workers must settle on the mode the TUI shows —
        the worker reads _sort_mode at execution time (Codex review)."""
        from unittest.mock import patch

        from agent_hq.ui_prefs import UiPrefsStore

        prefs_file = tmp_path / "ui-prefs.json"

        async def run_test() -> None:
            with patch("agent_hq.dashboard.is_daemon_running", return_value=True):
                app = AgentDashboard(ui_prefs_store=UiPrefsStore(path=prefs_file))
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    for _ in range(3):
                        await pilot.press("s")
                    assert app._sort_mode == "phase"  # cycled all the way round
                    assert await _persisted_sort_mode(prefs_file, "phase") == "phase"

        asyncio.run(run_test())


class TestPhaseHeaderText:
    """_phase_header_text fits the fixed-width Repo column so the trailing
    count stays visible even for long phase labels (review finding)."""

    def test_short_label_kept_full_with_count(self):
        from agent_hq.dashboard import _phase_header_text

        text = _phase_header_text(DevPhase.DESIGN, 2)
        assert "Design" in text
        assert "(2)" in text
        assert DevPhase.DESIGN.emoji in text

    def test_long_label_truncated_but_count_survives(self):
        from agent_hq.dashboard import _phase_header_text

        # "Implement" (9 chars) exceeds the header label budget so it is
        # shortened; the emoji still disambiguates and the count survives.
        text = _phase_header_text(DevPhase.IMPLEMENT, 3)
        assert "Implement" not in text
        assert "(3)" in text
        assert DevPhase.IMPLEMENT.emoji in text

    def test_visible_width_within_repo_column(self):
        import re

        from agent_hq.dashboard import _REPO_HEADER_NAME_WIDTH, _phase_header_text

        text = _phase_header_text(DevPhase.IMPLEMENT, 4)
        visible = re.sub(r"\[/?[a-z_ ]+\]", "", text)  # strip Rich markup
        # Count is the final token (not clipped) and the label fit the column.
        assert visible.rstrip().endswith("(4)")
        assert len(visible) <= _REPO_HEADER_NAME_WIDTH


class TestHeaderRowNavigation:
    """Regression (review finding): the cursor must never strand on a
    non-selectable section header, including the leading header at row 0."""

    def test_cursor_up_from_first_agent_skips_leading_header(self):
        from unittest.mock import patch

        agents = [
            make_phased_agent("%1", DevPhase.IMPLEMENT, AgentState.RUNNING),
            make_phased_agent("%2", DevPhase.REVIEW, AgentState.RUNNING),
        ]

        async def run_test() -> None:
            with (
                patch("agent_hq.dashboard.is_daemon_running", return_value=True),
                patch("agent_hq.dashboard.get_agents_cached", return_value=(agents, 1.0)),
                patch("agent_hq.dashboard.get_pane_content", return_value=""),
            ):
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    app._sort_mode = "phase"
                    app._rebuild_table()
                    await pilot.pause()
                    # Cursor sits on the first agent (row 0 is the phase header).
                    assert app._selected_pane_id() == "%1"
                    # Pressing up would land on the row-0 header; the fix must
                    # reverse so a real agent stays selected.
                    await pilot.press("k")
                    await pilot.pause()
                    assert app._selected_pane_id() is not None, (
                        "cursor stranded on a non-selectable header row"
                    )

        asyncio.run(run_test())

    def test_cursor_down_then_up_stays_selectable_in_repo_mode(self):
        from unittest.mock import patch

        agents = [
            make_phased_agent("%1", DevPhase.IMPLEMENT, repo_name="repo-a"),
            make_phased_agent("%2", DevPhase.REVIEW, repo_name="repo-b"),
        ]

        async def run_test() -> None:
            with (
                patch("agent_hq.dashboard.is_daemon_running", return_value=True),
                patch("agent_hq.dashboard.get_agents_cached", return_value=(agents, 1.0)),
                patch("agent_hq.dashboard.get_pane_content", return_value=""),
                patch("agent_hq.dashboard.group_by_worktree") as mock_group,
            ):
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    # Two single-agent repo groups → header rows at 0 and mid.
                    mock_group.return_value = [
                        make_group("repo-a", [(None, "main", [agents[0]])]),
                        make_group("repo-b", [(None, "dev", [agents[1]])]),
                    ]
                    app._sort_mode = "repo"
                    app._rebuild_table()
                    await pilot.pause()
                    await pilot.press("G")  # jump to bottom (last agent)
                    await pilot.pause()
                    assert app._selected_pane_id() is not None
                    await pilot.press("k")
                    await pilot.pause()
                    assert app._selected_pane_id() is not None

        asyncio.run(run_test())


class TestRenameBinding:
    """Issue #87: tmux claims F2 (search-down) at the terminal layer, so the
    old `f2` binding never reached Textual. The rename action is now bound
    to `R` (capital R) which passes through tmux cleanly and doesn't
    collide with the lowercase `r` Refresh binding."""

    def test_capital_r_binding_registered(self):
        binding_keys = [b.key for b in AgentDashboard.BINDINGS]
        assert "R" in binding_keys, (
            "R binding missing from AgentDashboard.BINDINGS — Footer won't "
            "show Rename and pressing Shift+R will do nothing."
        )

    def test_f2_binding_removed(self):
        binding_keys = [b.key for b in AgentDashboard.BINDINGS]
        assert "f2" not in binding_keys, (
            "f2 binding still present — tmux's default search-down keymap "
            "intercepts F2, so this binding can never fire inside tmux."
        )

    def test_rename_action_still_agents_only(self):
        assert "rename_session" in AgentDashboard._AGENTS_ONLY_ACTIONS

    def test_action_rename_session_callable(self):
        assert callable(AgentDashboard.action_rename_session)

    def test_lowercase_r_still_refresh(self):
        """Sanity: changing F2 → R must not stomp on the lowercase `r`
        Refresh binding."""
        refresh_bindings = [b for b in AgentDashboard.BINDINGS if b.key == "r"]
        assert len(refresh_bindings) == 1
        assert refresh_bindings[0].action == "refresh"


class TestGroupAgentsByPhase:
    """group_agents_by_phase must include EVERY DevPhase value as a bucket
    so kanban view can route any agent without KeyError."""

    def test_all_devphases_are_keys(self):
        from agent_hq.dashboard import group_agents_by_phase
        from agent_hq.models import DevPhase

        groups = group_agents_by_phase([])
        for phase in DevPhase:
            assert phase in groups, f"{phase} missing from group_agents_by_phase"

    def test_agent_with_each_phase_does_not_raise(self):
        """Routing an agent into every phase bucket — proves the kanban
        crash on DevPhase.READY can't recur."""
        from agent_hq.dashboard import group_agents_by_phase
        from agent_hq.models import Agent, AgentState, AgentType, DevPhase

        for phase in DevPhase:
            agent = Agent(
                pane_id="%1",
                session_name="s",
                window_index=0,
                pane_index=0,
                agent_type=AgentType.CLAUDE,
                cwd="/r",
                raw_command="claude",
                state=AgentState.IDLE,
                phase_snapshot=_phase_snapshot(phase),
            )
            groups = group_agents_by_phase([agent])
            assert agent in groups[phase]

    def test_groups_by_phase_with_unspecified_buckets_to_adhoc(self):
        from agent_hq.dashboard import group_agents_by_phase
        from agent_hq.models import Agent, AgentState, AgentType, DevPhase

        def make(pid, phase):
            return Agent(
                pane_id=pid,
                session_name="s",
                window_index=0,
                pane_index=0,
                agent_type=AgentType.CLAUDE,
                cwd="/r",
                raw_command="claude",
                state=AgentState.IDLE,
                phase_snapshot=_phase_snapshot(phase),
            )

        a = make("%1", DevPhase.IMPLEMENT)
        b = make("%2", DevPhase.REVIEW)
        c = make("%3", DevPhase.IMPLEMENT)
        d = make("%4", None)
        groups = group_agents_by_phase([a, b, c, d])
        assert groups[DevPhase.IMPLEMENT] == [a, c]
        assert groups[DevPhase.REVIEW] == [b]
        assert d in groups[DevPhase.ADHOC]


class TestKillConfirmationTimeout:
    """#14: `_pending_kill` must auto-clear when the 5 s confirmation toast
    expires, instead of silently swallowing the next unrelated keypress."""

    def test_confirm_kill_arms_auto_clear_timer(self):
        from unittest.mock import MagicMock, patch

        async def run_test() -> None:
            with (
                patch("agent_hq.dashboard.is_daemon_running", return_value=True),
                patch("agent_hq.dashboard.get_agents_cached", return_value=([], 0.0)),
            ):
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    timer = MagicMock()
                    with (
                        patch.object(app, "set_timer", return_value=timer) as set_timer,
                        patch.object(app, "_selected_pane_id", return_value="%1"),
                    ):
                        app.action_confirm_kill()
                    assert app._pending_kill == "%1"
                    set_timer.assert_called_once_with(
                        AgentDashboard._KILL_CONFIRM_TIMEOUT, app._clear_pending_kill
                    )
                    assert app._pending_kill_timer is timer

        asyncio.run(run_test())

    def test_timer_fire_auto_clears_pending_kill(self):
        """End-to-end through Textual's real timer: after the timeout the
        pending kill is gone and a late 'y' no longer kills anything."""
        from unittest.mock import patch

        async def run_test() -> None:
            with (
                patch("agent_hq.dashboard.is_daemon_running", return_value=True),
                patch("agent_hq.dashboard.get_agents_cached", return_value=([], 0.0)),
                patch("agent_hq.dashboard.kill_agent") as kill,
                patch.object(AgentDashboard, "_KILL_CONFIRM_TIMEOUT", 0.05),
            ):
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    with patch.object(app, "_selected_pane_id", return_value="%1"):
                        app.action_confirm_kill()
                    assert app._pending_kill == "%1"
                    await pilot.pause(0.5)
                    assert app._pending_kill is None
                    assert app._pending_kill_timer is None
                    await pilot.press("y")
                    await pilot.pause()
                    kill.assert_not_called()

        asyncio.run(run_test())

    def test_keypress_resolution_stops_timer(self):
        """Cancelling via a keypress must stop the armed timer so a stale
        fire can't clear a later confirmation early."""
        from unittest.mock import MagicMock, patch

        async def run_test() -> None:
            with (
                patch("agent_hq.dashboard.is_daemon_running", return_value=True),
                patch("agent_hq.dashboard.get_agents_cached", return_value=([], 0.0)),
            ):
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    timer = MagicMock()
                    app._pending_kill = "%1"
                    app._pending_kill_timer = timer
                    await pilot.press("n")
                    await pilot.pause()
                    assert app._pending_kill is None
                    timer.stop.assert_called_once()
                    assert app._pending_kill_timer is None

        asyncio.run(run_test())

    def test_second_confirm_stops_previous_timer(self):
        """Pressing K again re-arms: the first timer must not survive to
        clear the second confirmation early."""
        from unittest.mock import MagicMock, patch

        async def run_test() -> None:
            with (
                patch("agent_hq.dashboard.is_daemon_running", return_value=True),
                patch("agent_hq.dashboard.get_agents_cached", return_value=([], 0.0)),
            ):
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    first, second = MagicMock(), MagicMock()
                    with (
                        patch.object(app, "set_timer", side_effect=[first, second]),
                        patch.object(app, "_selected_pane_id", side_effect=["%1", "%2"]),
                    ):
                        app.action_confirm_kill()
                        app.action_confirm_kill()
                    assert app._pending_kill == "%2"
                    first.stop.assert_called_once()
                    assert app._pending_kill_timer is second

        asyncio.run(run_test())


class TestPreviewHistoryMode:
    """#171: ``h`` toggles the preview panel between the live capture
    tail and transcript-local history (paged from the agent's Claude
    transcript JSONL — zero pane interaction)."""

    @staticmethod
    def _agent_with_transcript(records, pane_id="%1", session_id="hist-1"):
        import json as _json
        from pathlib import Path

        from agent_hq.transcript import transcript_path_for

        agent = make_agent(pane_id)
        agent.claude_transcript_session_id = session_id
        path = Path(transcript_path_for(agent.cwd, session_id)[0])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(_json.dumps(r) for r in records) + "\n")
        return agent

    @staticmethod
    def _user_record(text):
        return {"type": "user", "message": {"content": text}}

    @staticmethod
    async def _wait_for(pilot, cond, timeout=3.0):
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if cond():
                return True
            await pilot.pause(0.05)
        return cond()

    @staticmethod
    def _preview_text(app):
        from agent_hq.dashboard import PreviewPanel

        preview = app.query_one("#right-panel", PreviewPanel)
        return "\n".join(strip.text for strip in preview.lines)

    def _patches(self, agents):
        from unittest.mock import patch

        return (
            patch("agent_hq.dashboard.is_daemon_running", return_value=True),
            patch("agent_hq.dashboard.get_agents_cached", return_value=(agents, 0.0)),
            patch("agent_hq.dashboard.get_pane_content", return_value="LIVE TAIL"),
        )

    def test_h_renders_history_turns(self):
        records = [
            self._user_record("fix the bug"),
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking."}]}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}
                    ]
                },
            },
        ]
        agent = self._agent_with_transcript(records)

        async def run_test() -> None:
            p1, p2, p3 = self._patches([agent])
            with p1, p2, p3:
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("h")
                    assert await self._wait_for(pilot, lambda: len(app._history_turns) == 3)
                    assert app._history_pane_id == "%1"
                    text = self._preview_text(app)
                    assert "fix the bug" in text
                    assert "Looking." in text
                    assert "Bash(git status)" in text

        asyncio.run(run_test())

    def test_wrap_user_lines_marks_first_line_and_keeps_blanks(self):
        from agent_hq.dashboard import _wrap_user_lines

        lines = _wrap_user_lines("hello world foo bar baz", 12)
        assert lines[0].startswith("> ")
        assert all(len(line) <= 12 for line in lines)
        # Paragraph breaks survive as empty lines so the bar runs through them.
        assert _wrap_user_lines("a\n\nb", 20) == ["> a", "", "b"]

    def test_user_turns_get_full_height_bar_like_web(self):
        """The web view brackets user messages with a left bar spanning the
        whole block; the TUI must match (the ``>`` alone was hard to spot)."""
        # Long enough that the panel scrolls: a vertical scrollbar steals a
        # column, and pre-wrapping to the wrong width lets continuation lines
        # re-wrap out from under the bar.
        long_prompt = " ".join(f"word{i}" for i in range(400))
        records = [
            self._user_record(long_prompt),
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "reply"}]}},
        ]
        agent = self._agent_with_transcript(records)

        async def run_test() -> None:
            p1, p2, p3 = self._patches([agent])
            with p1, p2, p3:
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("h")
                    assert await self._wait_for(pilot, lambda: len(app._history_turns) == 2)
                    await pilot.pause()
                    lines = self._preview_text(app).split("\n")
                    barred = [line for line in lines if line.startswith("▌")]
                    # Wrapped continuation lines carry the bar too, not just
                    # the first — that is what makes the block readable.
                    assert len(barred) >= 2
                    assert "> word0" in barred[0]
                    assert any("word399" in line for line in barred)
                    # No part of the message may escape the bar.
                    escaped = [
                        line for line in lines if "word" in line and not line.startswith("▌")
                    ]
                    assert escaped == []
                    # Barred lines must fit the width RichLog actually renders
                    # into (content width minus the scrollbar), or it re-wraps
                    # them and the overflow lands outside the bar.
                    from agent_hq.dashboard import PreviewPanel

                    preview = app.query_one("#right-panel", PreviewPanel)
                    usable = preview.scrollable_content_region.width
                    assert max(len(line) for line in barred) <= usable
                    reply = next(line for line in lines if "reply" in line)
                    assert not reply.startswith("▌")

        asyncio.run(run_test())

    def test_h_without_transcript_stays_live(self):
        agent = make_agent("%1")  # no claude_transcript_session_id

        async def run_test() -> None:
            p1, p2, p3 = self._patches([agent])
            with p1, p2, p3:
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("h")
                    await pilot.pause()
                    assert app._history_pane_id is None

        asyncio.run(run_test())

    def test_h_again_returns_to_live(self):
        agent = self._agent_with_transcript([self._user_record("hello")])

        async def run_test() -> None:
            p1, p2, p3 = self._patches([agent])
            with p1, p2, p3:
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("h")
                    assert await self._wait_for(pilot, lambda: app._history_pane_id == "%1")
                    await pilot.press("h")
                    assert await self._wait_for(pilot, lambda: app._history_pane_id is None)
                    assert await self._wait_for(
                        pilot, lambda: "LIVE TAIL" in self._preview_text(app)
                    )

        asyncio.run(run_test())

    def test_live_refresh_does_not_clobber_history(self):
        agent = self._agent_with_transcript([self._user_record("history line")])

        async def run_test() -> None:
            from rich.text import Text

            p1, p2, p3 = self._patches([agent])
            with p1, p2, p3:
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("h")
                    assert await self._wait_for(pilot, lambda: len(app._history_turns) == 1)
                    # A straggler live-capture result for the same pane must
                    # be dropped while history mode is active.
                    app._show_preview(Text("LIVE TAIL"), "%1")
                    await pilot.pause()
                    assert "LIVE TAIL" not in self._preview_text(app)
                    assert "history line" in self._preview_text(app)

        asyncio.run(run_test())

    def test_cursor_move_exits_history(self):
        agent1 = self._agent_with_transcript([self._user_record("one")], pane_id="%1")
        agent2 = make_agent("%2", branch="other")

        async def run_test() -> None:
            p1, p2, p3 = self._patches([agent1, agent2])
            with p1, p2, p3:
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    from textual.widgets import DataTable

                    table = app.query_one("#agent-table", DataTable)
                    table.move_cursor(row=app._row_pane_ids.index("%1"))
                    await pilot.pause()
                    await pilot.press("h")
                    assert await self._wait_for(pilot, lambda: app._history_pane_id == "%1")
                    await pilot.press("j")
                    assert await self._wait_for(pilot, lambda: app._history_pane_id is None)

        asyncio.run(run_test())

    def test_h_persists_pref_both_ways(self, tmp_path):
        """#171 follow-up: the h toggle writes tui_preview_mode to the
        shared prefs store — history on entry, live on exit."""
        from agent_hq.ui_prefs import UiPrefsStore

        store = UiPrefsStore(path=tmp_path / "ui-prefs.json")
        agent = self._agent_with_transcript([self._user_record("hello")])

        async def run_test() -> None:
            p1, p2, p3 = self._patches([agent])
            with p1, p2, p3:
                app = AgentDashboard(ui_prefs_store=store)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("h")
                    assert await self._wait_for(
                        pilot, lambda: store.get_tui_preview_mode() == "history"
                    )
                    await pilot.press("h")
                    assert await self._wait_for(
                        pilot, lambda: store.get_tui_preview_mode() == "live"
                    )

        asyncio.run(run_test())

    def test_sticky_history_follows_cursor(self):
        """Once in history mode, moving to another agent with a transcript
        shows THAT agent's history — no h re-press per agent."""
        agent1 = self._agent_with_transcript(
            [self._user_record("first agent line")], pane_id="%1", session_id="hist-1"
        )
        agent2 = self._agent_with_transcript(
            [self._user_record("second agent line")], pane_id="%2", session_id="hist-2"
        )
        agent2.branch = "other"

        async def run_test() -> None:
            p1, p2, p3 = self._patches([agent1, agent2])
            with p1, p2, p3:
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    from textual.widgets import DataTable

                    table = app.query_one("#agent-table", DataTable)
                    table.move_cursor(row=app._row_pane_ids.index("%1"))
                    await pilot.pause()
                    await pilot.press("h")
                    assert await self._wait_for(pilot, lambda: app._history_pane_id == "%1")
                    await pilot.press("j")
                    assert await self._wait_for(pilot, lambda: app._history_pane_id == "%2")
                    assert await self._wait_for(
                        pilot, lambda: "second agent line" in self._preview_text(app)
                    )

        asyncio.run(run_test())

    def test_sticky_falls_back_to_live_then_recovers(self):
        """A transcript-less agent shows the live capture, but the sticky
        mode survives: moving back to a transcript agent re-enters history."""
        agent1 = self._agent_with_transcript([self._user_record("one")], pane_id="%1")
        agent2 = make_agent("%2", branch="other")  # no transcript

        async def run_test() -> None:
            p1, p2, p3 = self._patches([agent1, agent2])
            with p1, p2, p3:
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    from textual.widgets import DataTable

                    table = app.query_one("#agent-table", DataTable)
                    table.move_cursor(row=app._row_pane_ids.index("%1"))
                    await pilot.pause()
                    await pilot.press("h")
                    assert await self._wait_for(pilot, lambda: app._history_pane_id == "%1")
                    await pilot.press("j")
                    assert await self._wait_for(pilot, lambda: app._history_pane_id is None)
                    assert await self._wait_for(
                        pilot, lambda: "LIVE TAIL" in self._preview_text(app)
                    )
                    await pilot.press("k")
                    assert await self._wait_for(pilot, lambda: app._history_pane_id == "%1")

        asyncio.run(run_test())

    def test_history_pref_restored_on_launch(self, tmp_path):
        """tui_preview_mode=history in the prefs store puts the preview in
        history mode from the first highlighted row — no h press."""
        from agent_hq.ui_prefs import UiPrefsStore

        store = UiPrefsStore(path=tmp_path / "ui-prefs.json")
        store.set_tui_preview_mode("history")
        agent = self._agent_with_transcript([self._user_record("restored line")])

        async def run_test() -> None:
            p1, p2, p3 = self._patches([agent])
            with p1, p2, p3:
                app = AgentDashboard(ui_prefs_store=store)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    assert await self._wait_for(pilot, lambda: app._history_pane_id == "%1")
                    assert await self._wait_for(
                        pilot, lambda: "restored line" in self._preview_text(app)
                    )

        asyncio.run(run_test())

    def test_scroll_to_top_loads_earlier_page(self):
        records = [self._user_record(f"msg {i:03d}") for i in range(100)]
        agent = self._agent_with_transcript(records)

        async def run_test() -> None:
            p1, p2, p3 = self._patches([agent])
            with p1, p2, p3:
                app = AgentDashboard()
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("h")
                    assert await self._wait_for(pilot, lambda: len(app._history_turns) == 40)
                    assert app._history_has_more is True
                    from agent_hq.dashboard import PreviewPanel

                    preview = app.query_one("#right-panel", PreviewPanel)
                    preview.scroll_to(y=0, animate=False)
                    await pilot.pause()
                    app.action_preview_scroll_up()
                    assert await self._wait_for(pilot, lambda: len(app._history_turns) == 80)
                    text = self._preview_text(app)
                    assert "msg 020" in text
                    assert "msg 099" in text

        asyncio.run(run_test())


@pytest.mark.parametrize("configured", [False, True])
def test_dispatch_triage_store_wiring(tmp_path, configured):
    from agent_hq.config import Config, TriageConfig
    from agent_hq.triage_store import TriageStore

    cfg = Config(repos={}, projects=[], worktree_root=tmp_path,
                 triage=TriageConfig(data_dir=tmp_path) if configured else None)
    async def exercise():
        with patch("agent_hq.config.load_config", return_value=cfg), patch(
            "agent_hq.dashboard.is_daemon_running", return_value=True
        ), patch("agent_hq.dashboard.get_agents_cached", return_value=([], 0.0)):
            app = AgentDashboard()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                store = app._dispatch_screen._triage_store
                if configured:
                    assert isinstance(store, TriageStore) and store.data_dir == tmp_path
                else:
                    assert store is None
    asyncio.run(exercise())
