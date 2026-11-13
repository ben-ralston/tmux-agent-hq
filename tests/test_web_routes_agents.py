"""Tests for the /agents page + /agents/list partial."""

from __future__ import annotations

import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from agent_hq.cache import SCHEMA_VERSION
from agent_hq.models import (
    Agent,
    AgentState,
    AgentType,
    DevPhase,
    InputsObserved,
    PhaseSnapshot,
)
from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value


def _mk_agent(
    pane_id: str,
    state: AgentState = AgentState.IDLE,
    branch: str = "main",
    phase: DevPhase | None = None,
    session_slug: str | None = None,
    claude_session_id: str | None = None,
) -> Agent:
    """Build an Agent for tests. ``phase`` (if given) is wrapped in a
    minimal ``PhaseSnapshot`` so the ``Agent.phase`` property returns it."""
    snapshot = (
        PhaseSnapshot(
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
        if phase is not None
        else None
    )
    return Agent(
        pane_id=pane_id,
        session_name="ses",
        window_index=0,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd="/tmp",
        repo_name="repo",
        branch=branch,
        session_slug=session_slug,
        state=state,
        phase_snapshot=snapshot,
        claude_session_id=claude_session_id,
    )


def _write_cache(cache_path, agents):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "producer": "daemon",
                "producer_epoch": time.time(),
                "sequence": 1,
                "timestamp": time.time(),
                "monotonic_ns": time.monotonic_ns(),
                "pid": 1,
                "agents": [a.to_dict() for a in agents],
            }
        )
    )


def _write_native_cache(cache_path, monkeypatch, agents):
    from agent_hq.status_types import LiveStatus

    runtime = cache_path.with_name("runtime.json")
    monkeypatch.setattr("agent_hq.status_collector.STATUS_FILE", runtime)
    monkeypatch.setattr("agent_hq.status_collector._alive", lambda pid: True)
    now, mono = time.time(), time.monotonic_ns()
    observations = []
    for index, agent in enumerate(agents, start=1):
        agent.agent_pid = index
        agent.claude_transcript_session_id = f"native-{index}"
        status = agent.status_snapshot or LiveStatus(
            agent.state.value,
            agent.state.value,
            agent.state.value.title(),
            "",
            "claude",
            agent.claude_transcript_session_id,
            index,
            now,
            mono,
        )
        observations.append({"status": status.to_dict()})
    runtime.write_text(
        json.dumps(
            {
                "version": 1,
                "heartbeat_at": now,
                "heartbeat_ns": mono,
                "observations": observations,
                "source_errors": {},
            }
        )
    )
    _write_cache(cache_path, agents)


@pytest.fixture
def authed_client(tmp_path, monkeypatch):
    """Authenticated test client with a fixture cache.json."""
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_path)
    overrides_path = tmp_path / "overrides.json"
    monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_path)
    # Inactive state lives at ~/.cache/agent-hq/inactive.json — point it
    # at tmp_path to keep the test hermetic.
    inactive_path = tmp_path / "inactive.json"
    monkeypatch.setattr("agent_hq.state.INACTIVE_FILE", inactive_path)

    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(token_file=tm.token_file)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client, cache_path, inactive_path


