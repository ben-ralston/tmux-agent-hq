"""Tests for /agents/{slug}/phase/* routes.

These reuse ``agent_hq.overrides`` directly — no fork. Each action:

- ``POST /phase/override``  → ``write_override({action: 'override', ...})`` + jsonl
- ``DELETE /phase/override`` → ``delete_override`` + jsonl undo entry
- ``POST /phase/confirm``    → ``write_override({action: 'confirm', ...})`` + jsonl
- ``POST /note``             → jsonl only, no overrides.json entry

After the disk write the route calls ``_publish_invalidate`` which
delegates to ``watcher.refresh_pane`` — the watcher re-runs the resolver
against the just-written overrides.json (read under flock). There is no
``fallback_phase`` plumbing any more: the override write bumps
overrides.json's revision and the resolver picks it up directly.

overrides.json is now the v2 envelope ``{"revision": int, "entries": {...}}`` —
``_read_entries`` unwraps the entries dict for assertions.
"""

from __future__ import annotations

import asyncio
import json
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
    pane_id: str = "%5",
    phase: DevPhase | None = DevPhase.IMPLEMENT,
    claude_session_id: str | None = "test-session-id",
) -> Agent:
    """Build an Agent for tests. ``phase`` is wrapped in a ``PhaseSnapshot``
    where ``detected_phase`` == ``effective_phase`` == ``phase`` — i.e. the
    auto-detected value with no override applied. ``_auto_phase_for`` reads
    ``detected_phase`` so this fixes the recorded ``auto_phase``."""
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
        repo_root="/tmp/repo",
        repo_name="repo",
        branch="feature/x",
        state=AgentState.IDLE,
        phase_snapshot=snapshot,
        claude_session_id=claude_session_id,
    )


def _write_v2_cache(path, agents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
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


def _read_entries(overrides_path) -> dict:
    """Unwrap the v2 envelope's ``entries`` dict; ``{}`` if file missing."""
    if not overrides_path.exists():
        return {}
    data = json.loads(overrides_path.read_text())
    return data.get("entries", {}) if isinstance(data, dict) else {}


@pytest.fixture
def authed_client(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    _write_v2_cache(cache_path, [_mk_agent("%5")])
    monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_path)
    overrides_path = tmp_path / "overrides.json"
    monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_path)
    feedback_path = tmp_path / "phase-feedback.jsonl"
    monkeypatch.setattr("agent_hq.overrides.FEEDBACK_FILE", feedback_path)
    inactive_path = tmp_path / "inactive.json"
    monkeypatch.setattr("agent_hq.state.INACTIVE_FILE", inactive_path)

    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(token_file=tm.token_file)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client, overrides_path, feedback_path


class TestPhaseOverride:
    def test_writes_override_entry(self, authed_client):
        client, overrides_path, _ = authed_client
        r = client.post(
            "/agents/5/phase/override",
            data={"phase": "qa", "note": "kicking tires"},
        )
        assert r.status_code == 200
        entries = _read_entries(overrides_path)
        assert "%5" in entries
        assert entries["%5"]["action"] == "override"
        assert entries["%5"]["phase"] == "qa"
        assert entries["%5"]["note"] == "kicking tires"

    def test_bumps_overrides_revision(self, authed_client):
        client, overrides_path, _ = authed_client
        client.post("/agents/5/phase/override", data={"phase": "qa"})
        data = json.loads(overrides_path.read_text())
        # First write to a fresh file → revision 1.
        assert data["revision"] == 1

    def test_appends_to_feedback_jsonl(self, authed_client):
        client, _, feedback_path = authed_client
        client.post("/agents/5/phase/override", data={"phase": "qa"})
        lines = feedback_path.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "override"
        assert entry["user_phase"] == "qa"
        assert entry["pane_id"] == "%5"

    def test_rejects_invalid_phase(self, authed_client):
        client, *_ = authed_client
        r = client.post(
            "/agents/5/phase/override",
            data={"phase": "not-a-phase"},
        )
        assert r.status_code == 400

    def test_legacy_phase_alias_normalized_to_current(self, authed_client):
        """#108: a legacy ``plan``/``brainstorm`` value (e.g. from an older
        client or a stale form) must normalize to ``design`` instead of 400."""
        client, overrides_path, _ = authed_client
        r = client.post("/agents/5/phase/override", data={"phase": "plan"})
        assert r.status_code == 200
        assert _read_entries(overrides_path)["%5"]["phase"] == "design"

    def test_persists_session_id(self, authed_client):
        """Override entries carry the writing agent's claude_session_id
        so the resolver can scope the entry to that Claude session (#65)."""
        client, overrides_path, _ = authed_client
        client.post("/agents/5/phase/override", data={"phase": "qa"})
        assert _read_entries(overrides_path)["%5"]["session_id"] == "test-session-id"


