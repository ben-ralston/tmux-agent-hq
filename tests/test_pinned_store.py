"""Tests for the disk-backed PinnedSessionsStore (pin agents to top). #141

A set of stable agent keys persisted under ``~/.config/agent-hq/`` and shared
by the TUI and the launchd web service via the on-disk file.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_hq.pinned_store import PinnedSessionsStore


class TestPinnedSessionsStore:
    def test_add_and_keys(self, tmp_path: Path) -> None:
        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add("sess-abc")
        store.add("claude:/repo:%5")
        assert store.keys() == {"sess-abc", "claude:/repo:%5"}

    def test_is_pinned(self, tmp_path: Path) -> None:
        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add("sess-abc")
        assert store.is_pinned("sess-abc")
        assert not store.is_pinned("sess-xyz")

    def test_add_is_idempotent(self, tmp_path: Path) -> None:
        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add("sess-abc")
        store.add("sess-abc")
        assert store.keys() == {"sess-abc"}

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        PinnedSessionsStore(path).add("sess-abc")
        assert PinnedSessionsStore(path).keys() == {"sess-abc"}

    def test_missing_and_corrupt_return_empty(self, tmp_path: Path) -> None:
        assert PinnedSessionsStore(tmp_path / "nope.json").keys() == set()
        path = tmp_path / "p.json"
        path.write_text("not json")
        assert PinnedSessionsStore(path).keys() == set()

    def test_remove_unpins_key(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        store = PinnedSessionsStore(path)
        store.add("sess-abc")
        store.add("sess-def")
        assert store.remove("sess-abc") is True
        # Persisted across instances (the unpin survives a restart).
        assert PinnedSessionsStore(path).keys() == {"sess-def"}

    def test_remove_missing_key_is_noop(self, tmp_path: Path) -> None:
        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add("sess-abc")
        assert store.remove("sess-xyz") is False
        assert store.keys() == {"sess-abc"}

    def test_uses_default_path_when_none(self, tmp_path: Path, monkeypatch) -> None:
        # The conftest autouse fixture redirects _DEFAULT_PINNED_FILE into tmp;
        # constructing with no path must fall back to that module constant.
        store = PinnedSessionsStore()
        store.add("sess-abc")
        assert store.keys() == {"sess-abc"}


class TestPinnedStoreReadFailureDoesNotClobber:
    """AHQ-014: a read failure must skip the write, not clobber existing pins."""

    def test_unreadable_file_skips_add_preserves_existing_pins(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        store = PinnedSessionsStore(path)
        store.add("sess-abc")
        store.add("sess-def")
        with patch("pathlib.Path.read_text", side_effect=OSError("EIO")):
            store.add("sess-new")
        assert PinnedSessionsStore(path).keys() == {"sess-abc", "sess-def"}

    def test_corrupt_file_skips_remove_and_is_left_untouched(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        path.write_text("not json")
        store = PinnedSessionsStore(path)
        assert store.remove("sess-abc") is False
        assert path.read_text() == "not json"

    def test_read_or_raise_raises_on_corrupt_and_unreadable_but_not_on_missing(
        self, tmp_path: Path
    ) -> None:
        """Direct unit test of the strict reader. Note: ``remove()``'s own
        early-return (``if key not in keys: return False``) means a read
        failure can never make it clobber the file regardless of this
        method's behavior — a misread always yields "key not found", so
        ``test_corrupt_file_skips_remove_and_is_left_untouched`` above holds
        on old code too. ``_read_or_raise`` is what actually needs the
        RED/GREEN proof; ``add()``'s companion test above covers the one
        call site where the distinction is load-bearing."""
        missing = PinnedSessionsStore(tmp_path / "nope.json")
        assert missing._read_or_raise() == set()

        corrupt_path = tmp_path / "corrupt.json"
        corrupt_path.write_text("not json")
        with pytest.raises(OSError):
            PinnedSessionsStore(corrupt_path)._read_or_raise()

        unreadable = PinnedSessionsStore(tmp_path / "unreadable.json")
        with patch("pathlib.Path.read_text", side_effect=OSError("EIO")):
            with pytest.raises(OSError):
                unreadable._read_or_raise()


class TestReplace:
    def test_replace_swaps_old_for_new(self, tmp_path: Path) -> None:
        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add("codex:/w:%1")
        assert store.replace("codex:/w:%1", "uuid-7") is True
        assert store.keys() == {"uuid-7"}

    def test_replace_noop_when_old_absent(self, tmp_path: Path) -> None:
        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add("uuid-7")
        assert store.replace("codex:/w:%1", "uuid-9") is False
        assert store.keys() == {"uuid-7"}

    def test_replace_dedupes_when_new_already_present(self, tmp_path: Path) -> None:
        """A pre-upgrade dual-written pin holds BOTH forms; replace collapses
        it to the stable key alone."""
        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add("codex:/w:%1")
        store.add("uuid-7")
        assert store.replace("codex:/w:%1", "uuid-7") is True
        assert store.keys() == {"uuid-7"}


def _codex_agent(pane_id: str, thread_id: str | None, cwd: str = "/w"):
    from agent_hq.models import Agent, AgentState, AgentType

    return Agent(
        pane_id=pane_id,
        session_name="main",
        window_index=1,
        pane_index=0,
        agent_type=AgentType.CODEX,
        cwd=cwd,
        repo_root=cwd,
        repo_name="w",
        branch="main",
        raw_command="codex",
        state=AgentState.RUNNING,
        codex_thread_id=thread_id,
    )


class TestMigrateLegacyPins:
    """#205 Codex review: legacy-form pins are re-keyed on the captured
    stable id the first time the pinned agent is seen with one, so the
    ambiguous ``tool:cwd:pane_id`` entry can't outlive the session and
    transfer to an unrelated future occupant of the same pane."""

    def test_migrates_precapture_pin_and_survives_pane_renumber(
        self, tmp_path: Path
    ) -> None:
        """The combined old→new transition Codex flagged: pin at %1 before
        UUID capture, capture while still live, then a pane renumber to
        %99. The migrated UUID key must still match."""
        from agent_hq.pinned_store import migrate_legacy_pins

        store = PinnedSessionsStore(tmp_path / "p.json")
        pre_capture = _codex_agent("%1", None)
        store.add(pre_capture.stable_key)  # legacy form: codex:/w:%1
        assert store.keys() == {"codex:/w:%1"}

        captured = _codex_agent("%1", "uuid-7")
        migrate_legacy_pins(store, [captured])
        assert store.keys() == {"uuid-7"}

        renumbered = _codex_agent("%99", "uuid-7")
        assert renumbered.is_pinned_in(store.keys())

    def test_successor_does_not_inherit_migrated_pin(self, tmp_path: Path) -> None:
        """After migration, an unrelated session reusing the pane and cwd
        (with or without its own UUID) must not read as pinned."""
        from agent_hq.pinned_store import migrate_legacy_pins

        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add("codex:/w:%1")
        migrate_legacy_pins(store, [_codex_agent("%1", "uuid-7")])

        successor_captured = _codex_agent("%1", "uuid-other")
        successor_pre_capture = _codex_agent("%1", None)
        assert not successor_captured.is_pinned_in(store.keys())
        assert not successor_pre_capture.is_pinned_in(store.keys())

    def test_collapses_pre_upgrade_dual_written_pin(self, tmp_path: Path) -> None:
        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add("uuid-7")
        store.add("codex:/w:%1")
        from agent_hq.pinned_store import migrate_legacy_pins

        migrate_legacy_pins(store, [_codex_agent("%1", "uuid-7")])
        assert store.keys() == {"uuid-7"}

    def test_noop_without_captured_stable_id(self, tmp_path: Path) -> None:
        """A live pre-capture agent can't upgrade its own legacy pin — the
        stable key IS the legacy key, so nothing to migrate (and no write)."""
        from agent_hq.pinned_store import migrate_legacy_pins

        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add("codex:/w:%1")
        before = (tmp_path / "p.json").stat().st_mtime_ns
        migrate_legacy_pins(store, [_codex_agent("%1", None)])
        assert store.keys() == {"codex:/w:%1"}
        assert (tmp_path / "p.json").stat().st_mtime_ns == before

    def test_noop_when_store_empty(self, tmp_path: Path) -> None:
        from agent_hq.pinned_store import migrate_legacy_pins

        store = PinnedSessionsStore(tmp_path / "p.json")
        migrate_legacy_pins(store, [_codex_agent("%1", "uuid-7")])
        assert not (tmp_path / "p.json").exists()

    def test_migrates_claude_legacy_pin(self, tmp_path: Path) -> None:
        """The migration is harness-generic: a claude pin from before #205
        (or from a pre-sid moment) upgrades to the transcript sid too."""
        from agent_hq.models import Agent, AgentState, AgentType
        from agent_hq.pinned_store import migrate_legacy_pins

        agent = Agent(
            pane_id="%3",
            session_name="main",
            window_index=1,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/w",
            repo_root="/w",
            repo_name="w",
            branch="main",
            raw_command="claude",
            state=AgentState.RUNNING,
            claude_transcript_session_id="sid-1",
        )
        store = PinnedSessionsStore(tmp_path / "p.json")
        store.add(agent.legacy_stable_key)
        migrate_legacy_pins(store, [agent])
        assert store.keys() == {"sid-1"}