class TestAgentsPage:
    def test_renders_page_shell(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents")
        assert r.status_code == 200
        # Page contains the shell + a target for the list partial
        assert "Agent HQ" in r.text or "agent-hq" in r.text.lower()
        # Should reference the partial-load target so htmx fills it in
        assert "/agents/list" in r.text or "agents-view" in r.text

    def test_default_view_loads_phase_partial(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents")
        # No ``view`` param → phase partial (the default, #127).
        assert 'hx-get="/agents/phase"' in r.text
        assert 'hx-get="/agents/list"' not in r.text
        assert 'hx-get="/agents/kanban"' not in r.text

    def test_kanban_view_loads_kanban_partial(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents?view=kanban")
        # ``view=kanban`` → kanban partial as the swap target.
        assert 'hx-get="/agents/kanban"' in r.text
        assert 'hx-get="/agents/list"' not in r.text

    def test_unknown_view_falls_back_to_phase(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents?view=garbage")
        # Unknown values fall back to the default phase view (#127).
        assert 'hx-get="/agents/phase"' in r.text
        assert 'hx-get="/agents/list"' not in r.text

    def test_phase_view_loads_phase_partial(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents?view=phase")
        # ``view=phase`` → phase-grouped partial.
        assert 'hx-get="/agents/phase"' in r.text
        assert 'hx-get="/agents/list"' not in r.text
        assert 'hx-get="/agents/kanban"' not in r.text

    def test_toolbar_has_list_and_phase_but_hides_kanban(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents")
        # List + Phase toggle links remain; Kanban button is hidden (#127).
        assert 'href="/agents?view=list"' in r.text
        assert 'href="/agents?view=phase"' in r.text
        assert 'href="/agents?view=kanban"' not in r.text
        # Phase button comes before List in the toolbar.
        assert r.text.index('href="/agents?view=phase"') < r.text.index('href="/agents?view=list"')

    def test_unauthenticated_redirects_to_login(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_path)
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file)
        client = TestClient(app)
        r = client.get("/agents", follow_redirects=False)
        assert r.status_code in (302, 303)
        assert "/login" in r.headers["location"]


@pytest.fixture
def prefs_client(tmp_path, monkeypatch):
    """Authenticated client with an injected UiPrefsStore (#176)."""
    from agent_hq.ui_prefs import UiPrefsStore

    monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", tmp_path / "cache.json")
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    store = UiPrefsStore(path=tmp_path / "ui-prefs.json")
    app = create_app(token_file=tm.token_file, ui_prefs_store=store)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client, store


class TestAgentsViewPersistence:
    """The chosen view survives restarts via the shared UiPrefsStore (#176):
    explicit toolbar clicks persist it; bare GETs restore it."""

    def test_explicit_view_is_persisted(self, prefs_client):
        client, store = prefs_client
        r = client.get("/agents?view=list")
        assert r.status_code == 200
        assert store.get_web_agents_view() == "list"

    def test_bare_get_renders_stored_view(self, prefs_client):
        client, store = prefs_client
        store.set_web_agents_view("list")
        r = client.get("/agents")
        assert 'hx-get="/agents/list"' in r.text
        assert 'hx-get="/agents/phase"' not in r.text

    def test_bare_get_with_empty_store_defaults_to_phase(self, prefs_client):
        client, store = prefs_client
        r = client.get("/agents")
        assert 'hx-get="/agents/phase"' in r.text

    def test_invalid_view_neither_persists_nor_overrides(self, prefs_client):
        client, store = prefs_client
        store.set_web_agents_view("list")
        r = client.get("/agents?view=garbage")
        # Falls back to the stored preference, not the hardcoded default…
        assert 'hx-get="/agents/list"' in r.text
        # …and the garbage value was not written to the store.
        assert store.get_web_agents_view() == "list"


class TestAgentsList:
    """The htmx swap target: returns just the cards, no shell."""

    def test_renders_card_per_agent(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [
                _mk_agent("%5", session_slug="my-session-a", branch="main"),
                _mk_agent("%6", session_slug="my-session-b", branch="dev"),
            ],
        )
        r = client.get("/agents/list")
        assert r.status_code == 200
        # Slug appears as the data-card-slug attribute (with the % stripped)
        assert 'data-card-slug="5"' in r.text
        assert 'data-card-slug="6"' in r.text
        assert "my-session-a" in r.text
        assert "my-session-b" in r.text

    def test_card_carries_stable_key(self, authed_client):
        """Each card must carry ``data-card-key`` (the pane-renumber-stable
        identity) so the SSE listener can find and update an agent's card
        in place after a tmux pane renumber (#132)."""
        client, cache_path, _ = authed_client
        agent = _mk_agent("%5", session_slug="my-session-a")
        agent.claude_transcript_session_id = "trans-abc"
        _write_cache(cache_path, [agent])
        r = client.get("/agents/list")
        assert r.status_code == 200
        assert 'data-card-key="trans-abc"' in r.text

    def test_pinned_waiting_to_top(self, authed_client, monkeypatch):
        """Waiting agents must render before idle ones in the list."""
        client, cache_path, _ = authed_client
        _write_native_cache(
            cache_path,
            monkeypatch,
            [
                _mk_agent("%1", AgentState.IDLE, session_slug="idle-one"),
                _mk_agent("%2", AgentState.WAITING, session_slug="waiting-one"),
                _mk_agent("%3", AgentState.RUNNING, session_slug="running-one"),
            ],
        )
        r = client.get("/agents/list")
        # Waiting agent's slug appears before the idle agent's slug
        idx_wait = r.text.find("waiting-one")
        idx_idle = r.text.find("idle-one")
        assert idx_wait != -1 and idx_idle != -1
        assert idx_wait < idx_idle

    def test_muted_at_bottom_under_separator(self, authed_client):
        client, cache_path, inactive_path = authed_client
        _write_cache(
            cache_path,
            [
                _mk_agent("%1", session_slug="active-one"),
                _mk_agent("%9", session_slug="muted-one"),
            ],
        )
        # Mark %9 as muted via the inactive-state file
        inactive_path.write_text(
            json.dumps({"%9": {"marked_at": time.time(), "fingerprint": "abc"}})
        )

        r = client.get("/agents/list")
        idx_active = r.text.find("active-one")
        idx_separator = r.text.lower().find("muted")
        idx_muted = r.text.find("muted-one")
        # A "muted" separator appears between the active and muted cards
        assert idx_active != -1 and idx_muted != -1
        assert idx_active < idx_muted
        assert idx_separator != -1
        assert idx_active < idx_separator < idx_muted

    def test_phase_chip_class_uses_phase_value(self, authed_client):
        """Cards must render the phase color class so the CSS lights up the
        chip — regression guard for stripping the phase out of the template."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%1", phase=DevPhase.IMPLEMENT)])
        r = client.get("/agents/list")
        assert "phase-chip--implement" in r.text

    def test_phase_chip_on_separate_row_from_branch(self, authed_client):
        """Phase chip lives in its own ``card__row`` below the branch name so
        long branch names aren't truncated by the chip on narrow viewports
        (issue #109)."""
        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [
                _mk_agent(
                    "%1",
                    branch="feature/very-long-branch-name-that-would-truncate",
                    phase=DevPhase.IMPLEMENT,
                )
            ],
        )
        r = client.get("/agents/list")
        title_idx = r.text.find("card__title")
        chip_idx = r.text.find("phase-chip--implement")
        assert title_idx != -1 and chip_idx != -1
        # The card__row containing the title must close BEFORE the phase
        # chip appears — i.e. they live on different rows.
        row_close_idx = r.text.find("</div>", title_idx, chip_idx)
        assert row_close_idx != -1, (
            "Phase chip should be in a separate row from the branch name "
            "(no closing </div> found between them)"
        )

    def test_none_phase_renders_adhoc_chip_not_blank(self, authed_client):
        """Agents whose phase couldn't be detected (``phase=None``) render
        as an Ad-hoc chip — a blank chip slot looks broken in the list
        view even though kanban/phase views already bucket None-phase
        agents under Ad-hoc."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%1", phase=None)])
        r = client.get("/agents/list")
        assert "phase-chip--adhoc" in r.text
        assert "Ad-hoc" in r.text

    def test_renders_state_dot_class(self, authed_client, monkeypatch):
        client, cache_path, _ = authed_client
        _write_native_cache(
            cache_path,
            monkeypatch,
            [
                _mk_agent("%1", state=AgentState.RUNNING),
                _mk_agent("%2", state=AgentState.WAITING),
            ],
        )
        r = client.get("/agents/list")
        assert "state-dot--running" in r.text
        assert "state-dot--waiting" in r.text

    def test_empty_state_when_no_agents(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [])
        r = client.get("/agents/list")
        assert r.status_code == 200
        # Some empty-state copy that mentions discovery / agents
        assert (
            "no agent" in r.text.lower()
            or "discovering" in r.text.lower()
            or "empty" in r.text.lower()
        )


class TestAgentsKanban:
    """Kanban swap target: groups agents by phase column.

    Only POPULATED phases get a column — empty phases are omitted so
    the populated columns get readable width. ``None``-phase agents
    land in the ad-hoc column (matching the TUI's behavior).
    """

    def test_renders_column_per_populated_phase(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [
                _mk_agent("%1", phase=DevPhase.IMPLEMENT, session_slug="impl-one"),
                _mk_agent("%2", phase=DevPhase.QA, session_slug="qa-one"),
            ],
        )
        r = client.get("/agents/kanban")
        assert r.status_code == 200
        # Only the populated columns appear — order matches DevPhase
        # left-to-right (adhoc → done).
        assert "Implement" in r.text
        assert "QA" in r.text
        # Empty phases are omitted entirely.
        for empty_label in ("Design", "Review", "Ready", "Done", "Ad-hoc"):
            assert empty_label not in r.text

    def test_card_lands_in_correct_column(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [
                _mk_agent("%5", phase=DevPhase.IMPLEMENT, session_slug="impl-one"),
                _mk_agent("%6", phase=DevPhase.QA, session_slug="qa-one"),
            ],
        )
        r = client.get("/agents/kanban")
        # Look for "Implement" column followed by impl-one then "QA" column
        # then qa-one. This guards against rendering all cards into one column.
        impl_idx = r.text.find("Implement")
        impl_card = r.text.find("impl-one")
        qa_idx = r.text.find("QA")
        qa_card = r.text.find("qa-one")
        assert impl_idx != -1 < impl_card
        assert qa_idx != -1 < qa_card
        assert impl_idx < impl_card < qa_idx < qa_card

    def test_none_phase_agents_go_to_adhoc_column(self, authed_client):
        """An agent with no detected phase should appear under "Ad-hoc"."""
        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [_mk_agent("%9", phase=None, session_slug="no-phase-agent")],
        )
        r = client.get("/agents/kanban")
        adhoc_idx = r.text.find("Ad-hoc")
        card_idx = r.text.find("no-phase-agent")
        assert adhoc_idx != -1 and card_idx != -1
        assert adhoc_idx < card_idx

    def test_empty_columns_are_omitted(self, authed_client):
        """Zero-count phases are dropped entirely — populated columns share
        the row width on desktop, and the mobile horizontal scroll only
        cycles through phases that actually have agents. Regression guard
        against the prior ``kanban-column--empty`` thin-strip rendering,
        which the user found visually noisy."""
        client, cache_path, _ = authed_client
        # Only one agent in implement; the other 7 phases must vanish.
        _write_cache(cache_path, [_mk_agent("%1", phase=DevPhase.IMPLEMENT)])
        r = client.get("/agents/kanban")
        # Exactly one column rendered, and it's the implement column.
        assert r.text.count('data-phase="') == 1
        assert 'data-phase="implement"' in r.text
        # No legacy empty-column markup left behind.
        assert "kanban-column--empty" not in r.text
        assert 'data-empty="1"' not in r.text

    def test_card_targets_overlay_slot_not_detail_slot(self, authed_client):
        """Clicking a kanban card opens a floating overlay (not the side
        pane). The card's hx-target / hx-get must reflect that wiring —
        regression guard for accidentally reverting to ``#detail-slot``."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5", phase=DevPhase.IMPLEMENT)])
        r = client.get("/agents/kanban")
        # Kanban card should target the overlay slot.
        assert 'hx-target="#overlay-slot"' in r.text
        assert 'hx-get="/agents/5/overlay"' in r.text

    def test_card_uses_kanban_variant(self, authed_client):
        """Kanban cards should use the dense ``card--kanban`` class so the
        CSS picks the right variant."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%1", phase=DevPhase.IMPLEMENT)])
        r = client.get("/agents/kanban")
        assert "card--kanban" in r.text


class TestAgentsPhase:
    """``GET /agents/phase`` — phase-grouped list (the ``V`` mode mirror).

    Unlike kanban, only POPULATED phases appear (no zero-count groups).
    Cards inside each group are the comfortable list cards, not the
    dense kanban variant — clicking them targets ``#detail-slot``.
    """

    def test_renders_section_per_populated_phase(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [
                _mk_agent("%1", phase=DevPhase.IMPLEMENT, session_slug="impl-one"),
                _mk_agent("%2", phase=DevPhase.QA, session_slug="qa-one"),
            ],
        )
        r = client.get("/agents/phase")
        assert r.status_code == 200
        # Both populated phases have a section with chip + count.
        assert 'data-phase="implement"' in r.text
        assert 'data-phase="qa"' in r.text
        assert "impl-one" in r.text
        assert "qa-one" in r.text

    def test_omits_empty_phases(self, authed_client):
        """Phase view skips empty groups entirely (in contrast to kanban
        which shows them as collapsed strips)."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%1", phase=DevPhase.IMPLEMENT)])
        r = client.get("/agents/phase")
        # Implement appears; the other 6 phases do NOT appear as section
        # data-phase markers.
        assert 'data-phase="implement"' in r.text
        for empty_phase in ("design", "qa", "review", "ready", "done", "adhoc"):
            assert f'data-phase="{empty_phase}"' not in r.text

    def test_none_phase_falls_into_adhoc_section(self, authed_client):
        """Agents with no detected phase appear under the Ad-hoc group —
        consistent with kanban + TUI."""
        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [_mk_agent("%9", phase=None, session_slug="no-phase-agent")],
        )
        r = client.get("/agents/phase")
        assert 'data-phase="adhoc"' in r.text
        assert "no-phase-agent" in r.text

    def test_uses_comfortable_card_variant(self, authed_client):
        """Phase view reuses the comfortable card so click handlers /
        layout match the flat list view exactly."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%1", phase=DevPhase.IMPLEMENT)])
        r = client.get("/agents/phase")
        assert "card--comfortable" in r.text
        # Cards still target the side pane, not the kanban overlay.
        assert 'hx-target="#detail-slot"' in r.text

    def test_section_header_shows_phase_chip_and_count(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [
                _mk_agent("%1", phase=DevPhase.IMPLEMENT, session_slug="a"),
                _mk_agent("%2", phase=DevPhase.IMPLEMENT, session_slug="b"),
            ],
        )
        r = client.get("/agents/phase")
        # Chip class for the section header
        assert "phase-chip--implement" in r.text
        # Count of 2 in the section header
        assert "phase-group__count" in r.text
        assert ">2<" in r.text  # count badge content

    def test_empty_renders_placeholder(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [])
        r = client.get("/agents/phase")
        assert r.status_code == 200
        assert "No agents discovered yet" in r.text


class TestAgentDetail:
    """``GET /agents/{slug}`` returns the detail content. Both the
    bottom-sheet (mobile) and right-pane (desktop) markup are rendered;
    CSS hides whichever doesn't apply for the viewport.
    """

    def test_renders_both_sheet_and_pane(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [_mk_agent("%5", session_slug="my-session-detail")],
        )
        r = client.get("/agents/5")
        assert r.status_code == 200
        assert "detail-sheet" in r.text
        assert "detail-pane" in r.text
        assert "my-session-detail" in r.text

    def test_404_on_unknown_slug(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/999")
        assert r.status_code == 404

    def test_renders_action_buttons(self, authed_client):
        """Detail must surface the four agent actions: jump, mute, rename, kill.

        We check for hx-post URLs because the buttons themselves may be
        rendered as links or buttons.
        """
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/5")
        for path in (
            "/agents/5/jump",
            "/agents/5/mute",
            "/agents/5/rename",
            "/agents/5/kill",
        ):
            assert path in r.text

    def test_phone_jump_uses_claude_session_id(self, authed_client):
        """When ``claude_session_id`` is present, the mobile jump button
        deep-links to ``https://claude.ai/code/{id}``."""
        client, cache_path, _ = authed_client
        a = _mk_agent("%5")
        a.claude_session_id = "session_01Tk4ztYgtWAhHHvE7uh5ghh"
        _write_cache(cache_path, [a])
        r = client.get("/agents/5")
        assert "https://claude.ai/code/session_01Tk4ztYgtWAhHHvE7uh5ghh" in r.text

    def test_phone_jump_hidden_when_session_id_missing(self, authed_client):
        """Older Claude Code versions don't write bridgeSessionId, so there's
        no native URI to jump to (#205 Task 18) — the button is hidden
        entirely rather than falling back to a bare, non-deep-linking
        ``https://claude.ai/code``."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])  # claude_session_id is None
        r = client.get("/agents/5")
        assert "claude.ai" not in r.text

    def test_phase_chip_on_separate_row_from_branch(self, authed_client):
        """In the detail body the phase chip + ⋯ menu live in their own
        ``card__row`` below the branch name so long branches aren't
        truncated by the chip on narrow viewports (issue #109).

        The detail response renders BOTH the sheet (mobile) and the pane
        (desktop) markup, so we assert the invariant once per pair."""
        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [
                _mk_agent(
                    "%5",
                    branch="feature/very-long-branch-name-that-would-truncate",
                    phase=DevPhase.IMPLEMENT,
                )
            ],
        )
        r = client.get("/agents/5")
        text = r.text
        pairs = 0
        cursor = 0
        while True:
            title_idx = text.find("card__title", cursor)
            if title_idx == -1:
                break
            chip_idx = text.find("phase-chip--implement", title_idx)
            assert chip_idx != -1, "expected a phase-chip after each title"
            row_close_idx = text.find("</div>", title_idx, chip_idx)
            assert row_close_idx != -1, (
                "Phase chip should be in a separate card__row from the "
                "branch name (no closing </div> found between them)"
            )
            cursor = chip_idx
            pairs += 1
        assert pairs >= 2, "expected detail to render both sheet and pane (>=2 title/chip pairs)"

    def test_detail_title_uses_wrap_modifier(self, authed_client):
        """The detail-panel title gets the ``card__title--wrap`` modifier so
        long branch names wrap (preferring hyphen breaks) across multiple
        lines instead of being truncated by ellipsis. The list-view title
        stays compact and single-line — only the detail panel wraps."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5", branch="main")])
        r = client.get("/agents/5")
        assert "card__title--wrap" in r.text

    def test_poll_lives_on_inner_live_element_not_host(self, authed_client):
        """The every-3s polling lives on the inner ``.preview-live``
        element, NOT on the ``.pane-preview-host`` scroll container
        (#171: the host is now mode-neutral so the Live|History toggle
        can swap its content, which also kills the poll timer when
        History is active). The protected invariant is unchanged from
        the original #144-era fix: the scroll container itself is never
        replaced by the poll swap, so scroll position survives ticks."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/5")
        assert "pane-preview-host" in r.text
        # The host opening tag itself must NOT carry the poll trigger.
        host_start = r.text.index("pane-preview-host")
        host_tag = r.text[r.text.rindex("<", 0, host_start) : r.text.index(">", host_start)]
        assert "hx-trigger" not in host_tag
        # The inner live element carries it, swapping its own innerHTML.
        assert "preview-live" in r.text
        live_start = r.text.index('class="preview-live"')
        live_tag = r.text[r.text.rindex("<", 0, live_start) : r.text.index(">", live_start)]
        assert 'hx-trigger="load, every 3s"' in live_tag
        assert 'hx-swap="innerHTML"' in live_tag


class TestAgentCardSwap:
    """``GET /agents/{slug}/card`` — single-card swap target for SSE.

    The front-end SSE listener fires htmx.ajax against this URL whenever
    the watcher publishes ``agent.invalidated``. Returns ONLY the card
    (not a wrapper), so the swap target's outerHTML cleanly replaces the
    old card.
    """

    def test_renders_single_card(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5", session_slug="card-swap-test")])
        r = client.get("/agents/5/card")
        assert r.status_code == 200
        # Just one card, no wrapper / no muted separator
        assert r.text.count('data-card-slug="5"') == 1
        assert "muted-separator" not in r.text
        assert "card-swap-test" in r.text

    def test_returns_404_on_unknown_slug(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/999/card")
        assert r.status_code == 404

    def test_card_keeps_swap_attribute(self, authed_client):
        """The returned card must carry ``data-card-slug`` so the front-end
        SSE listener can find it on the next invalidation."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/5/card")
        assert 'data-card-slug="5"' in r.text

    def test_kanban_variant_returns_kanban_card(self, authed_client):
        """``?variant=kanban`` must return the kanban card markup (which
        targets ``#overlay-slot``), not the comfortable list card. Without
        this, an SSE invalidation in kanban view would swap a kanban card
        with a comfortable card pointed at the wrong slot — clicking the
        card would open the (hidden) side pane instead of the overlay."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/5/card?variant=kanban")
        assert r.status_code == 200
        # Kanban card class + overlay swap target.
        assert "card--kanban" in r.text
        assert "#overlay-slot" in r.text
        # Comfortable card's #detail-slot target must NOT appear.
        assert "#detail-slot" not in r.text

    def test_default_variant_is_comfortable(self, authed_client):
        """No ``variant`` param → comfortable (back-compat with the
        existing list/phase swap target)."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/5/card")
        assert "card--comfortable" in r.text
        assert "#detail-slot" in r.text


