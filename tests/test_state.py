"""Tests for state persistence module."""

import json
import threading
from unittest.mock import patch

import pytest

from agent_hq.state import (
    InactiveRecord,
    cleanup_inactive_entries,
    cleanup_stale_entries,
    load_access_times,
    load_inactive_state,
    record_access,
    save_access_times,
    save_inactive_state,
    set_inactive,
    toggle_inactive,
)


class TestLoadAccessTimes:
    """Tests for load_access_times function."""

    def test_returns_empty_dict_when_file_missing(self, tmp_path):
        """Should return empty dict when state file doesn't exist."""
        with patch("agent_hq.state.ACCESS_FILE", tmp_path / "missing.json"):
            result = load_access_times()

        assert result == {}

    def test_loads_valid_json(self, tmp_path):
        """Should load access times from valid JSON file."""
        state_file = tmp_path / "access.json"
        state_file.write_text('{"%5": 1701234567.0, "%8": 1701234890.0}')

        with patch("agent_hq.state.ACCESS_FILE", state_file):
            result = load_access_times()

        assert result == {"%5": 1701234567.0, "%8": 1701234890.0}

    def test_returns_empty_on_invalid_json(self, tmp_path):
        """Should return empty dict on invalid JSON."""
        state_file = tmp_path / "access.json"
        state_file.write_text("not valid json")

        with patch("agent_hq.state.ACCESS_FILE", state_file):
            result = load_access_times()

        assert result == {}

    def test_returns_empty_on_wrong_structure(self, tmp_path):
        """Should return empty dict if JSON is not a dict."""
        state_file = tmp_path / "access.json"
        state_file.write_text('["not", "a", "dict"]')

        with patch("agent_hq.state.ACCESS_FILE", state_file):
            result = load_access_times()

        assert result == {}


class TestSaveAccessTimes:
    """Tests for save_access_times function."""

    def test_creates_directory_and_file(self, tmp_path):
        """Should create state directory and file."""
        state_dir = tmp_path / "cache" / "agent-hq"
        state_file = state_dir / "access.json"

        with (
            patch("agent_hq.state.STATE_DIR", state_dir),
            patch("agent_hq.state.ACCESS_FILE", state_file),
        ):
            save_access_times({"%5": 1701234567.0})

        assert state_file.exists()
        assert json.loads(state_file.read_text()) == {"%5": 1701234567.0}

    def test_overwrites_existing_file(self, tmp_path):
        """Should overwrite existing state file."""
        state_file = tmp_path / "access.json"
        state_file.write_text('{"%old": 123}')

        with (
            patch("agent_hq.state.STATE_DIR", tmp_path),
            patch("agent_hq.state.ACCESS_FILE", state_file),
        ):
            save_access_times({"%new": 456.0})

        assert json.loads(state_file.read_text()) == {"%new": 456.0}


class TestRecordAccess:
    """Tests for record_access function."""

    def test_records_new_access(self, tmp_path):
        """Should record access time for new pane."""
        state_dir = tmp_path / "cache"
        state_file = state_dir / "access.json"

        with (
            patch("agent_hq.state.STATE_DIR", state_dir),
            patch("agent_hq.state.ACCESS_FILE", state_file),
            patch("agent_hq.state.time.time", return_value=1701234567.0),
        ):
            record_access("%5")

        data = json.loads(state_file.read_text())
        assert data["%5"] == 1701234567.0

    def test_updates_existing_access(self, tmp_path):
        """Should update access time for existing pane."""
        state_dir = tmp_path
        state_file = tmp_path / "access.json"
        state_file.write_text('{"%5": 1000.0}')

        with (
            patch("agent_hq.state.STATE_DIR", state_dir),
            patch("agent_hq.state.ACCESS_FILE", state_file),
            patch("agent_hq.state.time.time", return_value=2000.0),
        ):
            record_access("%5")

        data = json.loads(state_file.read_text())
        assert data["%5"] == 2000.0