class TestPhaseConfirm:
    def test_writes_confirm_entry(self, authed_client):
        client, overrides_path, _ = authed_client
        r = client.post("/agents/5/phase/confirm", data={"note": "looks right"})
        assert r.status_code == 200
        entries = _read_entries(overrides_path)
        assert "%5" in entries
        assert entries["%5"]["action"] == "confirm"
        assert entries["%5"].get("phase") in (None, "")

    def test_persists_session_id(self, authed_client):
        client, overrides_path, _ = authed_client
        client.post("/agents/5/phase/confirm")
        assert _read_entries(overrides_path)["%5"]["session_id"] == "test-session-id"

    def test_appends_jsonl_entry(self, authed_client):
        client, _, feedback_path = authed_client
        client.post("/agents/5/phase/confirm")
        entry = json.loads(feedback_path.read_text().splitlines()[0])
        assert entry["action"] == "confirm"


class TestPhaseClear:
    def test_delete_removes_entry(self, authed_client):
        client, overrides_path, _ = authed_client
        client.post("/agents/5/phase/override", data={"phase": "qa"})
        assert "%5" in _read_entries(overrides_path)

        r = client.delete("/agents/5/phase/override")
        assert r.status_code == 200
        assert "%5" not in _read_entries(overrides_path)

    def test_delete_bumps_revision(self, authed_client):
        client, overrides_path, _ = authed_client
        client.post("/agents/5/phase/override", data={"phase": "qa"})
        client.delete("/agents/5/phase/override")
        data = json.loads(overrides_path.read_text())
        # write(1) + delete(2)
        assert data["revision"] == 2

    def test_delete_logs_undo(self, authed_client):
        client, _, feedback_path = authed_client
        client.post("/agents/5/phase/override", data={"phase": "qa"})
        client.delete("/agents/5/phase/override")
        actions = [json.loads(line)["action"] for line in feedback_path.read_text().splitlines()]
        assert "undo" in actions

    def test_delete_returns_404_when_no_entry(self, authed_client):
        client, *_ = authed_client
        r = client.delete("/agents/5/phase/override")
        assert r.status_code == 404


class TestPhaseNote:
    def test_appends_only_jsonl_no_override(self, authed_client):
        client, overrides_path, feedback_path = authed_client
        r = client.post("/agents/5/note", data={"note": "stuck on flake test"})
        assert r.status_code == 200
        assert "%5" not in _read_entries(overrides_path)
        entry = json.loads(feedback_path.read_text().splitlines()[0])
        assert entry["action"] == "note"
        assert entry["note"] == "stuck on flake test"


class TestPhase404:
    def test_unknown_pane_404(self, authed_client):
        client, *_ = authed_client
        r = client.post("/agents/999/phase/override", data={"phase": "qa"})
        assert r.status_code == 404


class TestPhaseDiskErrors:
    """A failed overrides.json / feedback-log write must surface an error
    toast — not a bare 500 with an empty body that leaves the user
    thinking their action stuck when it didn't."""

    def _boom(self, *_a, **_k):
        raise OSError("no space left on device")

    def test_override_write_failure_returns_error_toast(self, authed_client, monkeypatch):
        client, overrides_path, _ = authed_client
        monkeypatch.setattr("agent_hq.web.routes.phase.write_override", self._boom)
        r = client.post("/agents/5/phase/override", data={"phase": "qa"})
        assert r.status_code == 200
        assert "disk error" in r.text
        assert "%5" not in _read_entries(overrides_path)

    def test_clear_failure_returns_error_toast(self, authed_client, monkeypatch):
        client, overrides_path, _ = authed_client
        client.post("/agents/5/phase/override", data={"phase": "qa"})
        monkeypatch.setattr("agent_hq.web.routes.phase.delete_override", self._boom)
        r = client.delete("/agents/5/phase/override")
        assert r.status_code == 200
        assert "disk error" in r.text
        # The entry is still there — the clear didn't take effect.
        assert "%5" in _read_entries(overrides_path)

    def test_note_write_failure_returns_error_toast(self, authed_client, monkeypatch):
        client, *_ = authed_client
        monkeypatch.setattr("agent_hq.web.routes.phase.append_feedback", self._boom)
        r = client.post("/agents/5/note", data={"note": "stuck"})
        assert r.status_code == 200
        assert "disk error" in r.text