class TestAgentOverlay:
    """``GET /agents/{slug}/overlay`` — floating overlay panel target
    used by the kanban view. Same body as the side pane (sticky top +
    scrollable preview) plus a close ``×`` button. Dismiss is JS-only
    (no server-side close endpoint) — the front-end empties the slot
    on outside-click / Escape / × click."""

    def test_renders_overlay_wrapper_with_close_button(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5", session_slug="overlay-test")])
        r = client.get("/agents/5/overlay")
        assert r.status_code == 200
        assert "detail-overlay" in r.text
        # Close button has the JS hook attribute.
        assert "data-overlay-close" in r.text
        # Body content is shared with the side pane.
        assert "overlay-test" in r.text

    def test_overlay_uses_unique_phase_menu_slot_id(self, authed_client):
        """The phase menu slot ID must include the overlay suffix so it
        doesn't collide with a side-pane slot when both DOM elements
        coexist (kanban view click → overlay opens; user navigates to
        list view but the slot is still in the DOM)."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/5/overlay")
        assert "phase-menu-slot-overlay-5" in r.text

    def test_overlay_includes_action_buttons(self, authed_client):
        """All four action buttons (jump web/term, mute, rename, kill)
        must appear so the kanban view isn't read-only."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/5/overlay")
        for path in (
            "/agents/5/jump",
            "/agents/5/mute",
            "/agents/5/rename",
            "/agents/5/kill",
        ):
            assert path in r.text

    def test_overlay_includes_preview_host(self, authed_client):
        """Overlay must wire up the same auto-refreshing preview host
        (so the panel feels live, not frozen)."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/5/overlay")
        assert "pane-preview-host" in r.text
        assert 'hx-trigger="load, every 3s"' in r.text

    def test_returns_404_on_unknown_slug(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/999/overlay")
        assert r.status_code == 404


class TestPanePreview:
    """``GET /agents/{slug}/preview`` — pane preview content (just the
    ``<pre>`` body). The auto-refresh trigger lives on the parent
    ``.pane-preview-host`` inside the detail body so the host's scroll
    position survives the every-3s tick — see ``TestAgentDetail`` for
    coverage of that wiring.
    """

    def test_renders_ansi_html(self, authed_client, monkeypatch):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        # Stub tmux capture with raw text containing an ANSI escape so we
        # can verify ansi2html ran.
        ansi_text = "\x1b[32mhello\x1b[0m world"
        from agent_hq.web.routes import agents as agents_routes_mod

        monkeypatch.setattr(agents_routes_mod, "capture_pane", lambda *a, **kw: ansi_text)
        r = client.get("/agents/5/preview")
        assert r.status_code == 200
        # ansi2html should have converted the green escape to a span
        assert "hello" in r.text
        assert "world" in r.text
        # ANSI green is class 'ansi32' under inline=False / scheme map.
        # We just need to assert that *some* span/style emerged.
        assert "<span" in r.text.lower() or "style=" in r.text.lower()

    def test_returns_pre_only(self, authed_client, monkeypatch):
        """Preview returns just the ``<pre>`` body, no wrapper. The
        wrapping host with ``hx-trigger="every 3s"`` is rendered once
        in the detail body and repeatedly swaps ``innerHTML`` from this
        endpoint, so the host's scroll position survives ticks."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        from agent_hq.web.routes import agents as agents_routes_mod

        monkeypatch.setattr(agents_routes_mod, "capture_pane", lambda *a, **kw: "x")
        r = client.get("/agents/5/preview")
        text = r.text.strip()
        assert text.startswith("<pre")
        # Trigger MUST NOT live on the body — that would re-introduce the
        # outerHTML / scroll-reset bug we just fixed.
        assert 'hx-trigger="every 3s"' not in text

    def test_returns_404_on_unknown_slug(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/999/preview")
        assert r.status_code == 404

    def test_strips_osc_hyperlink_to_prevent_xss(self, authed_client, monkeypatch):
        """OSC-8 hyperlink escape sequences in pane output must be
        stripped before ANSI→HTML conversion. Without this, ansi2html
        would convert ``\\x1b]8;;javascript:alert(1)\\x1b\\\\evil``
        into a live ``<a href="javascript:…">`` tag, which the
        ``|safe`` filter on the preview template would render as
        executable HTML — a tmux pane writing a malicious OSC-8
        sequence (a misbehaving agent, a piped log) could land an
        XSS payload in the user's browser."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        from agent_hq.web.routes import agents as agents_routes_mod

        # ESC ] 8 ; ; URL ESC \ TEXT ESC ] 8 ; ; ESC \
        # Both common terminators (BEL and ST) should be stripped.
        payload_st = "\x1b]8;;javascript:alert(1)\x1b\\evil-link\x1b]8;;\x1b\\"
        payload_bel = "\x1b]8;;javascript:alert(2)\x07also-evil\x1b]8;;\x07"
        # Plus a window-title OSC (commonly emitted by shells) — also
        # has no place in a preview pane.
        title_osc = "\x1b]0;some title\x07"
        ansi_text = payload_st + payload_bel + title_osc + "tail"
        monkeypatch.setattr(agents_routes_mod, "capture_pane", lambda *a, **kw: ansi_text)

        r = client.get("/agents/5/preview")
        assert r.status_code == 200
        # Crucial: no anchor tag escaped through.
        body = r.text.lower()
        assert "<a " not in body
        assert "javascript:" not in body
        assert "href=" not in body
        # Visible link text remains (we strip the OSC sequence, not the
        # text inside).
        assert "evil-link" in r.text
        assert "also-evil" in r.text
        assert "tail" in r.text

    def test_strips_osc_rebuilt_by_nested_sequences(self):
        """A one-pass strip can splice the bytes around a removed inner OSC
        into a fresh ``ESC ] 8 ;;`` introducer (#263 F1): the leading lone
        ESC joins the ``]8;;…`` that followed the inner sequence. The strip
        must run until nothing is left to remove."""
        from agent_hq.web.routes.agents import ansi_to_html

        inner = "\x1b]0;x\x07"
        payload = (
            "\x1b" + inner + ']8;;javascript:alert(1)" onmouseover="alert(2)\x07click'
            "\x1b" + inner + "]8;;\x07tail"
        )
        html = ansi_to_html(payload).lower()
        # The spliced payload may survive as inert text; what must never
        # appear is markup built from it.
        assert "<a" not in html
        assert "href" not in html
        assert "tail" in html

    def test_strips_unterminated_osc_sequence(self, authed_client, monkeypatch):
        """Truncated capture or partial OSC writes can leave a sequence
        without its BEL/ST terminator. The regex must still strip those
        — otherwise an attacker can land an OSC-8 payload by simply
        omitting the terminator (e.g. dump a giant OSC and let the tmux
        capture window cut it off mid-sequence)."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        from agent_hq.web.routes import agents as agents_routes_mod

        # OSC-8 hyperlink with no closing ST or BEL. Everything after
        # the ESC ] runs to end-of-input.
        ansi_text = "\x1b]8;;javascript:alert(1)\x1b\\link-text\x1b]0;truncated"
        monkeypatch.setattr(agents_routes_mod, "capture_pane", lambda *a, **kw: ansi_text)

        r = client.get("/agents/5/preview")
        assert r.status_code == 200
        body = r.text.lower()
        # The "truncated" tail (window-title OSC payload) gets stripped
        # along with the unterminated OSC. No anchors leak.
        assert "<a " not in body
        assert "javascript:" not in body
        assert "truncated" not in body
        # The properly-terminated middle hyperlink's visible text
        # survives — we only strip OSC sequences, not surrounding text.
        assert "link-text" in r.text


class TestActionRoutes:
    """Mute / Rename / Kill POSTs."""

    def test_mute_sets_explicit_state_true(self, authed_client, monkeypatch):
        client, cache_path, inactive_path = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        # Stub fingerprint computation so we don't shell out to tmux.
        from agent_hq.web.routes import agents as agents_routes_mod

        monkeypatch.setattr(
            agents_routes_mod,
            "compute_content_fingerprint",
            lambda pane_id: "fp",
        )
        r = client.post("/agents/5/mute", data={"state": "true"})
        assert r.status_code == 200
        # Inactive file now contains %5
        import json as _json

        records = _json.loads(inactive_path.read_text())
        assert "%5" in records

    def test_mute_is_idempotent(self, authed_client, monkeypatch):
        """``state=true`` twice keeps the agent muted, doesn't flip back."""
        client, cache_path, inactive_path = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        from agent_hq.web.routes import agents as agents_routes_mod

        monkeypatch.setattr(
            agents_routes_mod,
            "compute_content_fingerprint",
            lambda pane_id: "fp",
        )
        client.post("/agents/5/mute", data={"state": "true"})
        client.post("/agents/5/mute", data={"state": "true"})
        import json as _json

        records = _json.loads(inactive_path.read_text())
        assert "%5" in records

    def test_mute_state_false_unmutes(self, authed_client, monkeypatch):
        client, cache_path, inactive_path = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        # Pre-mute %5
        import json as _json
        import time as _time

        inactive_path.write_text(
            _json.dumps({"%5": {"marked_at": _time.time(), "fingerprint": "x"}})
        )
        from agent_hq.web.routes import agents as agents_routes_mod

        monkeypatch.setattr(
            agents_routes_mod,
            "compute_content_fingerprint",
            lambda pane_id: "fp",
        )
        r = client.post("/agents/5/mute", data={"state": "false"})
        assert r.status_code == 200
        records = _json.loads(inactive_path.read_text())
        assert "%5" not in records

    def test_rename_calls_safe_rename(self, authed_client, monkeypatch):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        called = []

        def fake_rename(pane_id, new_name=None):
            called.append((pane_id, new_name))
            return True

        monkeypatch.setattr("agent_hq.web.errors.rename_session", fake_rename)
        r = client.post("/agents/5/rename", data={"name": "shiny-name"})
        assert r.status_code == 200
        assert called == [("%5", "shiny-name")]
        # Toast confirmation
        assert "renamed" in r.text.lower() or "rename" in r.text.lower()

    def test_rename_handles_action_error(self, authed_client, monkeypatch):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        # Make rename_session return False so safe_rename raises
        monkeypatch.setattr(
            "agent_hq.web.errors.rename_session",
            lambda pane_id, new_name=None: False,
        )
        r = client.post("/agents/5/rename", data={"name": "x"})
        assert r.status_code == 200
        # Error toast — body should include some failure copy
        assert (
            "could not" in r.text.lower() or "fail" in r.text.lower() or "error" in r.text.lower()
        )

    def test_kill_calls_safe_kill(self, authed_client, monkeypatch):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        called = []
        monkeypatch.setattr(
            "agent_hq.web.errors.kill_agent",
            lambda pane_id: called.append(pane_id) or True,
        )
        r = client.post("/agents/5/kill")
        assert r.status_code == 200
        assert called == ["%5"]

    def test_kill_handles_failure(self, authed_client, monkeypatch):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        monkeypatch.setattr("agent_hq.web.errors.kill_agent", lambda pane_id: False)
        r = client.post("/agents/5/kill")
        assert r.status_code == 200
        assert (
            "could not" in r.text.lower() or "fail" in r.text.lower() or "error" in r.text.lower()
        )

    def test_action_404_on_unknown_slug(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.post("/agents/999/kill")
        assert r.status_code == 404


class TestPinning:
    """Pin/unpin POST route + pinned-section rendering (#141).

    Pin state is keyed on ``Agent.stable_key`` and held in
    ``app.state.pinned_store`` (a ``PinnedSessionsStore`` whose default path
    the conftest fixture redirects into tmp, so these tests stay hermetic).
    """

    def _agent_with_key(self, pane_id: str, key: str, **kw):
        a = _mk_agent(pane_id, **kw)
        a.claude_transcript_session_id = key
        return a

    def _codex_agent_with_thread(self, pane_id: str, codex_thread_id: str, **kw):
        """A codex agent whose thread UUID has been captured, so its
        ``stable_key`` upgraded past the pre-upgrade ``legacy_stable_key``
        (#205 Tasks 1-3)."""
        a = _mk_agent(pane_id, **kw)
        a.agent_type = AgentType.CODEX
        a.codex_thread_id = codex_thread_id
        return a

    def test_pin_state_true_adds_stable_key(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [self._agent_with_key("%5", "trans-5")])
        r = client.post("/agents/5/pin", data={"state": "true"})
        assert r.status_code == 200
        assert "trans-5" in client.app.state.pinned_store.keys()

    def test_pin_state_false_removes_stable_key(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [self._agent_with_key("%5", "trans-5")])
        client.app.state.pinned_store.add("trans-5")
        r = client.post("/agents/5/pin", data={"state": "false"})
        assert r.status_code == 200
        assert "trans-5" not in client.app.state.pinned_store.keys()

    def test_pin_is_idempotent(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [self._agent_with_key("%5", "trans-5")])
        client.post("/agents/5/pin", data={"state": "true"})
        client.post("/agents/5/pin", data={"state": "true"})
        assert client.app.state.pinned_store.keys() == {"trans-5"}

    def test_pin_writes_only_stable_key(self, authed_client):
        """#205 Codex review: pinning writes ONLY ``stable_key``. The
        earlier dual-write left a ``tool:cwd:pane_id`` entry behind, so
        after the pinned session exited, an unrelated future session in the
        same pane and cwd collided with it and rendered pinned."""
        client, cache_path, _ = authed_client
        agent = self._codex_agent_with_thread("%5", "uuid-7")
        _write_cache(cache_path, [agent])
        r = client.post("/agents/5/pin", data={"state": "true"})
        assert r.status_code == 200
        keys = client.app.state.pinned_store.keys()
        assert keys == {"uuid-7"}

        # Successor collision (the flagged failure): a different codex
        # session reusing the pane + cwd must not inherit the pin.
        successor = self._codex_agent_with_thread("%5", "uuid-other")
        assert not successor.is_pinned_in(keys)
        successor.codex_thread_id = None
        assert not successor.is_pinned_in(keys)

    def test_pin_404_on_unknown_slug(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.post("/agents/999/pin", data={"state": "true"})
        assert r.status_code == 404

    def test_pin_publishes_pins_changed(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [self._agent_with_key("%5", "trans-5")])
        broker = client.app.state.broker
        before = len(broker.replay_since(0))
        client.post("/agents/5/pin", data={"state": "true"})
        new_events = broker.replay_since(0)[before:]
        assert any(
            e.topic == "agent.pins_changed" and e.data.get("pane_id") == "%5" for e in new_events
        )

    def test_list_floats_pinned_to_top_section(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [
                self._agent_with_key("%1", "trans-1", session_slug="plain-one"),
                self._agent_with_key("%2", "trans-2", session_slug="pinned-one"),
            ],
        )
        client.app.state.pinned_store.add("trans-2")
        r = client.get("/agents/list")
        assert "data-pinned-section" in r.text
        # The pinned agent renders before the plain one.
        assert r.text.find("pinned-one") < r.text.find("plain-one")

    def test_phase_excludes_pinned_from_its_group(self, authed_client):
        client, cache_path, _ = authed_client
        # Single IMPLEMENT agent, pinned → the implement phase group must
        # disappear entirely (the agent is shown only in the pinned section).
        _write_cache(
            cache_path,
            [
                self._agent_with_key(
                    "%5", "trans-5", phase=DevPhase.IMPLEMENT, session_slug="impl-pinned"
                )
            ],
        )
        client.app.state.pinned_store.add("trans-5")
        r = client.get("/agents/phase")
        assert "data-pinned-section" in r.text
        assert "impl-pinned" in r.text
        assert 'data-phase="implement"' not in r.text

    def test_kanban_floats_pinned_above_columns(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [
                self._agent_with_key(
                    "%5", "trans-5", phase=DevPhase.IMPLEMENT, session_slug="impl-pinned"
                )
            ],
        )
        client.app.state.pinned_store.add("trans-5")
        r = client.get("/agents/kanban")
        assert "data-pinned-section" in r.text
        # Pinned band precedes the kanban board.
        assert r.text.find("data-pinned-section") < r.text.find('class="kanban"')

    def test_kanban_pinned_card_targets_overlay_not_hidden_detail(self, authed_client):
        """In kanban view the side ``#detail-slot`` is hidden on desktop, so a
        pinned card must use the kanban variant (overlay target) — otherwise
        clicking it swaps detail into a hidden slot and nothing appears."""
        import re

        client, cache_path, _ = authed_client
        _write_cache(
            cache_path,
            [self._agent_with_key("%5", "trans-5", phase=DevPhase.IMPLEMENT)],
        )
        client.app.state.pinned_store.add("trans-5")
        r = client.get("/agents/kanban")
        m = re.search(r"data-pinned-section(.*?)</section>", r.text, re.S)
        assert m is not None
        band = m.group(1)
        assert "card--kanban" in band
        assert "/agents/5/overlay" in band
        assert "#detail-slot" not in band

    def test_pinned_card_has_pinned_class_and_glyph(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [self._agent_with_key("%5", "trans-5")])
        client.app.state.pinned_store.add("trans-5")
        r = client.get("/agents/list")
        assert "card--pinned" in r.text
        # Shared pin chrome (#247 follow-up): SVG glyph pill, active state.
        assert "pin-glyph" in r.text
        assert "pin-btn--active" in r.text

    def test_card_has_pin_button(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [self._agent_with_key("%5", "trans-5")])
        r = client.get("/agents/list")
        # Each card carries a pin toggle posting to the pin route.
        assert "/agents/5/pin" in r.text

    def test_card_swap_reflects_pin_state(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [self._agent_with_key("%5", "trans-5")])
        client.app.state.pinned_store.add("trans-5")
        r = client.get("/agents/5/card")
        assert "card--pinned" in r.text

    def test_detail_shows_pin_button(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/5")
        assert "/agents/5/pin" in r.text

    def test_legacy_pinned_codex_agent_still_shows_pinned(self, authed_client):
        """A codex agent pinned before its thread UUID was captured is
        pinned under ``legacy_stable_key``. Once the UUID is captured,
        ``stable_key`` upgrades — the pre-upgrade pin must still float the
        card into the pinned section (#205 Task 7), render with the
        ``card--pinned`` class, and show its pin button as pressed. The
        templates independently recompute pin status
        (``card_comfortable.html``/``card_kanban.html``/``detail_body.html``)
        so this must hold even though the route-level partitioning above is
        already fixed."""
        client, cache_path, _ = authed_client
        agent = self._codex_agent_with_thread("%5", "uuid-7", session_slug="legacy-pinned")
        _write_cache(cache_path, [agent])
        client.app.state.pinned_store.add(agent.legacy_stable_key)
        r = client.get("/agents/list")
        assert "data-pinned-section" in r.text
        assert "legacy-pinned" in r.text
        assert "card--pinned" in r.text
        assert 'aria-pressed="true"' in r.text

    def test_legacy_pinned_codex_agent_card_endpoint_shows_pinned(self, authed_client):
        """The single-card swap endpoint (``/agents/{slug}/card``, used by the
        SSE listener) must independently render the legacy-key pin as pinned
        (#205 Task 7) — it renders ``card_comfortable.html`` directly, not via
        the list route's already-fixed partitioning."""
        client, cache_path, _ = authed_client
        agent = self._codex_agent_with_thread("%5", "uuid-7")
        _write_cache(cache_path, [agent])
        client.app.state.pinned_store.add(agent.legacy_stable_key)
        r = client.get("/agents/5/card")
        assert "card--pinned" in r.text
        assert 'aria-pressed="true"' in r.text

    def test_legacy_pinned_codex_agent_detail_shows_pinned_and_unpin(self, authed_client):
        """The detail sheet (``detail_body.html``) must also treat a
        legacy-key pin as pinned — otherwise its "Pin"/"Unpin" button label
        and posted ``state`` are wrong, so clicking it re-pins instead of
        unpinning (#205 Task 7)."""
        client, cache_path, _ = authed_client
        agent = self._codex_agent_with_thread("%5", "uuid-7")
        _write_cache(cache_path, [agent])
        client.app.state.pinned_store.add(agent.legacy_stable_key)
        r = client.get("/agents/5")
        assert "Unpin" in r.text
        assert 'hx-vals=\'{"state": "false"}\'' in r.text

    def test_unpin_clears_both_stable_and_legacy_keys(self, authed_client):
        """Unpinning must clear both key forms so a card pinned under either
        the pre- or post-upgrade key doesn't stay stuck pinned (#205 Task 7)."""
        client, cache_path, _ = authed_client
        agent = self._codex_agent_with_thread("%5", "uuid-7")
        _write_cache(cache_path, [agent])
        client.app.state.pinned_store.add(agent.legacy_stable_key)
        r = client.post("/agents/5/pin", data={"state": "false"})
        assert r.status_code == 200
        assert agent.legacy_stable_key not in client.app.state.pinned_store.keys()
        assert agent.stable_key not in client.app.state.pinned_store.keys()


class TestActionSSEInvalidate:
    """Action routes (rename, kill) should publish ``agent.invalidated``
    so connected tabs swap their card on success — same path the phase
    routes use. Mute is the exception since #278: it moves the card between
    sections, so it publishes ``agent.mute_changed`` (whole-view refetch)
    INSTEAD of a card invalidation — both would make every tab refetch the
    view twice."""

    def test_mute_publishes_mute_changed_not_invalidate(self, authed_client, monkeypatch):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        from agent_hq.web.routes import agents as agents_routes_mod

        monkeypatch.setattr(
            agents_routes_mod,
            "compute_content_fingerprint",
            lambda pane_id: "fp",
        )
        # Capture publishes by inspecting the broker on the test app
        # via a peek into the test client's app state.
        app = client.app
        broker = app.state.broker
        before = len(broker.replay_since(0))
        client.post("/agents/5/mute", data={"state": "true"})
        after = broker.replay_since(0)
        new_events = after[before:]
        assert [e.topic for e in new_events] == ["agent.mute_changed"]
        assert new_events[0].data.get("pane_id") == "%5"

    def test_rename_publishes_invalidate(self, authed_client, monkeypatch):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        monkeypatch.setattr(
            "agent_hq.web.errors.rename_session",
            lambda pane_id, new_name=None: True,
        )
        app = client.app
        broker = app.state.broker
        before = len(broker.replay_since(0))
        client.post("/agents/5/rename", data={"name": "x"})
        after = broker.replay_since(0)
        new_events = after[before:]
        assert any(
            e.topic == "agent.invalidated" and e.data.get("pane_id") == "%5" for e in new_events
        )

    def test_kill_publishes_invalidate(self, authed_client, monkeypatch):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        monkeypatch.setattr("agent_hq.web.errors.kill_agent", lambda pane_id: True)
        app = client.app
        broker = app.state.broker
        before = len(broker.replay_since(0))
        client.post("/agents/5/kill")
        after = broker.replay_since(0)
        new_events = after[before:]
        assert any(
            e.topic == "agent.invalidated" and e.data.get("pane_id") == "%5" for e in new_events
        )

    async def test_publish_invalidate_skips_refresh_for_plain_actions(self):
        """Plain action routes (default ``re_resolve=False``) publish a
        bare event directly and must NOT call ``watcher.refresh_pane`` —
        a re-resolve would burn a hook-file read plus git / ``gh pr view``
        for an action that changes nothing the resolver reads. Phase
        routes pass ``re_resolve=True`` to opt back in."""
        from unittest.mock import AsyncMock, MagicMock

        from agent_hq.web.routes.agents import _publish_invalidate

        app = MagicMock()
        app.state.watcher = MagicMock()
        app.state.watcher.refresh_pane = AsyncMock()
        app.state.broker = MagicMock()
        app.state.broker.publish = AsyncMock()

        await _publish_invalidate(app, "%5")
        app.state.watcher.refresh_pane.assert_not_called()
        app.state.broker.publish.assert_awaited_once()

        await _publish_invalidate(app, "%5", re_resolve=True)
        app.state.watcher.refresh_pane.assert_awaited_once_with("%5")


class TestJumpTimeoutCeiling:
    """#170 audit AHQ-010: the outer wait_for was 2.0s while perform_jump's
    internal osascript alone can take ~2.1s — a successful jump reported
    "Jump timed out." and skipped the invalidate publish. The ceiling must
    exceed the worst-case thread time (4.0s, matching /agents/new)."""

    def test_slow_successful_jump_is_not_reported_as_timeout(self, authed_client):
        from unittest.mock import patch

        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])

        def slow_jump(pane_id, focus=True):
            time.sleep(2.5)  # > old 2.0s ceiling, < the 4.0s one
            return True

        with patch("agent_hq.web.jump.perform_jump", side_effect=slow_jump):
            r = client.post("/agents/5/jump")
        assert r.status_code == 200
        assert "Jump timed out." not in r.text


# --- #171: transcript-local history ---------------------------------------


def _hist_user(text):
    return {"type": "user", "message": {"content": text}}


def _hist_assistant(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _hist_tool(name, tool_input):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name, "input": tool_input}]},
    }