class TestCleanupStaleEntries:
    """Tests for cleanup_stale_entries function."""

    def test_removes_stale_entries(self, tmp_path):
        """Should remove entries for panes that no longer exist."""
        state_file = tmp_path / "access.json"
        state_file.write_text('{"%5": 100.0, "%8": 200.0, "%10": 300.0}')

        with (
            patch("agent_hq.state.STATE_DIR", tmp_path),
            patch("agent_hq.state.ACCESS_FILE", state_file),
        ):
            cleanup_stale_entries({"%5", "%10"})  # %8 is stale

        data = json.loads(state_file.read_text())
        assert "%5" in data
        assert "%10" in data
        assert "%8" not in data

    def test_no_change_when_all_valid(self, tmp_path):
        """Should not modify file when all entries are valid."""
        state_file = tmp_path / "access.json"
        original = '{"%5": 100.0, "%8": 200.0}'
        state_file.write_text(original)

        with (
            patch("agent_hq.state.STATE_DIR", tmp_path),
            patch("agent_hq.state.ACCESS_FILE", state_file),
        ):
            cleanup_stale_entries({"%5", "%8"})

        # File should still contain same entries
        data = json.loads(state_file.read_text())
        assert len(data) == 2


# --- Inactive state tests ---


class TestLoadInactiveState:
    """Tests for load_inactive_state function."""

    def test_returns_empty_dict_when_file_missing(self, tmp_path):
        """Should return empty dict when inactive file doesn't exist."""
        with patch("agent_hq.state.INACTIVE_FILE", tmp_path / "missing.json"):
            result = load_inactive_state()

        assert result == {}

    def test_loads_valid_json(self, tmp_path):
        """Should load inactive records from valid JSON file."""
        state_file = tmp_path / "inactive.json"
        state_file.write_text(
            "{"
            '"%5": {"marked_at": 1701234567.0, "fingerprint": "abc123"},'
            '"%8": {"marked_at": 1701234890.0, "fingerprint": "def456"}'
            "}"
        )

        with patch("agent_hq.state.INACTIVE_FILE", state_file):
            result = load_inactive_state()

        assert len(result) == 2
        assert result["%5"].marked_at == 1701234567.0
        assert result["%5"].fingerprint == "abc123"
        assert result["%8"].marked_at == 1701234890.0
        assert result["%8"].fingerprint == "def456"

    def test_returns_empty_on_invalid_json(self, tmp_path):
        """Should return empty dict on invalid JSON."""
        state_file = tmp_path / "inactive.json"
        state_file.write_text("not valid json")

        with patch("agent_hq.state.INACTIVE_FILE", state_file):
            result = load_inactive_state()

        assert result == {}

    def test_skips_malformed_entries(self, tmp_path):
        """Should skip entries missing required fields."""
        state_file = tmp_path / "inactive.json"
        state_file.write_text(
            "{"
            '"%5": {"marked_at": 1000.0, "fingerprint": "abc"},'
            '"%6": {"marked_at": 2000.0},'
            '"%7": "not a dict"'
            "}"
        )

        with patch("agent_hq.state.INACTIVE_FILE", state_file):
            result = load_inactive_state()

        assert len(result) == 1
        assert "%5" in result


class TestSaveInactiveState:
    """Tests for save_inactive_state function."""

    def test_creates_directory_and_file(self, tmp_path):
        """Should create state directory and file."""
        state_dir = tmp_path / "cache" / "agent-hq"
        state_file = state_dir / "inactive.json"

        records = {"%5": InactiveRecord(marked_at=1701234567.0, fingerprint="abc123")}

        with (
            patch("agent_hq.state.STATE_DIR", state_dir),
            patch("agent_hq.state.INACTIVE_FILE", state_file),
        ):
            save_inactive_state(records)

        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["%5"]["marked_at"] == 1701234567.0
        assert data["%5"]["fingerprint"] == "abc123"