class TestAutoPhasePreservation:
    """Repeat-override / confirm / undo must NOT clobber the original
    auto-detected phase. ``_auto_phase_for`` reads
    ``agent.phase_snapshot.detected_phase`` — the resolver records that
    separately from ``effective_phase``, so even after an override the
    auto-detected value is preserved."""

    def test_repeat_override_preserves_original_auto_phase(self, authed_client):
        client, overrides_path, _ = authed_client

        client.post("/agents/5/phase/override", data={"phase": "qa"})
        first = _read_entries(overrides_path)["%5"]
        assert first["auto_phase"] == "implement"

        client.post("/agents/5/phase/override", data={"phase": "review"})
        second = _read_entries(overrides_path)["%5"]
        # detected_phase in the cache snapshot is still IMPLEMENT — the
        # route didn't rewrite the cache, just overrides.json.
        assert second["auto_phase"] == "implement"

    def test_confirm_after_override_preserves_auto_phase(self, authed_client):
        client, overrides_path, _ = authed_client
        client.post("/agents/5/phase/override", data={"phase": "qa"})
        client.post("/agents/5/phase/confirm")
        entry = _read_entries(overrides_path)["%5"]
        assert entry["action"] == "confirm"
        assert entry["auto_phase"] == "implement"

    def test_undo_logs_real_auto_phase_not_overridden(self, authed_client):
        client, _, feedback_path = authed_client
        client.post("/agents/5/phase/override", data={"phase": "qa"})
        client.delete("/agents/5/phase/override")
        undo_line = next(
            json.loads(line)
            for line in feedback_path.read_text().splitlines()
            if json.loads(line)["action"] == "undo"
        )
        assert undo_line["auto_phase"] == "implement"


class TestClearRevertsViaResolver:
    """End-to-end: after a clear-override, the watcher's resolver re-runs
    against the now-absent entry and the in-memory snapshot's
    ``effective_phase`` reverts to ``detected_phase`` immediately — no
    ``fallback_phase`` plumbing needed because ``delete_override`` bumped
    overrides.json's revision and the resolver picked it up."""

    def test_detail_route_reverts_after_clear(self, tmp_path, monkeypatch):
        from agent_hq.web.events import Broker
        from agent_hq.web.watcher import Watcher

        # The resolver re-detects detected_phase each call (no carry-
        # forward). In a real repo ``detect_branch_phase`` is
        # deterministic; here the repo path is fake, so pin it.
        monkeypatch.setattr(
            "agent_hq.resolver.detect_branch_phase",
            lambda **_kw: DevPhase.IMPLEMENT,
        )

        cache_file = tmp_path / "cache.json"
        overrides_file = tmp_path / "overrides.json"
        feedback_file = tmp_path / "phase-feedback.jsonl"
        inactive_file = tmp_path / "inactive.json"

        # Daemon-realistic cache: snapshot records detected=IMPLEMENT,
        # effective=QA (override-applied), with feedback recorded.
        from agent_hq.models import PhaseFeedback

        agent = _mk_agent("%5", phase=DevPhase.IMPLEMENT)
        agent.phase_snapshot = PhaseSnapshot(
            detected_phase=DevPhase.IMPLEMENT,
            effective_phase=DevPhase.QA,
            phase_source="rule",
            phase_session_id=None,
            feedback=PhaseFeedback(
                action="override",
                phase=DevPhase.QA,
                auto_phase=DevPhase.IMPLEMENT,
                set_at="2026-05-10T00:00:00Z",
                note=None,
                session_id="test-session-id",
            ),
            inputs_observed=InputsObserved(overrides_revision=1, hook_state_revision=0),
            producer="daemon",
            producer_epoch=0.0,
            sequence=1,
            monotonic_ns=0,
            timestamp=0.0,
        )
        _write_v2_cache(cache_file, [agent])
        # Seed overrides at revision 1 with the override entry.
        overrides_file.write_text(
            json.dumps(
                {
                    "revision": 1,
                    "entries": {
                        "%5": {
                            "action": "override",
                            "phase": "qa",
                            "auto_phase": "implement",
                            "set_at": "2026-05-10T00:00:00Z",
                            "note": None,
                            "session_id": "test-session-id",
                        }
                    },
                }
            )
        )
        monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_file)
        monkeypatch.setattr("agent_hq.cache.CACHE_FILE", cache_file)
        monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_file)
        monkeypatch.setattr("agent_hq.overrides.FEEDBACK_FILE", feedback_file)
        monkeypatch.setattr("agent_hq.state.INACTIVE_FILE", inactive_file)

        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file)

        broker = Broker(buffer_size=50)
        watcher = Watcher(broker=broker, cache_poll_interval=60)
        app.state.broker = broker
        app.state.watcher = watcher

        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            asyncio.run(watcher._reload_from_cache())
            r = client.get("/agents/5")
            assert "phase-chip--qa" in r.text

            r = client.delete("/agents/5/phase/override")
            assert r.status_code == 200

            # The next detail fetch shows the reverted IMPLEMENT chip.
            r = client.get("/agents/5")
            assert "phase-chip--implement" in r.text
            assert "phase-chip--qa" not in r.text