def _agent_with_transcript(records, pane_id="%5", session_id="hist-sess"):
    """Build a Claude agent plus a real transcript JSONL at the path
    ``transcript_path_for`` computes (CLAUDE_PROJECTS_DIR is redirected
    into tmp by the autouse isolation fixture)."""
    from pathlib import Path

    from agent_hq.transcript import transcript_path_for

    agent = _mk_agent(pane_id)
    agent.claude_transcript_session_id = session_id
    path = Path(transcript_path_for(agent.cwd, session_id)[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return agent


class TestAgentHistory:
    """``GET /agents/{slug}/history`` — transcript-local scrollback (#171).

    History pages are rendered from the agent's transcript JSONL with
    zero pane interaction; the top sentinel drives infinite upward
    scroll via ``hx-trigger="intersect once"``.
    """

    def test_renders_turns_with_kind_classes(self, authed_client):
        client, cache_path, _ = authed_client
        agent = _agent_with_transcript(
            [
                _hist_user("fix the bug"),
                _hist_assistant("Looking at it."),
                _hist_tool("Bash", {"command": "git status"}),
            ]
        )
        _write_cache(cache_path, [agent])
        r = client.get("/agents/5/history")
        assert r.status_code == 200
        assert "history-turn--user" in r.text
        assert "fix the bug" in r.text
        assert "history-turn--assistant" in r.text
        assert "Looking at it." in r.text
        assert "history-turn--tool" in r.text
        assert "Bash(git status)" in r.text

    def test_single_page_has_no_sentinel_but_marks_beginning(self, authed_client):
        client, cache_path, _ = authed_client
        agent = _agent_with_transcript([_hist_user("only message")])
        _write_cache(cache_path, [agent])
        r = client.get("/agents/5/history")
        assert "history-sentinel" not in r.text
        assert "history-beginning" in r.text

    def test_paginates_via_intersect_sentinel(self, authed_client):
        client, cache_path, _ = authed_client
        agent = _agent_with_transcript([_hist_user(f"msg {i:03d}") for i in range(100)])
        _write_cache(cache_path, [agent])
        r = client.get("/agents/5/history")
        assert "msg 099" in r.text
        assert "msg 000" not in r.text
        # ``intersect once``, NOT ``revealed``: htmx's revealed trigger only
        # listens to window scroll, and the history scrolls inside the
        # .pane-preview-host container — caught live in Phase E (#171).
        assert "history-sentinel" in r.text
        assert 'hx-trigger="intersect once"' in r.text
        assert 'hx-swap="outerHTML"' in r.text
        # Follow sentinel cursors back to the beginning of the session.
        seen = r.text
        cursor_re = re.compile(r'hx-get="(/agents/5/history\?before=\d+)"')
        hops = 0
        while (m := cursor_re.search(seen)) and hops < 10:
            r = client.get(m.group(1))
            assert r.status_code == 200
            seen = r.text
            hops += 1
        assert hops >= 1
        assert "msg 000" in seen
        assert "history-beginning" in seen

    def test_unknown_agent_404s(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/999/history")
        assert r.status_code == 404

    def test_agent_without_transcript_shows_unavailable(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])  # no transcript session id
        r = client.get("/agents/5/history")
        assert r.status_code == 200
        assert "history-unavailable" in r.text
        assert "history-sentinel" not in r.text

    def test_unauthenticated_is_rejected(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_path)
        _write_cache(cache_path, [_mk_agent("%5")])
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file)
        client = TestClient(app)
        r = client.get("/agents/5/history", follow_redirects=False)
        assert r.status_code in (302, 303, 401)


class TestPreviewModeToggle:
    """Live | History segmented control in the detail body (#171),
    gated on a locatable transcript."""

    def test_detail_shows_toggle_when_transcript_exists(self, authed_client):
        client, cache_path, _ = authed_client
        agent = _agent_with_transcript([_hist_user("hello")])
        _write_cache(cache_path, [agent])
        r = client.get("/agents/5")
        assert "data-preview-toggle" in r.text
        assert "/agents/5/history" in r.text
        assert "/agents/5/preview/live" in r.text

    def test_detail_hides_toggle_without_transcript(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/5")
        assert "data-preview-toggle" not in r.text
        assert "/agents/5/history" not in r.text

    def test_overlay_shows_toggle_when_transcript_exists(self, authed_client):
        client, cache_path, _ = authed_client
        agent = _agent_with_transcript([_hist_user("hello")])
        _write_cache(cache_path, [agent])
        r = client.get("/agents/5/overlay")
        assert "data-preview-toggle" in r.text

    def test_preview_live_partial_restores_polling(self, authed_client):
        """Switching back to Live swaps in an element that re-establishes
        the every-3s poll (the History swap removed the old one)."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/5/preview/live")
        assert r.status_code == 200
        assert "preview-live" in r.text
        assert 'hx-get="/agents/5/preview"' in r.text
        assert 'hx-trigger="load, every 3s"' in r.text


@pytest.fixture
def history_prefs_client(tmp_path, monkeypatch):
    """Authenticated client with BOTH an injected UiPrefsStore and a
    writable agents cache — for the sticky preview-mode tests."""
    from agent_hq.ui_prefs import UiPrefsStore

    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_path)
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    store = UiPrefsStore(path=tmp_path / "ui-prefs.json")
    app = create_app(token_file=tm.token_file, ui_prefs_store=store)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client, store, cache_path


class TestPreviewModePersistence:
    """#171 follow-up: the Live|History choice is sticky. Explicit toggle
    clicks persist ``web_preview_mode`` to the shared UiPrefsStore; the
    detail body then opens every agent in the remembered mode."""

    def test_history_toggle_click_persists(self, history_prefs_client):
        client, store, cache_path = history_prefs_client
        _write_cache(cache_path, [_agent_with_transcript([_hist_user("hello")])])
        r = client.get("/agents/5/history?persist=1")
        assert r.status_code == 200
        assert store.get_web_preview_mode() == "history"

    def test_plain_history_get_does_not_persist(self, history_prefs_client):
        """The initial-load fetch and the pagination sentinel must not
        write the pref — only explicit toggle clicks carry persist=1."""
        client, store, cache_path = history_prefs_client
        _write_cache(cache_path, [_agent_with_transcript([_hist_user(f"m{i}") for i in range(50)])])
        r = client.get("/agents/5/history")
        assert r.status_code == 200
        m = re.search(r'hx-get="(/agents/5/history\?before=\d+)"', r.text)
        assert m  # sentinel present
        assert "persist" not in m.group(1)
        client.get(m.group(1))
        assert store.get_web_preview_mode() is None

    def test_live_toggle_click_persists(self, history_prefs_client):
        client, store, cache_path = history_prefs_client
        store.set_web_preview_mode("history")
        _write_cache(cache_path, [_agent_with_transcript([_hist_user("hello")])])
        r = client.get("/agents/5/preview/live?persist=1")
        assert r.status_code == 200
        assert store.get_web_preview_mode() == "live"

    def test_toggle_buttons_carry_persist(self, history_prefs_client):
        client, store, cache_path = history_prefs_client
        _write_cache(cache_path, [_agent_with_transcript([_hist_user("hello")])])
        r = client.get("/agents/5")
        assert 'hx-get="/agents/5/history?persist=1"' in r.text
        assert 'hx-get="/agents/5/preview/live?persist=1"' in r.text

    def test_detail_opens_in_history_when_preferred(self, history_prefs_client):
        client, store, cache_path = history_prefs_client
        store.set_web_preview_mode("history")
        _write_cache(cache_path, [_agent_with_transcript([_hist_user("hello")])])
        r = client.get("/agents/5")
        # History button is the active segment…
        assert re.search(r'is-active"[^>]*hx-get="/agents/5/history\?persist=1"', r.text)
        assert not re.search(r'is-active"[^>]*hx-get="/agents/5/preview/live\?persist=1"', r.text)
        # …and the host boots into history (load-triggered fetch), not the
        # live poll element.
        assert re.search(r'history-loading"[^>]*hx-get="/agents/5/history"', r.text)
        assert 'class="preview-live"' not in r.text

    def test_overlay_opens_in_history_when_preferred(self, history_prefs_client):
        client, store, cache_path = history_prefs_client
        store.set_web_preview_mode("history")
        _write_cache(cache_path, [_agent_with_transcript([_hist_user("hello")])])
        r = client.get("/agents/5/overlay")
        assert re.search(r'history-loading"[^>]*hx-get="/agents/5/history"', r.text)

    def test_detail_defaults_to_live_without_pref(self, history_prefs_client):
        client, store, cache_path = history_prefs_client
        _write_cache(cache_path, [_agent_with_transcript([_hist_user("hello")])])
        r = client.get("/agents/5")
        assert re.search(r'is-active"[^>]*hx-get="/agents/5/preview/live\?persist=1"', r.text)
        assert 'class="preview-live"' in r.text
        assert "history-loading" not in r.text

    def test_history_pref_without_transcript_falls_back_to_live(self, history_prefs_client):
        """A transcript-less agent can't render history: show Live (no
        toggle), but leave the stored preference untouched."""
        client, store, cache_path = history_prefs_client
        store.set_web_preview_mode("history")
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents/5")
        assert "data-preview-toggle" not in r.text
        assert 'class="preview-live"' in r.text
        assert "history-loading" not in r.text
        assert store.get_web_preview_mode() == "history"


class TestAgentsInlineFirstPaint:
    """#226: agent data lives in process memory (watcher snapshot / cache
    file), yet the page shipped a 'Discovering agents…' shell and needed a
    second round trip to show anything. The page GET now renders the view
    partial inline; the htmx trigger drops ``load`` so no duplicate fetch
    fires on boot."""

    def test_page_renders_cards_inline(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5", branch="feature/99-inline-paint")])
        r = client.get("/agents")
        assert r.status_code == 200
        assert "feature/99-inline-paint" in r.text
        assert "Discovering agents…" not in r.text

    def test_page_trigger_omits_load(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        r = client.get("/agents")
        assert 'hx-trigger="every 10s' in r.text
        assert 'hx-trigger="load' not in r.text

    def test_partial_routes_still_serve(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5", branch="feature/99-partial")])
        r = client.get("/agents/phase")
        assert r.status_code == 200
        assert "feature/99-partial" in r.text


@pytest.mark.parametrize("url", ["/agents/list", "/agents/kanban", "/agents/1"])
def test_native_backend_label_and_reason_render_on_every_surface(authed_client, monkeypatch, url):
    from agent_hq.status_types import LiveStatus

    client, cache_path, _ = authed_client
    agent = _mk_agent("%1", AgentState.WAITING)
    agent.status_snapshot = LiveStatus(
        "waiting",
        "future-native",
        "Novel <status>",
        'Approve "command"',
        "claude",
        "native-1",
        1,
        time.time(),
        time.monotonic_ns(),
    )
    _write_native_cache(cache_path, monkeypatch, [agent])
    response = client.get(url)
    assert response.status_code == 200
    assert "state-dot--waiting" in response.text
    assert "Novel &lt;status&gt;" in response.text
    assert "Approve &#34;command&#34;" in response.text
    assert "Novel <status>" not in response.text


class TestMutedSection:
    """Muted agents sink into a labelled bottom section — below Done — in
    every Agents view (#278). Mirrors the Pinned top section; pinned wins
    over muted for placement."""

    @staticmethod
    def _mute(inactive_path, *pane_ids):
        inactive_path.write_text(
            json.dumps({p: {"marked_at": time.time(), "fingerprint": "abc"} for p in pane_ids})
        )

    def test_list_renders_muted_section_last(self, authed_client):
        client, cache_path, inactive_path = authed_client
        _write_cache(
            cache_path,
            [
                _mk_agent("%1", session_slug="active-one"),
                _mk_agent("%9", session_slug="muted-one"),
            ],
        )
        self._mute(inactive_path, "%9")
        r = client.get("/agents/list")
        assert "data-muted-section" in r.text
        assert 'muted-separator">muted' not in r.text
        section = r.text.find("data-muted-section")
        assert r.text.find("active-one") < section < r.text.find("muted-one")

    def test_phase_muted_below_done_and_out_of_its_group(self, authed_client):
        client, cache_path, inactive_path = authed_client
        _write_cache(
            cache_path,
            [
                _mk_agent("%1", phase=DevPhase.IMPLEMENT, session_slug="impl-active"),
                _mk_agent("%2", phase=DevPhase.DONE, session_slug="done-one"),
                _mk_agent("%3", phase=DevPhase.IMPLEMENT, session_slug="impl-muted"),
            ],
        )
        self._mute(inactive_path, "%3")
        r = client.get("/agents/phase")
        assert "data-muted-section" in r.text
        done_group = r.text.find('data-phase="done"')
        muted_section = r.text.find("data-muted-section")
        assert done_group < muted_section < r.text.find("impl-muted")
        # The muted agent is not inside the Implement group.
        impl_group = r.text.find('data-phase="implement"')
        impl_end = r.text.find("</section>", impl_group)
        assert "impl-muted" not in r.text[impl_group:impl_end]
        assert "impl-active" in r.text[impl_group:impl_end]
        assert r.text.count("impl-muted") == r.text.count("impl-active")

    def test_phase_muted_and_done_lands_in_muted_section(self, authed_client):
        client, cache_path, inactive_path = authed_client
        _write_cache(cache_path, [_mk_agent("%2", phase=DevPhase.DONE, session_slug="done-muted")])
        self._mute(inactive_path, "%2")
        r = client.get("/agents/phase")
        assert "data-muted-section" in r.text
        assert 'data-phase="done"' not in r.text
        assert "done-muted" in r.text
        assert "No agents discovered" not in r.text

    def test_kanban_muted_band_below_columns(self, authed_client):
        client, cache_path, inactive_path = authed_client
        _write_cache(
            cache_path,
            [
                _mk_agent("%1", phase=DevPhase.IMPLEMENT, session_slug="impl-active"),
                _mk_agent("%3", phase=DevPhase.IMPLEMENT, session_slug="impl-muted"),
            ],
        )
        self._mute(inactive_path, "%3")
        r = client.get("/agents/kanban")
        assert r.text.find('class="kanban"') < r.text.find("data-muted-section")
        board_end = r.text.find("data-muted-section")
        assert "impl-muted" not in r.text[:board_end]
        # The band uses the kanban card variant (overlay target).
        assert "card--kanban" in r.text[board_end:]

    def test_pinned_wins_over_muted(self, authed_client):
        client, cache_path, inactive_path = authed_client
        a = _mk_agent("%5", session_slug="pinned-muted")
        a.claude_transcript_session_id = "trans-5"
        _write_cache(cache_path, [a])
        self._mute(inactive_path, "%5")
        client.app.state.pinned_store.add("trans-5")
        r = client.get("/agents/list")
        assert "data-pinned-section" in r.text
        assert "data-muted-section" not in r.text
        assert r.text.count("pinned-muted") == 1

    def test_no_muted_section_when_none_muted(self, authed_client):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%1")])
        for view in ("list", "phase", "kanban"):
            assert "data-muted-section" not in client.get(f"/agents/{view}").text

    def test_mute_publishes_mute_changed(self, authed_client, monkeypatch):
        """Muting moves a card between sections, which a per-card swap can't
        express — the route publishes ``agent.mute_changed`` so tabs refetch
        the whole view (same mechanism as ``agent.pins_changed``)."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5")])
        broker = client.app.state.broker
        before = len(broker.replay_since(0))
        from agent_hq.web.routes import agents as agents_routes_mod

        monkeypatch.setattr(agents_routes_mod, "compute_content_fingerprint", lambda pane_id: "fp")
        client.post("/agents/5/mute", data={"state": "true"})
        new_events = broker.replay_since(0)[before:]
        assert any(
            e.topic == "agent.mute_changed" and e.data.get("pane_id") == "%5" for e in new_events
        )