class TestToggleInactive:
    """Tests for toggle_inactive function."""

    def test_marks_active_as_inactive(self, tmp_path):
        """Should mark an active pane as inactive."""
        state_dir = tmp_path
        state_file = tmp_path / "inactive.json"
        # Start with empty file
        state_file.write_text("{}")

        with (
            patch("agent_hq.state.STATE_DIR", state_dir),
            patch("agent_hq.state.INACTIVE_FILE", state_file),
            patch("agent_hq.state.time.time", return_value=1701234567.0),
        ):
            result = toggle_inactive("%5", "abc123")

        assert result is True  # Now inactive
        data = json.loads(state_file.read_text())
        assert "%5" in data
        assert data["%5"]["fingerprint"] == "abc123"

    def test_marks_inactive_as_active(self, tmp_path):
        """Should mark an inactive pane as active."""
        state_dir = tmp_path
        state_file = tmp_path / "inactive.json"
        state_file.write_text('{ "%5": {"marked_at": 1000.0, "fingerprint": "abc"} }')

        with (
            patch("agent_hq.state.STATE_DIR", state_dir),
            patch("agent_hq.state.INACTIVE_FILE", state_file),
        ):
            result = toggle_inactive("%5", "newprint")

        assert result is False  # Now active
        data = json.loads(state_file.read_text())
        assert "%5" not in data


