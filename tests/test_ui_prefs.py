"""Tests for UiPrefsStore — persisted UI preferences (#176)."""

import json
from unittest.mock import patch

import pytest

from agent_hq.ui_prefs import UiPrefsStore


@pytest.fixture
def store(tmp_path):
    return UiPrefsStore(path=tmp_path / "ui-prefs.json")


class TestTuiSortMode:
    def test_missing_file_returns_none(self, store):
        assert store.get_tui_sort_mode() is None

    def test_round_trip(self, store):
        store.set_tui_sort_mode("repo")
        assert store.get_tui_sort_mode() == "repo"

    @pytest.mark.parametrize("mode", ["priority", "phase", "repo"])
    def test_all_valid_modes_accepted(self, store, mode):
        store.set_tui_sort_mode(mode)
        assert store.get_tui_sort_mode() == mode

    def test_set_invalid_mode_raises(self, store):
        with pytest.raises(ValueError):
            store.set_tui_sort_mode("kanban")

    def test_invalid_stored_value_returns_none(self, store, tmp_path):
        (tmp_path / "ui-prefs.json").write_text(json.dumps({"tui_sort_mode": "bogus"}))
        assert store.get_tui_sort_mode() is None


class TestWebAgentsView:
    def test_missing_file_returns_none(self, store):
        assert store.get_web_agents_view() is None

    def test_round_trip(self, store):
        store.set_web_agents_view("list")
        assert store.get_web_agents_view() == "list"

    @pytest.mark.parametrize("view", ["phase", "list", "kanban"])
    def test_all_valid_views_accepted(self, store, view):
        store.set_web_agents_view(view)
        assert store.get_web_agents_view() == view

    def test_set_invalid_view_raises(self, store):
        with pytest.raises(ValueError):
            store.set_web_agents_view("repo")

    def test_invalid_stored_value_returns_none(self, store, tmp_path):
        (tmp_path / "ui-prefs.json").write_text(json.dumps({"web_agents_view": 42}))
        assert store.get_web_agents_view() is None


class TestWebIssuesMode:
    def test_missing_file_returns_none(self, store):
        assert store.get_web_issues_mode() is None

    @pytest.mark.parametrize("mode", ["queue", "browse", "triage"])
    def test_round_trip(self, store, mode):
        store.set_web_issues_mode(mode)
        assert store.get_web_issues_mode() == mode

    def test_set_invalid_mode_raises(self, store):
        with pytest.raises(ValueError):
            store.set_web_issues_mode("bogus")

    def test_invalid_stored_value_returns_none(self, store, tmp_path):
        (tmp_path / "ui-prefs.json").write_text(json.dumps({"web_issues_mode": "bogus"}))
        assert store.get_web_issues_mode() is None


class TestWebTheme:
    def test_missing_file_returns_none(self, store):
        assert store.get_web_theme() is None

    @pytest.mark.parametrize(
        "theme",
        [
            "default",
            "midnight",
            "warm",
            "paper",
            "terminal",
            "neon",
            "brutalist",
            "console",
            "bubblegum",
        ],
    )
    def test_all_valid_themes_accepted(self, store, theme):
        store.set_web_theme(theme)
        assert store.get_web_theme() == theme

    def test_registry_matches_expected_roster(self):
        # Pin the post-bake-off roster (#220): the original three, the five
        # expressive themes, and bubblegum (adopted from Codex's #221 take,
        # then reworked). Order matters — the settings menu lists them in
        # registry order.
        from agent_hq.ui_prefs import WEB_THEMES

        assert WEB_THEMES == (
            "default",
            "midnight",
            "warm",
            "paper",
            "terminal",
            "neon",
            "brutalist",
            "console",
            "bubblegum",
        )

    def test_set_invalid_theme_raises(self, store):
        with pytest.raises(ValueError):
            store.set_web_theme("solarized")

    def test_invalid_stored_value_returns_none(self, store, tmp_path):
        (tmp_path / "ui-prefs.json").write_text(json.dumps({"web_theme": ["warm"]}))
        assert store.get_web_theme() is None