class TestAnsiToHtmlHardening:
    """#263 F1 follow-up: the OSC strip must be linear-time and leave no ESC
    that ansi2html could read as anything but an SGR/CSI sequence."""

    def test_deeply_nested_osc_payload_is_linear(self):
        import time

        from agent_hq.web.routes.agents import ansi_to_html

        inner = "\x1b]0;x\x07"
        depth = 20000
        payload = "\x1b" * depth + inner + "]" * depth + "tail"
        start = time.perf_counter()
        html = ansi_to_html(payload)
        assert time.perf_counter() - start < 2.0
        assert "tail" in html

    def test_no_non_csi_escape_reaches_the_converter(self, monkeypatch):
        from agent_hq.web.routes import agents as mod

        seen = {}

        def spy(text, full=False):
            seen["text"] = text
            return ""

        monkeypatch.setattr(mod._ansi, "convert", spy)
        mod.ansi_to_html("\x1b\x1b]0;x\x07]8;;u\x07k\x1b(B\x1b[31mred\x1b[0m\x1b")
        text = seen["text"]
        assert "\x1b]" not in text
        positions = [i for i, ch in enumerate(text) if ch == "\x1b"]
        assert positions, "SGR colour sequences must survive"
        assert all(text[i + 1 : i + 2] == "[" for i in positions)

    def test_non_csi_sequences_are_dropped_whole(self, monkeypatch):
        """``tput sgr0`` emits ``ESC ( B``; charset, keypad and save/restore
        escapes must vanish entirely, not leak their trailing bytes as text
        (#263 review)."""
        from agent_hq.web.routes import agents as mod

        seen = {}
        monkeypatch.setattr(
            mod._ansi, "convert", lambda text, full=False: seen.setdefault("t", text)
        )
        mod.ansi_to_html("\x1b[31mred\x1b(B\x1b[m \x1b7x\x1b8 \x1b=k\x1b>\x1b")
        assert seen["t"] == "\x1b[31mred\x1b[m x k"