class TestSetInactive:
    """set_inactive (#397): an explicit set/clear, never a toggle, so two
    racing remote mutes can't cancel each other out."""

    @pytest.fixture
    def state_file(self, tmp_path):
        state_file = tmp_path / "inactive.json"
        with (
            patch("agent_hq.state.STATE_DIR", tmp_path),
            patch("agent_hq.state.INACTIVE_FILE", state_file),
        ):
            yield state_file

    def test_mute_then_mute_again_stays_muted(self, state_file):
        assert set_inactive("%5", "fp1", muted=True) is True
        first = json.loads(state_file.read_text())["%5"]
        assert set_inactive("%5", "fp2", muted=True) is True
        assert json.loads(state_file.read_text())["%5"] == first  # no-op keeps the record

    def test_unmute_clears_and_repeat_is_noop(self, state_file):
        state_file.write_text('{"%5": {"marked_at": 1.0, "fingerprint": "x"}}')
        assert set_inactive("%5", "fp", muted=False) is False
        assert "%5" not in json.loads(state_file.read_text())
        assert set_inactive("%5", "fp", muted=False) is False

    def test_racing_mutes_leave_the_pane_muted(self, state_file):
        threads = [
            threading.Thread(target=set_inactive, args=("%5", "fp"), kwargs={"muted": True})
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert "%5" in json.loads(state_file.read_text())

    def test_unreadable_state_returns_none_and_keeps_file(self, state_file):
        state_file.write_text("{not json")
        assert set_inactive("%5", "fp", muted=True) is None
        assert state_file.read_text() == "{not json"


class TestCleanupInactiveEntries:
    """Tests for cleanup_inactive_entries function."""

    def test_removes_stale_entries(self, tmp_path):
        """Should remove entries for panes that no longer exist."""
        state_file = tmp_path / "inactive.json"
        state_file.write_text(
            "{"
            '"%5": {"marked_at": 100.0, "fingerprint": "a"},'
            '"%8": {"marked_at": 200.0, "fingerprint": "b"},'
            '"%10": {"marked_at": 300.0, "fingerprint": "c"}'
            "}"
        )

        with (
            patch("agent_hq.state.STATE_DIR", tmp_path),
            patch("agent_hq.state.INACTIVE_FILE", state_file),
        ):
            cleanup_inactive_entries({"%5", "%10"})  # %8 is stale

        data = json.loads(state_file.read_text())
        assert "%5" in data
        assert "%10" in data
        assert "%8" not in data


# --- #164: file_lock + atomic writes for access.json/inactive.json ---


class TestAtomicWrites:
    def test_save_access_times_writes_via_atomic_rename_no_tmp_left(self, tmp_path):
        state_file = tmp_path / "access.json"
        with (
            patch("agent_hq.state.STATE_DIR", tmp_path),
            patch("agent_hq.state.ACCESS_FILE", state_file),
        ):
            save_access_times({"%5": 1.0})
        assert not state_file.with_suffix(".json.tmp").exists()
        assert json.loads(state_file.read_text()) == {"%5": 1.0}

    def test_save_inactive_state_writes_via_atomic_rename_no_tmp_left(self, tmp_path):
        state_file = tmp_path / "inactive.json"
        records = {"%5": InactiveRecord(marked_at=1.0, fingerprint="a")}
        with (
            patch("agent_hq.state.STATE_DIR", tmp_path),
            patch("agent_hq.state.INACTIVE_FILE", state_file),
        ):
            save_inactive_state(records)
        assert not state_file.with_suffix(".json.tmp").exists()
        assert json.loads(state_file.read_text())["%5"]["fingerprint"] == "a"


class TestToggleInactiveReadFailureDoesNotClobber:
    """AHQ-001: a torn/corrupt read must skip the toggle, not clobber."""

    def test_corrupt_file_skips_toggle_and_is_left_untouched(self, tmp_path):
        state_file = tmp_path / "inactive.json"
        state_file.write_text("not json")
        with (
            patch("agent_hq.state.STATE_DIR", tmp_path),
            patch("agent_hq.state.INACTIVE_FILE", state_file),
        ):
            result = toggle_inactive("%5", "fp")
        assert result is False
        assert state_file.read_text() == "not json"

    def test_load_inactive_state_or_raise_raises_on_corrupt_but_not_missing(self, tmp_path):
        """Direct unit test of the strict reader. Note:
        ``cleanup_inactive_entries`` computes both ``records`` and
        ``cleaned`` from the same (possibly swallowed-empty) read, so
        ``len(cleaned) != len(records)`` is never true when the read fails
        — it structurally can't clobber via a read failure regardless of
        this method's behavior. ``_load_inactive_state_or_raise`` is what
        actually needs the RED/GREEN proof; ``toggle_inactive``'s
        companion test above and the concurrency test below cover the
        call sites where the distinction is load-bearing."""
        from agent_hq.state import _load_inactive_state_or_raise

        with patch("agent_hq.state.INACTIVE_FILE", tmp_path / "missing.json"):
            assert _load_inactive_state_or_raise() == {}

        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("not json")
        with (
            patch("agent_hq.state.INACTIVE_FILE", corrupt),
            pytest.raises((ValueError, OSError)),
        ):
            _load_inactive_state_or_raise()

    def test_cleanup_inactive_entries_is_a_noop_when_file_unreadable(self, tmp_path):
        state_file = tmp_path / "inactive.json"
        state_file.write_text('{"%5": {"marked_at": 1.0, "fingerprint": "a"}}')
        real_open = open

        def _boom(path, *args, **kwargs):
            # Only the state file is "unreadable" — the lock sidecar
            # (opened by file_lock) must still open normally.
            if str(path) == str(state_file):
                raise OSError("EIO")
            return real_open(path, *args, **kwargs)

        with (
            patch("agent_hq.state.STATE_DIR", tmp_path),
            patch("agent_hq.state.INACTIVE_FILE", state_file),
            patch("builtins.open", side_effect=_boom),
        ):
            cleanup_inactive_entries(set())
        assert "%5" in json.loads(state_file.read_text())


class TestToggleInactiveSerializesConcurrentCallers:
    def test_concurrent_toggles_on_distinct_panes_both_land(self, tmp_path):
        """Blind check-then-toggle race (AHQ-001): without a lock around
        the whole read-modify-write, two concurrent toggles can both read
        the same pre-toggle state and one flip is silently lost. Exercise
        N concurrent toggles on N distinct panes and require all N to be
        durably recorded.

        The patch is applied once for the whole block, not per-thread —
        ``unittest.mock.patch`` isn't safe to enter/exit concurrently from
        multiple threads against the same target, which would race
        independently of whatever locking ``toggle_inactive`` does."""
        state_file = tmp_path / "inactive.json"
        state_file.write_text("{}")
        N = 20

        with (
            patch("agent_hq.state.STATE_DIR", tmp_path),
            patch("agent_hq.state.INACTIVE_FILE", state_file),
        ):
            threads = [
                threading.Thread(target=toggle_inactive, args=(f"%{i}", f"fp{i}")) for i in range(N)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        data = json.loads(state_file.read_text())
        assert set(data.keys()) == {f"%{i}" for i in range(N)}