class TestPreviewModes:
    """Sticky Live|History preview preference, one key per surface (#171
    follow-up): the TUI ``h`` toggle and the web toggle each remember
    their own mode."""

    def test_missing_file_returns_none(self, store):
        assert store.get_tui_preview_mode() is None
        assert store.get_web_preview_mode() is None

    @pytest.mark.parametrize("mode", ["live", "history"])
    def test_tui_round_trip(self, store, mode):
        store.set_tui_preview_mode(mode)
        assert store.get_tui_preview_mode() == mode

    @pytest.mark.parametrize("mode", ["live", "history"])
    def test_web_round_trip(self, store, mode):
        store.set_web_preview_mode(mode)
        assert store.get_web_preview_mode() == mode

    def test_surfaces_are_independent(self, store):
        store.set_tui_preview_mode("history")
        assert store.get_web_preview_mode() is None
        store.set_web_preview_mode("live")
        assert store.get_tui_preview_mode() == "history"

    def test_set_invalid_mode_raises(self, store):
        with pytest.raises(ValueError):
            store.set_tui_preview_mode("scrollback")
        with pytest.raises(ValueError):
            store.set_web_preview_mode("scrollback")

    def test_invalid_stored_value_returns_none(self, store, tmp_path):
        (tmp_path / "ui-prefs.json").write_text(json.dumps({"tui_preview_mode": 7}))
        assert store.get_tui_preview_mode() is None


class TestFileTolerance:
    def test_corrupt_file_reads_as_empty(self, store, tmp_path):
        (tmp_path / "ui-prefs.json").write_text("{not json")
        assert store.get_tui_sort_mode() is None
        assert store.get_web_agents_view() is None

    def test_non_dict_document_reads_as_empty(self, store, tmp_path):
        (tmp_path / "ui-prefs.json").write_text(json.dumps(["phase"]))
        assert store.get_tui_sort_mode() is None

    def test_set_over_corrupt_file_recovers(self, store, tmp_path):
        (tmp_path / "ui-prefs.json").write_text("{not json")
        store.set_tui_sort_mode("phase")
        assert store.get_tui_sort_mode() == "phase"

    def test_keys_are_independent(self, store):
        store.set_tui_sort_mode("repo")
        store.set_web_agents_view("list")
        assert store.get_tui_sort_mode() == "repo"
        assert store.get_web_agents_view() == "list"

    def test_unknown_keys_preserved_on_write(self, store, tmp_path):
        prefs_file = tmp_path / "ui-prefs.json"
        prefs_file.write_text(json.dumps({"future_pref": "keep-me"}))
        store.set_tui_sort_mode("priority")
        data = json.loads(prefs_file.read_text())
        assert data["future_pref"] == "keep-me"
        assert data["tui_sort_mode"] == "priority"

    def test_default_path_is_config_home(self):
        from agent_hq import ui_prefs

        assert UiPrefsStore()._path == ui_prefs._DEFAULT_PREFS_FILE


class TestReadFailureDoesNotClobber:
    """A write must not erase prefs it merely failed to READ (AHQ-014 class).

    ``_set`` is a read-modify-write with no early return, so a read that
    degrades to ``{}`` writes a document containing only the key being set —
    silently dropping every other pref. #192 fixed this class for
    pinned/snapshot/overrides/inactive; ``UiPrefsStore`` was never covered.

    Unreadable and corrupt are deliberately NOT treated alike here (a
    divergence from #192's stores): an unreadable file (EIO/EACCES) probably
    holds good bytes we just can't see, so overwriting destroys real data —
    skip the write. A corrupt file has nothing preservable, and refusing to
    write would strand the user with a broken file and no UI path to fix it —
    so the write still recovers it, per ``test_set_over_corrupt_file_recovers``.
    """

    def test_unreadable_file_skips_write_and_preserves_other_prefs(self, store, tmp_path):
        store.set_tui_sort_mode("phase")
        store.set_web_agents_view("list")
        with patch("pathlib.Path.read_text", side_effect=OSError("EIO")):
            store.set_web_theme("midnight")
        fresh = UiPrefsStore(path=tmp_path / "ui-prefs.json")
        assert fresh.get_tui_sort_mode() == "phase"
        assert fresh.get_web_agents_view() == "list"

    def test_strict_read_raises_only_when_present_but_unreadable(self, tmp_path):
        assert UiPrefsStore(path=tmp_path / "nope.json")._read_for_write() == {}

        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{not json")
        assert UiPrefsStore(path=corrupt)._read_for_write() == {}

        good = tmp_path / "good.json"
        good.write_text(json.dumps({"tui_sort_mode": "repo"}))
        with patch("pathlib.Path.read_text", side_effect=OSError("EIO")):
            with pytest.raises(OSError):
                UiPrefsStore(path=good)._read_for_write()


def test_web_issues_mode_accepts_hygiene(tmp_path):
    store = UiPrefsStore(tmp_path / "ui-prefs.json")
    store.set_web_issues_mode("hygiene")
    assert store.get_web_issues_mode() == "hygiene"