class TestAgentsDataStates:
    """#338: the Agents tab says what it knows about its data — loading, a
    failed or stale read, or a genuinely empty list — instead of rendering
    bare phase headers or an empty board."""

    VIEWS = ("list", "phase", "kanban")

    class _FakeWatcher:
        def __init__(self, agents, health):
            self._agents, self._health = agents, health

        def snapshot(self):
            return list(self._agents)

        def health(self):
            return self._health

    def _with_watcher(self, client, agents, **health):
        from agent_hq.web.watcher import CacheHealth

        client.app.state.watcher = self._FakeWatcher(agents, CacheHealth(**health))

    @pytest.mark.parametrize("view", VIEWS)
    def test_fresh_empty_cache_shows_explicit_empty_state(self, authed_client, view):
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [])
        r = client.get(f"/agents/{view}")
        assert r.status_code == 200
        assert "No agents discovered yet" in r.text
        assert "data-agents-notice" not in r.text

    @pytest.mark.parametrize("view", VIEWS)
    def test_unreadable_cache_shows_error_not_empty_state(self, authed_client, view):
        client, cache_path, _ = authed_client
        cache_path.write_text('{"schema_version": 2, "agents": [')
        r = client.get(f"/agents/{view}")
        assert r.status_code == 200
        assert 'data-agents-notice="error"' in r.text
        assert 'role="status"' in r.text
        assert "could not be read" in r.text
        assert "No agents discovered yet" not in r.text

    @pytest.mark.parametrize("view", VIEWS)
    def test_missing_cache_shows_daemon_error(self, authed_client, view):
        client, _cache_path, _ = authed_client
        r = client.get(f"/agents/{view}")
        assert 'data-agents-notice="error"' in r.text
        assert "daemon" in r.text
        assert "No agents discovered yet" not in r.text

    @pytest.mark.parametrize("view", VIEWS)
    def test_loading_state_before_first_read(self, authed_client, view):
        client, _cache_path, _ = authed_client
        self._with_watcher(client, [], loaded=False, problem=None, updated_at=None)
        r = client.get(f"/agents/{view}")
        assert "Discovering agents…" in r.text
        assert "No agents discovered yet" not in r.text
        assert "data-agents-notice" not in r.text

    @pytest.mark.parametrize("view", VIEWS)
    def test_stale_data_keeps_cards_and_says_so(self, authed_client, view):
        """The daemon stopped writing (its tick keeps failing, #317): the
        cards are the last good list, so they stay — under a warning."""
        client, _cache_path, _ = authed_client
        agent = _mk_agent("%5", branch="feature/stale-card", phase=DevPhase.IMPLEMENT)
        self._with_watcher(
            client, [agent], loaded=True, problem=None, updated_at=time.time() - 600
        )
        r = client.get(f"/agents/{view}")
        assert "feature/stale-card" in r.text
        assert 'data-agents-notice="stale"' in r.text
        assert "out of date" in r.text
        assert "10m ago" in r.text

    @pytest.mark.parametrize("view", VIEWS)
    def test_fresh_watcher_data_has_no_notice(self, authed_client, view):
        client, _cache_path, _ = authed_client
        agent = _mk_agent("%5", branch="feature/fresh-card")
        self._with_watcher(client, [agent], loaded=True, problem=None, updated_at=time.time())
        r = client.get(f"/agents/{view}")
        assert "feature/fresh-card" in r.text
        assert "data-agents-notice" not in r.text

    def test_unreadable_after_good_read_keeps_cards_under_error(self, authed_client):
        client, _cache_path, _ = authed_client
        agent = _mk_agent("%5", branch="feature/kept-card", phase=DevPhase.QA)
        self._with_watcher(
            client, [agent], loaded=True, problem="unreadable", updated_at=time.time() - 150
        )
        r = client.get("/agents/phase")
        assert "feature/kept-card" in r.text
        assert 'data-agents-notice="error"' in r.text
        assert "Showing the list the daemon wrote 2m ago" in r.text

    def test_page_inline_paint_carries_notice(self, authed_client):
        client, cache_path, _ = authed_client
        cache_path.write_text("not json")
        r = client.get("/agents")
        assert 'data-agents-notice="error"' in r.text

    def test_fallback_shows_cards_from_an_older_cache_with_the_banner(self, authed_client):
        """No watcher (boot / tests): cards and banner come from ONE read, so a
        cache a few minutes old shows its cards under the stale banner instead
        of an empty list (review round 1)."""
        client, cache_path, _ = authed_client
        _write_cache(cache_path, [_mk_agent("%5", branch="feature/old-cache")])
        data = json.loads(cache_path.read_text())
        data["timestamp"] = time.time() - 300
        cache_path.write_text(json.dumps(data))
        r = client.get("/agents/phase")
        assert "feature/old-cache" in r.text
        assert 'data-agents-notice="stale"' in r.text
        assert "No agents discovered yet" not in r.text
