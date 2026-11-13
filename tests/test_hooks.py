"""Tests for hooks-based state detection module."""

import json
import os
import shutil
import threading
import time
from io import StringIO
from unittest.mock import patch

import pytest

from agent_hq.hooks import (
    EVENT_STATE_MAP,
    STALE_THRESHOLD_SECONDS,
    handle_hook_event,
    read_hook_state,
)
from agent_hq.models import Agent, AgentState, AgentType


class TestReadHookState:
    """``read_hook_state`` returns ``(state, validity)`` and session-scopes
    via the ``current_session_id`` keyword argument.

    Validity codes: missing | tombstone | legacy | mismatch |
    transient_unknown | stale | valid. Only ``valid`` produces a non-
    None state. ``stale`` is state-only (phase has no staleness rule)."""

    def _write(self, tmp_path, payload):
        (tmp_path / "5.json").write_text(json.dumps(payload))

    def test_valid_returns_state(self, tmp_path):
        self._write(
            tmp_path,
            {
                "revision": 1,
                "state": "running",
                "session_id": "abc",
                "timestamp": time.time(),
            },
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5", current_session_id="abc")
        assert state == AgentState.RUNNING
        assert validity == "valid"

    def test_returns_idle_state_when_valid(self, tmp_path):
        self._write(
            tmp_path,
            {"revision": 1, "state": "idle", "session_id": "abc", "timestamp": time.time()},
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5", current_session_id="abc")
        assert state == AgentState.IDLE
        assert validity == "valid"

    def test_returns_waiting_state_when_valid(self, tmp_path):
        self._write(
            tmp_path,
            {"revision": 1, "state": "waiting", "session_id": "abc", "timestamp": time.time()},
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5", current_session_id="abc")
        assert state == AgentState.WAITING
        assert validity == "valid"

    def test_missing_file(self, tmp_path):
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5")
        assert state is None
        assert validity == "missing"

    def test_malformed_json_reads_as_missing(self, tmp_path):
        (tmp_path / "5.json").write_text("not valid json")
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5")
        assert state is None
        # Corrupt file behaves like missing — caller falls back.
        assert validity == "missing"

    def test_session_id_missing_key_is_legacy(self, tmp_path):
        """Pre-#65 entries (no session_id key) classify as ``legacy``;
        daemon prune writes tombstones for them."""
        self._write(tmp_path, {"revision": 1, "state": "running", "timestamp": time.time()})
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5", current_session_id="abc")
        assert state is None
        assert validity == "legacy"

    def test_session_id_positive_mismatch(self, tmp_path):
        self._write(
            tmp_path,
            {
                "revision": 1,
                "state": "running",
                "session_id": "old",
                "timestamp": time.time(),
            },
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5", current_session_id="new")
        assert state is None
        assert validity == "mismatch"

    def test_session_id_both_none_is_valid(self, tmp_path):
        """Non-Claude agents: stored=None, current=None → valid."""
        self._write(
            tmp_path,
            {
                "revision": 1,
                "state": "idle",
                "session_id": None,
                "timestamp": time.time(),
            },
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5", current_session_id=None)
        assert state == AgentState.IDLE
        assert validity == "valid"

    def test_session_id_exactly_one_none_is_transient_unknown(self, tmp_path):
        """Don't apply, don't prune — the next successful read resolves it."""
        self._write(
            tmp_path,
            {
                "revision": 1,
                "state": "running",
                "session_id": "abc",
                "timestamp": time.time(),
            },
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5", current_session_id=None)
        assert state is None
        assert validity == "transient_unknown"

    def test_empty_string_session_id_normalizes_to_none(self, tmp_path):
        """SessionStart can emit ``""`` before a session id is assigned.
        Normalize to None so the matrix doesn't fire a false mismatch."""
        self._write(
            tmp_path,
            {"revision": 1, "state": "running", "session_id": "", "timestamp": time.time()},
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5", current_session_id=None)
        # stored="" → None; current=None; both-None → valid
        assert state == AgentState.RUNNING
        assert validity == "valid"

    def test_tombstone(self, tmp_path):
        """SessionEnd writes a tombstone; reads return ``(None, "tombstone")``."""
        self._write(
            tmp_path,
            {
                "revision": 5,
                "tombstone": True,
                "session_id": None,
                "timestamp": time.time(),
            },
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5", current_session_id="abc")
        assert state is None
        assert validity == "tombstone"

    def test_handles_pane_id_without_percent(self, tmp_path):
        self._write(
            tmp_path,
            {"revision": 1, "state": "running", "session_id": None, "timestamp": time.time()},
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("5", current_session_id=None)
        assert state == AgentState.RUNNING
        assert validity == "valid"

    def test_handles_pane_id_with_percent(self, tmp_path):
        (tmp_path / "12.json").write_text(
            json.dumps(
                {"revision": 1, "state": "idle", "session_id": None, "timestamp": time.time()}
            )
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%12", current_session_id=None)
        assert state == AgentState.IDLE
        assert validity == "valid"

    def test_stale_file_returns_stale(self, tmp_path):
        """5-minute staleness cap applies to state only. Phase is sticky."""
        stale_ts = time.time() - STALE_THRESHOLD_SECONDS - 60
        self._write(
            tmp_path,
            {"revision": 1, "state": "running", "session_id": "abc", "timestamp": stale_ts},
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5", current_session_id="abc")
        assert state is None
        assert validity == "stale"

    def test_file_without_timestamp_is_stale(self, tmp_path):
        self._write(tmp_path, {"revision": 1, "state": "idle", "session_id": "abc"})
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5", current_session_id="abc")
        assert state is None
        assert validity == "stale"

    def test_file_with_non_numeric_timestamp_is_stale(self, tmp_path):
        self._write(
            tmp_path,
            {"revision": 1, "state": "idle", "session_id": "abc", "timestamp": "yesterday"},
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state, validity = read_hook_state("%5", current_session_id="abc")
        assert state is None
        assert validity == "stale"


class TestHandleHookEvent:
    """Tests for handle_hook_event function (hook entry point)."""

    def _run_handler(self, stdin_data, env_vars, state_dir):
        """Helper to run handle_hook_event with mocked stdin/env/dir."""
        with (
            patch("sys.stdin", StringIO(stdin_data)),
            patch.dict("os.environ", env_vars, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
            patch("sys.stdout", new_callable=StringIO) as mock_stdout,
        ):
            handle_hook_event()
        return mock_stdout.getvalue()

    def test_unsafe_runtime_root_never_breaks_the_hook(self, tmp_path, monkeypatch):
        """The hook protocol must not raise: a hostile /tmp/agent-hq still
        yields ``{}`` and exit 0, and nothing is written there (#263 review)."""
        from agent_hq import runtime_dir

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        root = tmp_path / "agent-hq"
        root.symlink_to(elsewhere)
        monkeypatch.setattr(runtime_dir, "RUNTIME_ROOT", root)
        stdin_data = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"})
        output = self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, root / "states")
        assert output.strip() == "{}"
        assert list(elsewhere.iterdir()) == []

    def test_creates_state_file_for_pre_tool_use(self, tmp_path):
        """Should write RUNNING state file for PreToolUse event."""
        stdin_data = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"})
        output = self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        state_file = tmp_path / "5.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["state"] == "running"
        assert data["hook_event"] == "PreToolUse"
        assert output.strip() == "{}"

    def test_creates_state_file_for_stop(self, tmp_path):
        """Should write IDLE state file for Stop event."""
        stdin_data = json.dumps({"hook_event_name": "Stop", "session_id": "s1"})
        self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        state_file = tmp_path / "5.json"
        data = json.loads(state_file.read_text())
        assert data["state"] == "idle"

    def test_creates_state_file_for_permission_request(self, tmp_path):
        """Should write WAITING state file for PermissionRequest event."""
        stdin_data = json.dumps({"hook_event_name": "PermissionRequest", "session_id": "s1"})
        self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        state_file = tmp_path / "5.json"
        data = json.loads(state_file.read_text())
        assert data["state"] == "waiting"

    def test_notification_is_ignored(self, tmp_path):
        """Should not create state file for Notification event.

        Notifications fire for informational messages (e.g., "agent was stopped")
        that don't require user input, causing false WAITING detection.
        """
        stdin_data = json.dumps({"hook_event_name": "Notification", "session_id": "s1"})
        self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        assert not (tmp_path / "5.json").exists()

    def test_creates_state_file_for_user_prompt_submit(self, tmp_path):
        """Should write RUNNING state file for UserPromptSubmit event."""
        stdin_data = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s1"})
        self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        state_file = tmp_path / "5.json"
        data = json.loads(state_file.read_text())
        assert data["state"] == "running"

    def test_creates_state_file_for_subagent_start(self, tmp_path):
        """Should write RUNNING state file for SubagentStart event."""
        stdin_data = json.dumps({"hook_event_name": "SubagentStart", "session_id": "s1"})
        self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        state_file = tmp_path / "5.json"
        data = json.loads(state_file.read_text())
        assert data["state"] == "running"

    def test_session_end_writes_tombstone(self, tmp_path):
        """SessionEnd writes a tombstone (revision+1, tombstone:true)
        instead of unlinking. Preserves revision monotonicity for the
        brief window before the next session's first hook write."""
        state_file = tmp_path / "5.json"
        state_file.write_text(json.dumps({"revision": 7, "state": "running"}))

        stdin_data = json.dumps({"hook_event_name": "SessionEnd", "session_id": "s1"})
        self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["tombstone"] is True
        assert data["session_id"] is None
        assert data["revision"] == 8

    def test_session_end_no_error_if_file_missing(self, tmp_path):
        """Should not error when SessionEnd fires but no state file exists."""
        stdin_data = json.dumps({"hook_event_name": "SessionEnd", "session_id": "s1"})
        output = self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)
        assert output.strip() == "{}"

    def test_no_tmux_pane_exits_silently(self, tmp_path):
        """Should output {} and exit when TMUX_PANE is not set."""
        stdin_data = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"})
        env = {k: v for k, v in []}  # empty extra env

        with (
            patch("sys.stdin", StringIO(stdin_data)),
            patch.dict("os.environ", env, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("sys.stdout", new_callable=StringIO) as mock_stdout,
        ):
            # Remove TMUX_PANE if it exists
            import os

            old_val = os.environ.pop("TMUX_PANE", None)
            try:
                handle_hook_event()
            finally:
                if old_val is not None:
                    os.environ["TMUX_PANE"] = old_val

        # No state files should be created
        assert list(tmp_path.glob("*.json")) == []
        assert mock_stdout.getvalue().strip() == "{}"

    def test_unknown_event_exits_silently(self, tmp_path):
        """Should output {} and not create file for unknown event names."""
        stdin_data = json.dumps({"hook_event_name": "UnknownEvent", "session_id": "s1"})
        output = self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        assert list(tmp_path.glob("*.json")) == []
        assert output.strip() == "{}"

    def test_creates_state_dir_if_needed(self, tmp_path):
        """Should create the state directory if it doesn't exist."""
        state_dir = tmp_path / "nested" / "states"
        stdin_data = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"})
        self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, state_dir)

        assert (state_dir / "5.json").exists()

    def test_outputs_valid_json_response(self, tmp_path):
        """Should always output {} as valid hook response."""
        stdin_data = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"})
        output = self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        parsed = json.loads(output.strip())
        assert parsed == {}

    def test_state_file_includes_session_id(self, tmp_path):
        """Should include session_id from hook input in state file."""
        stdin_data = json.dumps({"hook_event_name": "PreToolUse", "session_id": "my-session"})
        self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        data = json.loads((tmp_path / "5.json").read_text())
        assert data["session_id"] == "my-session"

    def test_state_file_includes_timestamp(self, tmp_path):
        """Should include a timestamp in state file."""
        stdin_data = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"})
        self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        data = json.loads((tmp_path / "5.json").read_text())
        assert "timestamp" in data
        assert isinstance(data["timestamp"], float)

    def test_strips_percent_from_pane_id(self, tmp_path):
        """Should use numeric pane ID for filename (strip %)."""
        stdin_data = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"})
        self._run_handler(stdin_data, {"TMUX_PANE": "%12"}, tmp_path)

        assert (tmp_path / "12.json").exists()
        assert not (tmp_path / "%12.json").exists()

    def test_sequential_events_bump_revision(self, tmp_path):
        """Each hook event increments the on-disk ``revision`` by 1."""
        stdin = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"})
        for expected in (1, 2, 3):
            self._run_handler(stdin, {"TMUX_PANE": "%5"}, tmp_path)
            assert json.loads((tmp_path / "5.json").read_text())["revision"] == expected

    def test_concurrent_hook_events_serialize_via_flock(self, tmp_path):
        """Two ``handle_hook_event`` calls racing on the same pane must
        serialize through the per-pane flock: the second reads the first's
        revision and bumps to 2. A broken lock would let both read 0 and
        write 1 (lost increment) — final revision would be 1, not 2."""

        class _ReplayStdin:
            """A stdin stand-in whose ``read()`` always returns the full
            payload — both threads need to parse the same event."""

            def __init__(self, data: str) -> None:
                self._data = data

            def read(self, *_args, **_kwargs) -> str:
                return self._data

        stdin_obj = _ReplayStdin(json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"}))
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            handle_hook_event()

        with (
            patch("sys.stdin", stdin_obj),
            patch.dict(os.environ, {"TMUX_PANE": "%5"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("sys.stdout", new_callable=StringIO),
        ):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

        assert not t1.is_alive() and not t2.is_alive()
        assert json.loads((tmp_path / "5.json").read_text())["revision"] == 2

    def test_write_leaves_no_temp_files(self, tmp_path):
        """Atomic-rename writer must clean up its temp file on success.

        The flock sidecar (``5.json.lock``) is expected — that's the
        per-pane lock file that persists across writes."""
        stdin_data = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"})
        self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        leftovers = sorted(
            p.name for p in tmp_path.iterdir() if p.name not in {"5.json", "5.json.lock"}
        )
        assert leftovers == [], f"unexpected leftovers in state dir: {leftovers}"

    def test_write_cleans_up_temp_on_failure(self, tmp_path):
        """If the rename step fails, the temp file must still be cleaned up."""
        stdin_data = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"})

        original_replace = os.replace

        def boom(src, dst):
            raise OSError("simulated rename failure")

        with patch("agent_hq.hooks.os.replace", side_effect=boom):
            # The hook handler swallows OSError and logs to stderr — we just
            # need to confirm no .tmp file remains.
            self._run_handler(stdin_data, {"TMUX_PANE": "%5"}, tmp_path)

        # Final state file was not written; no .tmp leftovers either.
        # The flock sidecar is allowed to persist.
        assert not (tmp_path / "5.json").exists()
        leftovers = sorted(p.name for p in tmp_path.iterdir() if p.name != "5.json.lock")
        assert leftovers == [], f"unexpected leftovers in state dir: {leftovers}"

        # Sanity: os.replace is still callable normally after the patch exits.
        assert original_replace is os.replace

    def test_destination_unchanged_during_inner_write(self, tmp_path):
        """Atomic-rename invariant: the destination file is only updated
        via ``os.replace``, never written to in place.

        Pre-seed the destination with content A. Pause the writer inside
        ``json.dump`` (which writes to the tempfile, not the destination).
        While paused, the reader observes the destination — it MUST still
        be content A. After unpausing, the destination flips to content B.

        Determinism: this is the real regression guard. A revert to
        ``state_file.write_text(json.dumps(...))`` would never call
        ``json.dump`` (it uses ``json.dumps`` outside the open), so the
        ``pause.wait`` below would time out and the assertion would fail
        with a clear message. A free-running concurrent-writer test
        relies on a microsecond truncate window that the GIL hides on
        modern hardware and is unreliable.
        """
        import threading
        from io import StringIO

        state_file = tmp_path / "5.json"
        seed = {
            "state": "idle",
            "session_id": "seed",
            "hook_event": "seed",
            "timestamp": time.time(),
        }
        state_file.write_text(json.dumps(seed))

        pause_started = threading.Event()
        allow_finish = threading.Event()
        real_json_dump = json.dump

        def paused_dump(obj, fp):
            # Signal we've entered the inner write, then block. The
            # destination file must remain untouched while we hold here.
            pause_started.set()
            assert allow_finish.wait(timeout=5), "test forgot to unpause writer"
            real_json_dump(obj, fp)

        writer_exc: list[BaseException] = []

        def writer():
            try:
                stdin_data = json.dumps(
                    {"hook_event_name": "PreToolUse", "session_id": "after-seed"}
                )
                with (
                    patch("sys.stdin", StringIO(stdin_data)),
                    patch.dict("os.environ", {"TMUX_PANE": "%5"}, clear=False),
                    patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
                    patch("sys.stdout", new_callable=StringIO),
                    patch("agent_hq.hooks.json.dump", side_effect=paused_dump),
                ):
                    handle_hook_event()
            except BaseException as exc:
                writer_exc.append(exc)

        t = threading.Thread(target=writer)
        t.start()
        try:
            assert pause_started.wait(timeout=5), (
                "writer never reached json.dump — implementation may have "
                "regressed to a non-atomic write_text path"
            )

            # Writer is paused inside the inner write. Destination must
            # still resolve to the seed.
            text = state_file.read_text()
            assert json.loads(text) == seed, (
                f"destination was modified mid-write (atomic-rename broken): {text!r}"
            )

            allow_finish.set()
            t.join(timeout=5)
            assert not t.is_alive(), "writer never finished after unpause"
            assert not writer_exc, f"writer raised: {writer_exc[0]!r}"

            # Destination now reflects the new write.
            final = json.loads(state_file.read_text())
            assert final["hook_event"] == "PreToolUse"
            assert final["session_id"] == "after-seed"
        finally:
            allow_finish.set()
            t.join(timeout=5)

        # And no temp files left behind. The flock sidecar is allowed.
        leftovers = sorted(
            p.name for p in tmp_path.iterdir() if p.name not in {"5.json", "5.json.lock"}
        )
        assert leftovers == [], f"unexpected leftovers: {leftovers}"


class TestEventStateMap:
    """Tests for EVENT_STATE_MAP constant."""

    def test_all_expected_events_mapped(self):
        """Should have mappings for all expected hook events."""
        expected = {
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "SubagentStart",
            "PermissionRequest",
            "Stop",
            "SessionEnd",
        }
        assert set(EVENT_STATE_MAP.keys()) == expected

    def test_running_events(self):
        """Running events should map to RUNNING state."""
        for event in ["UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStart"]:
            assert EVENT_STATE_MAP[event] == AgentState.RUNNING

    def test_waiting_events(self):
        """Waiting events should map to WAITING state."""
        assert EVENT_STATE_MAP["PermissionRequest"] == AgentState.WAITING

    def test_idle_events(self):
        """Stop should map to IDLE state."""
        assert EVENT_STATE_MAP["Stop"] == AgentState.IDLE

    def test_session_end_is_none(self):
        """SessionEnd should map to None (signal to delete file)."""
        assert EVENT_STATE_MAP["SessionEnd"] is None


class TestLatestSummaryCapture:
    """Stop hook extracts the last assistant text from the transcript."""

    def test_stop_extracts_last_assistant_message(self, tmp_path):
        import json
        import os
        from unittest.mock import patch

        from agent_hq.hooks import handle_hook_event

        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps({"type": "user", "message": {"content": "hi"}}),
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {"content": [{"type": "text", "text": "First reply"}]},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {"content": [{"type": "text", "text": "Final answer."}]},
                        }
                    ),
                ]
            )
        )

        hook_input = {
            "hook_event_name": "Stop",
            "session_id": "abc",
            "transcript_path": str(transcript),
        }

        # Isolate the state dir so the test doesn't collide with real /tmp state
        state_dir = tmp_path / "states"
        with (
            patch("sys.stdin") as stdin_mock,
            patch.dict(os.environ, {"TMUX_PANE": "%42"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
        ):
            stdin_mock.read.return_value = json.dumps(hook_input)
            handle_hook_event()

        state_file = state_dir / "42.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["state"] == "idle"
        assert data["latest_summary"] == "Final answer."

    def test_stop_without_transcript_path_stores_none(self, tmp_path):
        import json
        import os
        from unittest.mock import patch

        from agent_hq.hooks import handle_hook_event

        hook_input = {"hook_event_name": "Stop", "session_id": "abc"}
        state_dir = tmp_path / "states"
        with (
            patch("sys.stdin") as stdin_mock,
            patch.dict(os.environ, {"TMUX_PANE": "%42"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
        ):
            stdin_mock.read.return_value = json.dumps(hook_input)
            handle_hook_event()

        data = json.loads((state_dir / "42.json").read_text())
        assert data.get("latest_summary") is None


class TestReadHookSummary:
    """read_hook_summary returns the latest_summary from the state file, or None."""

    def test_returns_summary_when_present(self, tmp_path):
        import json
        import time as _time
        from unittest.mock import patch

        from agent_hq.hooks import read_hook_summary

        state_dir = tmp_path / "states"
        state_dir.mkdir()
        (state_dir / "42.json").write_text(
            json.dumps(
                {
                    "state": "idle",
                    "session_id": "abc",
                    "hook_event": "Stop",
                    "timestamp": _time.time(),
                    "latest_summary": "Did the thing",
                }
            )
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir):
            assert read_hook_summary("%42") == "Did the thing"

    def test_returns_none_when_missing_file(self, tmp_path):
        from unittest.mock import patch

        from agent_hq.hooks import read_hook_summary

        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path / "states"):
            assert read_hook_summary("%42") is None

    def test_returns_none_when_summary_field_missing(self, tmp_path):
        import json
        import time as _time
        from unittest.mock import patch

        from agent_hq.hooks import read_hook_summary

        state_dir = tmp_path / "states"
        state_dir.mkdir()
        (state_dir / "42.json").write_text(json.dumps({"state": "idle", "timestamp": _time.time()}))
        with patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir):
            assert read_hook_summary("%42") is None


class TestHooksIntegration:
    """Hook events and terminal content never override native live status."""

    def test_get_agent_state_ignores_hook_state(self, tmp_path):
        """get_agent_state should return hook state when file exists.

        Session matches (non-Claude None / None) → ``valid`` → state."""
        from agent_hq.enrichment import get_agent_state

        state_file = tmp_path / "5.json"
        state_file.write_text(
            json.dumps(
                {
                    "revision": 1,
                    "state": "waiting",
                    "session_id": None,
                    "timestamp": time.time(),
                }
            )
        )

        with (
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("agent_hq.enrichment.get_pane_content") as mock_content,
        ):
            mock_content.return_value = "> \n-- INSERT --"
            result = get_agent_state("%5", AgentType.CLAUDE)

        assert result == AgentState.UNKNOWN
        mock_content.assert_not_called()

    def test_get_agent_state_does_not_fall_back_to_regex(self, tmp_path):
        """get_agent_state should use regex when no hook file exists."""
        from agent_hq.enrichment import get_agent_state

        with (
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("agent_hq.enrichment.get_pane_content") as mock_content,
        ):
            mock_content.return_value = "> \n-- INSERT --"
            result = get_agent_state("%5", AgentType.CLAUDE)

        assert result == AgentState.UNKNOWN
        mock_content.assert_not_called()

    def test_get_agent_state_non_claude_ignores_hooks(self, tmp_path):
        """Non-Claude agents should still use their own detection (no hooks)."""
        from agent_hq.enrichment import get_agent_state

        state_file = tmp_path / "5.json"
        state_file.write_text(json.dumps({"revision": 1, "state": "running"}))

        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            result = get_agent_state("%5", AgentType.AIDER)

        assert result == AgentState.UNKNOWN


class TestReadHookRevision:
    """``read_hook_revision`` returns ``(validity, revision)``.

    Pure read. Session-scoped. The hook no longer writes phase — this
    only feeds ``inputs_observed.hook_state_revision`` so ``replaces()``
    can order state-driven snapshot updates.
    """

    def test_valid_returns_validity_and_revision(self, tmp_path):
        from agent_hq.hooks import read_hook_revision

        (tmp_path / "5.json").write_text(
            json.dumps(
                {
                    "revision": 4,
                    "session_id": "abc",
                    "state": "idle",
                    "timestamp": time.time(),
                }
            )
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            validity, revision = read_hook_revision("%5", current_session_id="abc")
        assert validity == "valid"
        assert revision == 4

    def test_missing_returns_missing_with_revision_zero(self, tmp_path):
        from agent_hq.hooks import read_hook_revision

        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            validity, revision = read_hook_revision("%99")
        assert validity == "missing"
        assert revision == 0

    def test_session_mismatch_keeps_revision(self, tmp_path):
        from agent_hq.hooks import read_hook_revision

        (tmp_path / "5.json").write_text(
            json.dumps(
                {
                    "revision": 7,
                    "session_id": "stored",
                    "state": "idle",
                    "timestamp": time.time(),
                }
            )
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            validity, revision = read_hook_revision("%5", current_session_id="other")
        assert validity == "mismatch"
        assert revision == 7

    def test_legacy_file_without_session_key(self, tmp_path):
        from agent_hq.hooks import read_hook_revision

        (tmp_path / "5.json").write_text(
            json.dumps({"revision": 2, "state": "idle", "timestamp": time.time()})
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            validity, revision = read_hook_revision("%5", current_session_id="abc")
        assert validity == "legacy"
        assert revision == 2

    def test_tombstone_returns_tombstone_with_revision(self, tmp_path):
        from agent_hq.hooks import read_hook_revision

        (tmp_path / "5.json").write_text(
            json.dumps(
                {
                    "revision": 11,
                    "tombstone": True,
                    "session_id": None,
                    "timestamp": time.time(),
                }
            )
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            validity, revision = read_hook_revision("%5")
        assert validity == "tombstone"
        assert revision == 11

    def test_pre_v5_file_without_revision_reads_as_revision_zero(self, tmp_path):
        """A missing ``revision`` key is revision 0 — NOT ``"legacy"``
        (that's reserved for a missing ``session_id`` key)."""
        from agent_hq.hooks import read_hook_revision

        (tmp_path / "5.json").write_text(
            json.dumps({"session_id": "abc", "state": "idle", "timestamp": time.time()})
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            validity, revision = read_hook_revision("%5", current_session_id="abc")
        assert validity == "valid"
        assert revision == 0


def _write_transcript(path, *messages: str) -> None:
    """Write a JSONL transcript whose assistant turns are the given strings."""
    lines = []
    for text in messages:
        lines.append(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
            )
        )
    path.write_text("\n".join(lines))


class TestSessionStartInstruction:
    """SessionStart still injects PHASE_INSTRUCTION when a daemon is up.

    Phase derivation moved to the resolver, but Claude must still be
    asked to emit ``<phase>`` tags — the resolver reads them from the
    transcript instead of the hook.
    """

    def test_session_start_with_tmux_injects_instruction(self, tmp_path):
        with (
            patch(
                "sys.stdin",
                StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "x"})),
            ),
            patch.dict(os.environ, {"TMUX_PANE": "%5"}, clear=False),
            patch("sys.stdout", new_callable=StringIO) as out,
        ):
            handle_hook_event()

        payload = json.loads(out.getvalue())
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "<phase>" in ctx
        for phase in (
            "adhoc",
            "design",
            "implement",
            "qa",
            "review",
            "ready",
            "done",
        ):
            assert phase in ctx
        # #108: the collapsed-away phases must no longer be advertised.
        assert "brainstorm" not in ctx
        first_paragraph = ctx.split("\n\n")[0]
        assert "merged" not in first_paragraph

    def test_phase_instruction_directs_tag_on_every_assistant_message(self):
        from agent_hq.hooks import PHASE_INSTRUCTION

        text = PHASE_INSTRUCTION.lower()
        assert "every assistant message" in text
        assert "tool" in text

    def test_phase_instruction_design_covers_ideation_and_planning(self):
        """#108: the single `design` phase must describe BOTH ideation
        (requirements / tradeoffs) AND concrete plan-writing (steps), so a
        reader knows it absorbed the former brainstorm + plan phases."""
        from agent_hq.hooks import PHASE_INSTRUCTION

        text = PHASE_INSTRUCTION.lower()
        assert "design:" in text
        # ideation side
        assert "requirement" in text or "tradeoff" in text
        # plan-writing side
        assert "plan" in text and "step" in text

    def test_phase_instruction_ready_requires_committed_work(self):
        """#110 (a): uncommitted-but-verified work is NOT ready."""
        from agent_hq.hooks import PHASE_INSTRUCTION

        text = PHASE_INSTRUCTION.lower()
        assert "committed" in text
        assert "uncommitted" in text

    def test_phase_instruction_ready_requires_resolved_review(self):
        """#110 (b): review must have run or been explicitly user-skipped,
        and 'not yet decided / didn't happen' does NOT count as skipped."""
        from agent_hq.hooks import PHASE_INSTRUCTION

        text = PHASE_INSTRUCTION.lower()
        assert "explicitly skipped" in text
        assert "not count as skipped" in text

    def test_phase_instruction_qa_is_resting_state(self):
        """#110: qa is the holding/resting state after tests pass while the
        review-or-PR decision is pending — not just 'actively running'."""
        from agent_hq.hooks import PHASE_INSTRUCTION

        text = PHASE_INSTRUCTION.lower()
        assert "holding state" in text or "resting" in text
        assert "do not jump to ready" in text

    def test_phase_instruction_verification_bar_includes_manual(self):
        """#110 (c): the verification bar is tests + lint, plus any applicable
        manual/functional check; a user-declined step counts as satisfied."""
        from agent_hq.hooks import PHASE_INSTRUCTION

        text = PHASE_INSTRUCTION.lower()
        assert "lint" in text
        assert "manual" in text
        assert "declined" in text

    def test_phase_instruction_describes_done_for_non_pr_completion(self):
        from agent_hq.hooks import PHASE_INSTRUCTION

        text = PHASE_INSTRUCTION.lower()
        assert "done" in text
        assert any(
            phrase in text
            for phrase in ("no pr", "without a pr", "one-shot", "wrapped up", "finished")
        )

    def test_phase_instruction_ready_covers_unopened_pr(self):
        """A feature branch that's committed + pushed but has no PR open yet
        is `ready`, not `done`. The instruction must say so explicitly so the
        tag doesn't jump straight to done before integration (see the gap hit
        on #97: committed/pushed, no PR → erroneously tagged done)."""
        from agent_hq.hooks import PHASE_INSTRUCTION

        text = PHASE_INSTRUCTION.lower()
        # `ready` no longer requires the PR to literally be open.
        assert "hasn't been opened yet" in text or "not yet opened" in text
        # `done` is guarded against premature use on an unmerged branch — and
        # that guard must NOT itself license a premature `ready` (#110): an
        # unmerged branch is "not done", and only `ready` if ready's full bar
        # holds, otherwise it stays in implement/qa/review.
        assert "not done" in text
        assert "only if ready" in text

    def test_session_start_injects_instruction_regardless_of_daemon(self, tmp_path):
        """#285: the instruction is delivered exactly once, at SessionStart.

        A session that starts while the daemon is down (install-global,
        crash, manual stop) would otherwise never be told to emit
        ``<phase>`` tags — for its whole lifetime, even after the daemon
        returns. An unmonitored session carrying a tag line is harmless;
        a monitored one that never tags is not. So no pid-file gate: the
        hook never consults the daemon at all.
        """
        with (
            patch(
                "sys.stdin",
                StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "x"})),
            ),
            patch.dict(os.environ, {"TMUX_PANE": "%5"}, clear=False),
            patch("sys.stdout", new_callable=StringIO) as out,
        ):
            handle_hook_event()

        payload = json.loads(out.getvalue())
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "<phase>" in payload["hookSpecificOutput"]["additionalContext"]

    def test_session_start_without_tmux_pane_emits_no_context(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
        with (
            patch(
                "sys.stdin",
                StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "x"})),
            ),
            patch.dict(os.environ, env, clear=True),
            patch("sys.stdout", new_callable=StringIO) as out,
        ):
            handle_hook_event()

        text = out.getvalue().strip()
        assert text == "{}" or "additionalContext" not in text


class TestHookNoLongerWritesPhase:
    """The hook stopped owning phase (#73 Test A fix).

    It must never write ``phase``/``phase_set_at``/``phase_source`` —
    even when the transcript carries a ``<phase>`` tag — but it still
    writes the display-only ``latest_summary`` on Stop and still bumps
    the monotonic revision (incl. across SessionEnd).
    """

    def _run(self, event_name, state_dir, *, transcript_path=None, pane_id="%42"):
        payload = {"hook_event_name": event_name, "session_id": "abc"}
        if transcript_path is not None:
            payload["transcript_path"] = str(transcript_path)
        with (
            patch("sys.stdin", StringIO(json.dumps(payload))),
            patch.dict(os.environ, {"TMUX_PANE": pane_id}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
            patch("sys.stdout", new_callable=StringIO),
        ):
            handle_hook_event()
        return state_dir / f"{pane_id.lstrip('%')}.json"

    def test_stop_with_phase_tag_does_not_write_phase_fields(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        _write_transcript(transcript, "Did the thing.\n\n<phase>qa</phase>")
        state_file = self._run("Stop", tmp_path / "states", transcript_path=transcript)

        data = json.loads(state_file.read_text())
        assert "phase" not in data
        assert "phase_set_at" not in data
        assert "phase_source" not in data
        # Display-only summary is still captured on Stop.
        assert "<phase>qa</phase>" in (data.get("latest_summary") or "")
        assert data["state"] == "idle"

    def test_post_tool_use_with_tag_does_not_write_phase(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        _write_transcript(transcript, "Reading.\n\n<phase>brainstorm</phase>")
        state_file = self._run("PostToolUse", tmp_path / "states", transcript_path=transcript)

        data = json.loads(state_file.read_text())
        assert "phase" not in data
        # PostToolUse no longer reads the transcript at all (no summary).
        assert data.get("latest_summary") is None
        assert data["state"] == "running"

    def test_reused_pane_cannot_launder_stale_phase(self, tmp_path):
        """The #1 stale-carry bug class is now structurally impossible:
        the hook writes no phase, so a pane reused by a new session
        cannot inherit (or re-scope) a prior session's phase."""
        state_dir = tmp_path / "states"
        state_dir.mkdir()
        (state_dir / "7.json").write_text(
            json.dumps(
                {
                    "revision": 4,
                    "state": "idle",
                    "session_id": "old-session",
                    "timestamp": time.time(),
                }
            )
        )
        state_file = self._run("PreToolUse", state_dir, pane_id="%7")

        data = json.loads(state_file.read_text())
        assert data["session_id"] == "abc"
        assert data["revision"] == 5
        assert "phase" not in data

    def test_session_end_tombstones_with_revision_bump(self, tmp_path):
        state_dir = tmp_path / "states"
        seed = self._run("Stop", state_dir)
        revision_before = json.loads(seed.read_text())["revision"]

        state_file = self._run("SessionEnd", state_dir)

        data = json.loads(state_file.read_text())
        assert data["tombstone"] is True
        assert data["session_id"] is None
        assert data["revision"] == revision_before + 1


class TestExtractLastAssistantText:
    """Reverse-read implementation: avoid re-parsing the entire JSONL on every Stop."""

    def test_returns_last_assistant_text(self, tmp_path):
        from agent_hq.transcript import extract_last_assistant_text

        transcript = tmp_path / "s.jsonl"
        _write_transcript(transcript, "first turn", "second turn", "third turn")
        assert extract_last_assistant_text(str(transcript)) == "third turn"

    def test_returns_none_when_no_assistant_records(self, tmp_path):
        from agent_hq.transcript import extract_last_assistant_text

        transcript = tmp_path / "s.jsonl"
        transcript.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        assert extract_last_assistant_text(str(transcript)) is None

    def test_returns_none_for_empty_file(self, tmp_path):
        from agent_hq.transcript import extract_last_assistant_text

        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")
        assert extract_last_assistant_text(str(transcript)) is None

    def test_returns_none_for_missing_file(self):
        from agent_hq.transcript import extract_last_assistant_text

        assert extract_last_assistant_text("/no/such/file.jsonl") is None

    def test_skips_non_assistant_records_at_tail(self, tmp_path):
        """Common shape: assistant turn followed by user/tool records — must
        still find the assistant text via reverse-walking past them."""
        from agent_hq.transcript import extract_last_assistant_text

        transcript = tmp_path / "s.jsonl"
        records = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "the answer"}]}},
            {"type": "user", "message": {"content": "follow-up"}},
            {"type": "user", "message": {"content": "another follow-up"}},
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in records))
        assert extract_last_assistant_text(str(transcript)) == "the answer"

    def test_handles_record_split_across_chunk_boundary(self, tmp_path):
        """Reverse read happens in chunks. A long assistant record may straddle
        a chunk boundary; the leftover-on-next-chunk handling must reassemble it."""
        from agent_hq.transcript import extract_last_assistant_text

        transcript = tmp_path / "s.jsonl"
        # First turn is huge — much larger than the reverse-read chunk.
        big = "x" * (200 * 1024)
        _write_transcript(transcript, big, "tiny last")
        result = extract_last_assistant_text(str(transcript))
        assert result == "tiny last"

    def test_finds_assistant_in_large_file_with_only_one_assistant_turn(self, tmp_path):
        """Reverse walk must keep going past many user records to find the
        single assistant turn near the start."""
        from agent_hq.transcript import extract_last_assistant_text

        transcript = tmp_path / "s.jsonl"
        records = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "early answer"}]},
            },
        ]
        # Pad with thousands of user records.
        for i in range(2000):
            records.append({"type": "user", "message": {"content": f"msg {i}"}})
        transcript.write_text("\n".join(json.dumps(r) for r in records))
        assert extract_last_assistant_text(str(transcript)) == "early answer"


class TestPruneStaleHookStateFiles:
    """``prune_stale_hook_state_files(active_pane_ids, agents_by_pane)`` —
    daemon GC over the state dir.

    * Orphan (pane no longer in tmux) → ``os.unlink`` (safe: any future
      session reusing the pane number starts at revision=1).
    * Mismatch / legacy (still-live pane, wrong/absent session_id) →
      write a tombstone with ``revision+1``.
    * Empty ``active_pane_ids`` → no-op (no observation).
    """

    def _agent(self, pane_id, transcript_id):
        return Agent(
            pane_id=pane_id,
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/tmp",
            raw_command="claude",
            state=AgentState.IDLE,
            claude_transcript_session_id=transcript_id,
        )

    def test_orphan_pane_state_file_is_unlinked(self, tmp_path):
        from agent_hq.hooks import prune_stale_hook_state_files

        (tmp_path / "5.json").write_text(
            json.dumps({"revision": 1, "state": "idle", "session_id": "abc"})
        )
        (tmp_path / "9.json").write_text(
            json.dumps({"revision": 3, "state": "idle", "session_id": "xyz"})
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            prune_stale_hook_state_files({"%5"}, {"%5": self._agent("%5", "abc")})

        assert (tmp_path / "5.json").exists()
        assert not (tmp_path / "9.json").exists()

    def test_active_pane_without_discovered_agent_is_not_unlinked(self, tmp_path):
        """Codex v5 finding: orphan set is built from ALL tmux panes, not
        just discovered agents. If discovery transiently miss-classifies a
        pane (Claude between commands, weird process tree), the pane is in
        ``active_pane_ids`` but absent from ``agents_by_pane`` — and its
        state file must survive."""
        from agent_hq.hooks import prune_stale_hook_state_files

        (tmp_path / "5.json").write_text(
            json.dumps({"revision": 1, "state": "running", "session_id": "abc"})
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            # Pane %5 is in active set but NOT in agents_by_pane.
            prune_stale_hook_state_files({"%5"}, {})

        assert (tmp_path / "5.json").exists()
        data = json.loads((tmp_path / "5.json").read_text())
        # No agent → current_session_id=None → exactly-one-None →
        # transient_unknown, NOT mismatch/legacy → no tombstone.
        assert "tombstone" not in data

    def test_mismatched_session_id_gets_tombstoned(self, tmp_path):
        from agent_hq.hooks import prune_stale_hook_state_files

        (tmp_path / "5.json").write_text(
            json.dumps({"revision": 4, "state": "idle", "session_id": "old-session"})
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            prune_stale_hook_state_files({"%5"}, {"%5": self._agent("%5", "new-session")})

        data = json.loads((tmp_path / "5.json").read_text())
        assert data["tombstone"] is True
        assert data["session_id"] is None
        assert data["revision"] == 5  # revision+1

    def test_legacy_file_without_session_id_key_gets_tombstoned(self, tmp_path):
        from agent_hq.hooks import prune_stale_hook_state_files

        (tmp_path / "5.json").write_text(
            json.dumps({"revision": 2, "state": "idle"})  # no session_id key
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            prune_stale_hook_state_files({"%5"}, {"%5": self._agent("%5", "abc")})

        data = json.loads((tmp_path / "5.json").read_text())
        assert data["tombstone"] is True
        assert data["revision"] == 3

    def test_matching_session_id_left_alone(self, tmp_path):
        from agent_hq.hooks import prune_stale_hook_state_files

        original = {"revision": 1, "state": "running", "session_id": "abc"}
        (tmp_path / "5.json").write_text(json.dumps(original))
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            prune_stale_hook_state_files({"%5"}, {"%5": self._agent("%5", "abc")})

        data = json.loads((tmp_path / "5.json").read_text())
        assert "tombstone" not in data
        assert data["session_id"] == "abc"

    def test_existing_tombstone_left_alone(self, tmp_path):
        from agent_hq.hooks import prune_stale_hook_state_files

        (tmp_path / "5.json").write_text(
            json.dumps({"revision": 9, "tombstone": True, "session_id": None})
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            prune_stale_hook_state_files({"%5"}, {"%5": self._agent("%5", "abc")})

        data = json.loads((tmp_path / "5.json").read_text())
        # Untouched — already a tombstone, no revision bump.
        assert data["revision"] == 9

    def test_no_op_when_active_set_empty(self, tmp_path):
        """Empty active set means no observation, NOT 'no panes'."""
        from agent_hq.hooks import prune_stale_hook_state_files

        (tmp_path / "5.json").write_text(json.dumps({"revision": 1, "state": "idle"}))
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            prune_stale_hook_state_files(set(), {})

        assert (tmp_path / "5.json").exists()
        data = json.loads((tmp_path / "5.json").read_text())
        assert "tombstone" not in data

    def test_handles_missing_dir(self, tmp_path):
        from agent_hq.hooks import prune_stale_hook_state_files

        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path / "nope"):
            # Should not raise.
            prune_stale_hook_state_files({"%5"}, {})

    def test_ignores_non_json_files(self, tmp_path):
        from agent_hq.hooks import prune_stale_hook_state_files

        (tmp_path / "5.json").write_text(json.dumps({"revision": 1, "state": "idle"}))
        (tmp_path / "notes.txt").write_text("hello")
        (tmp_path / ".hidden").write_text("x")
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            prune_stale_hook_state_files({"%5"}, {"%5": self._agent("%5", None)})

        assert (tmp_path / "5.json").exists()
        assert (tmp_path / "notes.txt").exists()
        assert (tmp_path / ".hidden").exists()


CODEX_THREAD_A = "019f0000-0000-7000-8000-00000000aaaa"
CODEX_THREAD_B = "019fffff-0000-7000-8000-00000000bbbb"

CODEX_PAYLOAD_BASE = {
    "session_id": CODEX_THREAD_A,
    "transcript_path": "/tmp/rollout.jsonl",
    "cwd": "/work/tree",
    "model": "gpt-5.6-sol",
    "permission_mode": "on-request",
}


def _write_tui_rollout(tmp_path, *, thread_id=CODEX_THREAD_A, name="rollout.jsonl"):
    """An interactive-TUI rollout head — what the SessionStart identity
    write requires before it will bind a pane (subagent/exec filter)."""
    p = tmp_path / name
    meta = {
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "cwd": "/work/tree",
            "originator": "codex-tui",
            "thread_source": "user",
        },
    }
    p.write_text(json.dumps(meta) + "\n")
    return p


class TestCodexHookWrites:
    """``agent-hq-hook --harness codex`` (#206): identity-carrying writes.

    No SessionEnd exists in Codex — nothing here writes tombstones; expiry
    stays with the prune/staleness/pane-reuse paths.
    """

    def _run_codex(self, event, state_dir, extra=None, pane="%7", argv=None):
        # The rollout must belong to the session in the payload — the hook
        # rejects incoherent pairings (see _codex_event_owns_pane).
        session = (extra or {}).get("session_id", CODEX_THREAD_A)
        rollout = _write_tui_rollout(state_dir, thread_id=session, name=f"rollout-{session}.jsonl")
        payload = {
            **CODEX_PAYLOAD_BASE,
            "transcript_path": str(rollout),
            "hook_event_name": event,
            **(extra or {}),
        }
        with (
            patch("sys.stdin", StringIO(json.dumps(payload))),
            patch.dict("os.environ", {"TMUX_PANE": pane}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
            patch("sys.argv", argv or ["agent-hq-hook", "--harness", "codex"]),
            patch("sys.stdout", new_callable=StringIO) as mock_stdout,
        ):
            handle_hook_event()
        return mock_stdout.getvalue()

    def test_session_start_writes_identity(self, tmp_path):
        out = self._run_codex("SessionStart", tmp_path, {"source": "startup"})
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["harness"] == "codex"
        assert data["session_id"] == CODEX_THREAD_A
        assert data["transcript_path"] == str(tmp_path / f"rollout-{CODEX_THREAD_A}.jsonl")
        assert data["cwd"] == "/work/tree"
        assert data["model"] == "gpt-5.6-sol"
        assert data["revision"] == 1
        assert "state" not in data
        # #285: an interactive codex TUI session is told to emit <phase>
        # tags at SessionStart regardless of daemon state (the identity
        # write above is what this test is really about).
        assert "<phase>" in json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def test_state_event_carries_identity(self, tmp_path):
        self._run_codex("UserPromptSubmit", tmp_path)
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["state"] == "running"
        assert data["harness"] == "codex"
        assert data["transcript_path"] == str(tmp_path / f"rollout-{CODEX_THREAD_A}.jsonl")
        assert data["cwd"] == "/work/tree"
        assert data["model"] == "gpt-5.6-sol"

    def test_stop_summary_from_payload(self, tmp_path):
        self._run_codex(
            "Stop",
            tmp_path,
            {"last_assistant_message": "did the thing", "stop_hook_active": False},
        )
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["state"] == "idle"
        assert data["latest_summary"] == "did the thing"

    def test_stop_without_message_leaves_summary_none(self, tmp_path):
        self._run_codex("Stop", tmp_path)
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["latest_summary"] is None

    def test_coherent_new_session_claims_the_pane(self, tmp_path):
        """A codex restarted in the same pane announces itself on its first
        state event (its SessionStart skips the identity write because the
        rollout does not exist yet) — it must be able to take the pane.
        Incoherent events are rejected instead; see
        TestRestartAndInjectionGating."""
        self._run_codex("SessionStart", tmp_path, {"source": "startup"})
        self._run_codex("PreToolUse", tmp_path, {"session_id": CODEX_THREAD_B})
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["session_id"] == CODEX_THREAD_B
        assert data["state"] == "running"

    def test_new_sessionstart_replaces_identity(self, tmp_path):
        self._run_codex("SessionStart", tmp_path, {"source": "startup"})
        self._run_codex(
            "SessionStart", tmp_path, {"session_id": CODEX_THREAD_B, "source": "startup"}
        )
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["session_id"] == CODEX_THREAD_B
        assert data["revision"] == 2

    def test_matching_event_after_sessionstart_writes_state(self, tmp_path):
        self._run_codex("SessionStart", tmp_path, {"source": "startup"})
        self._run_codex("UserPromptSubmit", tmp_path)
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["state"] == "running"
        assert data["session_id"] == CODEX_THREAD_A
        assert data["revision"] == 2

    def test_event_after_tombstone_writes_fresh(self, tmp_path):
        """A prune-written tombstone must not block the next session's events."""
        (tmp_path / "7.json").write_text(
            json.dumps(
                {"revision": 5, "tombstone": True, "session_id": None, "timestamp": time.time()}
            )
        )
        self._run_codex("UserPromptSubmit", tmp_path)
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["state"] == "running"
        assert data["session_id"] == CODEX_THREAD_A
        assert data["revision"] == 6

    def test_sessionstart_injects_phase_instruction(self, tmp_path):
        out = self._run_codex("SessionStart", tmp_path, {"source": "startup"})
        response = json.loads(out)
        assert response["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "Agent HQ is monitoring" in response["hookSpecificOutput"]["additionalContext"]
        # Identity written too.
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["harness"] == "codex"

    def test_claude_events_write_no_harness_key(self, tmp_path):
        stdin_data = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"})
        with (
            patch("sys.stdin", StringIO(stdin_data)),
            patch.dict("os.environ", {"TMUX_PANE": "%7"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("sys.argv", ["agent-hq-hook"]),
            patch("sys.stdout", new_callable=StringIO),
        ):
            handle_hook_event()
        data = json.loads((tmp_path / "7.json").read_text())
        assert "harness" not in data
        assert "transcript_path" not in data

    def test_claude_takeover_erases_codex_identity(self, tmp_path):
        """A claude session reusing the pane organically drops codex identity."""
        self._run_codex("SessionStart", tmp_path, {"source": "startup"})
        stdin_data = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s1"})
        with (
            patch("sys.stdin", StringIO(stdin_data)),
            patch.dict("os.environ", {"TMUX_PANE": "%7"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("sys.argv", ["agent-hq-hook"]),
            patch("sys.stdout", new_callable=StringIO),
        ):
            handle_hook_event()
        data = json.loads((tmp_path / "7.json").read_text())
        assert "harness" not in data
        assert data["session_id"] == "s1"


class TestReadHookIdentity:
    def _run_codex_sessionstart(self, state_dir, extra=None):
        session = (extra or {}).get("session_id", CODEX_THREAD_A)
        rollout = _write_tui_rollout(state_dir, thread_id=session, name=f"rollout-{session}.jsonl")
        payload = {
            **CODEX_PAYLOAD_BASE,
            "transcript_path": str(rollout),
            "hook_event_name": "SessionStart",
            **(extra or {}),
        }
        with (
            patch("sys.stdin", StringIO(json.dumps(payload))),
            patch.dict("os.environ", {"TMUX_PANE": "%7"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
            patch("sys.argv", ["agent-hq-hook", "--harness", "codex"]),
            patch("sys.stdout", new_callable=StringIO),
        ):
            handle_hook_event()

    def test_roundtrip(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        self._run_codex_sessionstart(tmp_path)
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            ident = read_hook_identity("%7")
        assert ident.harness == "codex"
        assert ident.session_id == CODEX_THREAD_A
        assert ident.transcript_path == str(tmp_path / f"rollout-{CODEX_THREAD_A}.jsonl")
        assert ident.cwd == "/work/tree"
        assert ident.model == "gpt-5.6-sol"

    def test_claude_file_has_no_identity(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        (tmp_path / "7.json").write_text(
            json.dumps(
                {"revision": 1, "state": "idle", "session_id": "abc", "timestamp": time.time()}
            )
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            assert read_hook_identity("%7") is None

    def test_missing_file(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            assert read_hook_identity("%7") is None

    def test_tombstone(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        (tmp_path / "7.json").write_text(
            json.dumps({"revision": 3, "tombstone": True, "session_id": None})
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            assert read_hook_identity("%7") is None


class TestPruneCodexGeneralization:
    """Prune's mismatch check keys on ``session_metadata.stable_id`` —
    for codex that's the thread id, so a stale codex state file whose
    session no longer matches the discovered agent gets tombstoned."""

    def _codex_agent(self, pane_id, thread_id):
        return Agent(
            pane_id=pane_id,
            session_name="s",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CODEX,
            cwd="/tmp",
            raw_command="codex",
            state=AgentState.IDLE,
            codex_thread_id=thread_id,
        )

    def test_codex_mismatch_tombstoned(self, tmp_path):
        from agent_hq.hooks import prune_stale_hook_state_files

        (tmp_path / "5.json").write_text(
            json.dumps(
                {
                    "revision": 2,
                    "state": "idle",
                    "harness": "codex",
                    "session_id": CODEX_THREAD_A,
                    "timestamp": time.time(),
                }
            )
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            prune_stale_hook_state_files({"%5"}, {"%5": self._codex_agent("%5", CODEX_THREAD_B)})
        data = json.loads((tmp_path / "5.json").read_text())
        assert data.get("tombstone") is True
        assert data["revision"] == 3

    def test_codex_match_untouched(self, tmp_path):
        from agent_hq.hooks import prune_stale_hook_state_files

        payload = {
            "revision": 2,
            "state": "idle",
            "harness": "codex",
            "session_id": CODEX_THREAD_A,
            "timestamp": time.time(),
        }
        (tmp_path / "5.json").write_text(json.dumps(payload))
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            prune_stale_hook_state_files({"%5"}, {"%5": self._codex_agent("%5", CODEX_THREAD_A)})
        data = json.loads((tmp_path / "5.json").read_text())
        assert "tombstone" not in data
        assert data["revision"] == 2


class TestReadHookIdentityStaleness:
    """Codex review P1: with no SessionEnd, a hooks-file edit / trust loss
    stops all writes — the identity must expire (STALE_THRESHOLD_SECONDS)
    rather than bind a later same-cwd session forever."""

    def _write_identity(self, tmp_path, *, age_s):
        (tmp_path / "7.json").write_text(
            json.dumps(
                {
                    "revision": 1,
                    "harness": "codex",
                    "session_id": CODEX_THREAD_A,
                    "transcript_path": "/tmp/rollout.jsonl",
                    "cwd": "/work/tree",
                    "model": "gpt-5.6-sol",
                    "timestamp": time.time() - age_s,
                }
            )
        )

    def test_fresh_identity_returned(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        self._write_identity(tmp_path, age_s=10)
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            assert read_hook_identity("%7") is not None

    def test_stale_identity_expires(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        self._write_identity(tmp_path, age_s=STALE_THRESHOLD_SECONDS + 60)
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            assert read_hook_identity("%7") is None

    def test_missing_timestamp_is_stale(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        (tmp_path / "7.json").write_text(
            json.dumps({"revision": 1, "harness": "codex", "session_id": CODEX_THREAD_A})
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            assert read_hook_identity("%7") is None


class TestIdentityValidityByProcessAge:
    """Multi-review P1 (both reviewers): elapsed time is the wrong expiry
    discriminator for a pane→thread association. What matters is whether
    the pane's CURRENT codex process is the one that wrote the identity.
    A live-but-idle session must keep its identity; a new process in a
    reused pane must not inherit the old one."""

    def _write(self, tmp_path, *, age_s):
        (tmp_path / "7.json").write_text(
            json.dumps(
                {
                    "revision": 1,
                    "harness": "codex",
                    "session_id": CODEX_THREAD_A,
                    "transcript_path": "/tmp/rollout.jsonl",
                    "cwd": "/work/tree",
                    "model": "gpt-5.6-sol",
                    "timestamp": time.time() - age_s,
                }
            )
        )

    def test_fresh_identity_is_also_process_validated(self, tmp_path):
        """Superseded by TestFreshIdentityAlsoValidatesProcess: ownership is
        decided by the process at ANY age, so a fresh record is checked too
        (pane reuse inside the staleness window is a real case)."""
        from agent_hq.hooks import read_hook_identity

        self._write(tmp_path, age_s=10)
        with (
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("agent_hq.hooks.process_start_time", return_value=1.0) as mock_start,
        ):
            assert read_hook_identity("%7", agent_pid=999) is not None
        mock_start.assert_called_once_with(999)

    def test_stale_identity_survives_when_process_predates_it(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        self._write(tmp_path, age_s=3600)
        # Process started long before the identity write → same session.
        with (
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("agent_hq.hooks.process_start_time", return_value=time.time() - 7200),
        ):
            ident = read_hook_identity("%7", agent_pid=999)
        assert ident is not None
        assert ident.session_id == CODEX_THREAD_A

    def test_stale_identity_rejected_when_process_is_newer(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        self._write(tmp_path, age_s=3600)
        # Process started AFTER the identity write → pane was reused.
        with (
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("agent_hq.hooks.process_start_time", return_value=time.time() - 60),
        ):
            assert read_hook_identity("%7", agent_pid=999) is None

    def test_stale_identity_rejected_when_process_time_unknown(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        self._write(tmp_path, age_s=3600)
        with (
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("agent_hq.hooks.process_start_time", return_value=None),
        ):
            assert read_hook_identity("%7", agent_pid=999) is None

    def test_stale_identity_rejected_without_pid(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        self._write(tmp_path, age_s=3600)
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            assert read_hook_identity("%7") is None

    def test_missing_timestamp_still_rejected(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        (tmp_path / "7.json").write_text(
            json.dumps({"revision": 1, "harness": "codex", "session_id": CODEX_THREAD_A})
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            assert read_hook_identity("%7", agent_pid=999) is None


class TestSessionStartSubagentFilter:
    """Workflow review: the heuristic path refuses non-`codex-tui` and
    subagent rollouts; the hook identity write must apply the same filter
    or a `codex exec` run sharing the pane steals the association."""

    def _run_sessionstart(self, state_dir, transcript_path, session_id=CODEX_THREAD_A):
        payload = {
            **CODEX_PAYLOAD_BASE,
            "hook_event_name": "SessionStart",
            "source": "startup",
            "session_id": session_id,
            "transcript_path": str(transcript_path),
        }
        with (
            patch("sys.stdin", StringIO(json.dumps(payload))),
            patch.dict("os.environ", {"TMUX_PANE": "%7"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
            patch("sys.argv", ["agent-hq-hook", "--harness", "codex"]),
            patch("sys.stdout", new_callable=StringIO),
        ):
            handle_hook_event()

    def _rollout(self, tmp_path, name, *, originator, thread_source="user"):
        p = tmp_path / name
        meta = {
            "type": "session_meta",
            "payload": {
                "id": CODEX_THREAD_A,
                "cwd": "/work/tree",
                "originator": originator,
                "thread_source": thread_source,
            },
        }
        p.write_text(json.dumps(meta) + "\n")
        return p

    def test_interactive_tui_writes_identity(self, tmp_path):
        rollout = self._rollout(tmp_path, "tui.jsonl", originator="codex-tui")
        self._run_sessionstart(tmp_path, rollout)
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["harness"] == "codex"
        assert data["session_id"] == CODEX_THREAD_A

    def test_exec_originator_does_not_write_identity(self, tmp_path):
        rollout = self._rollout(tmp_path, "exec.jsonl", originator="codex_exec")
        self._run_sessionstart(tmp_path, rollout)
        assert not (tmp_path / "7.json").exists()

    def test_subagent_does_not_write_identity(self, tmp_path):
        rollout = self._rollout(
            tmp_path, "sub.jsonl", originator="codex-tui", thread_source="subagent"
        )
        self._run_sessionstart(tmp_path, rollout)
        assert not (tmp_path / "7.json").exists()

    def test_unreadable_rollout_does_not_write_identity(self, tmp_path):
        self._run_sessionstart(tmp_path, tmp_path / "missing.jsonl")
        assert not (tmp_path / "7.json").exists()

    def test_exec_does_not_clobber_live_interactive_identity(self, tmp_path):
        tui = self._rollout(tmp_path, "tui.jsonl", originator="codex-tui")
        self._run_sessionstart(tmp_path, tui)
        exec_rollout = self._rollout(tmp_path, "exec.jsonl", originator="codex_exec")
        self._run_sessionstart(tmp_path, exec_rollout, session_id=CODEX_THREAD_B)
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["session_id"] == CODEX_THREAD_A  # interactive keeps the pane


class TestStateEventInteractiveFilter:
    """Live-smoke follow-up: the TUI creates its rollout lazily, so the
    SessionStart identity write is often skipped and identity first lands
    on a STATE event. That path must apply the same interactive filter, or
    a `codex exec` sharing the pane still steals the association."""

    def _run(self, state_dir, rollout, event="UserPromptSubmit", session_id=CODEX_THREAD_A):
        payload = {
            **CODEX_PAYLOAD_BASE,
            "hook_event_name": event,
            "session_id": session_id,
            "transcript_path": str(rollout),
        }
        with (
            patch("sys.stdin", StringIO(json.dumps(payload))),
            patch.dict("os.environ", {"TMUX_PANE": "%7"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
            patch("sys.argv", ["agent-hq-hook", "--harness", "codex"]),
            patch("sys.stdout", new_callable=StringIO),
        ):
            handle_hook_event()

    def _rollout(self, tmp_path, name, *, originator, thread_source="user", thread_id=None):
        p = tmp_path / name
        meta = {
            "type": "session_meta",
            "payload": {
                "id": thread_id or CODEX_THREAD_A,
                "cwd": "/work/tree",
                "originator": originator,
                "thread_source": thread_source,
            },
        }
        p.write_text(json.dumps(meta) + "\n")
        return p

    def test_interactive_state_event_writes_identity(self, tmp_path):
        rollout = self._rollout(tmp_path, "tui.jsonl", originator="codex-tui")
        self._run(tmp_path, rollout)
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["state"] == "running"
        assert data["harness"] == "codex"
        assert data["transcript_path"] == str(rollout)

    def test_exec_state_event_writes_no_identity(self, tmp_path):
        rollout = self._rollout(tmp_path, "exec.jsonl", originator="codex_exec")
        self._run(tmp_path, rollout)
        assert not (tmp_path / "7.json").exists()

    def test_subagent_state_event_writes_no_identity(self, tmp_path):
        rollout = self._rollout(
            tmp_path, "sub.jsonl", originator="codex-tui", thread_source="subagent"
        )
        self._run(tmp_path, rollout)
        assert not (tmp_path / "7.json").exists()

    def test_exec_cannot_overwrite_live_interactive_identity(self, tmp_path):
        tui = self._rollout(tmp_path, "tui.jsonl", originator="codex-tui")
        self._run(tmp_path, tui)
        ex = self._rollout(
            tmp_path, "exec.jsonl", originator="codex_exec", thread_id=CODEX_THREAD_B
        )
        self._run(tmp_path, ex, event="Stop", session_id=CODEX_THREAD_B)
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["session_id"] == CODEX_THREAD_A
        assert data["state"] == "running"  # exec's idle never applied


class TestFreshIdentityAlsoValidatesProcess:
    """Final review P1: a FRESH record must also be process-validated —
    a codex process replaced within the staleness window (with its
    SessionStart missed / hooks untrusted) would otherwise inherit the
    previous process's thread, rollout, deep link and resume target."""

    def _write(self, tmp_path, *, age_s):
        (tmp_path / "7.json").write_text(
            json.dumps(
                {
                    "revision": 1,
                    "harness": "codex",
                    "session_id": CODEX_THREAD_A,
                    "transcript_path": "/tmp/rollout.jsonl",
                    "cwd": "/work/tree",
                    "model": "gpt-5.6-sol",
                    "timestamp": time.time() - age_s,
                }
            )
        )

    def test_fresh_record_rejected_when_process_is_newer(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        self._write(tmp_path, age_s=30)
        with (
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("agent_hq.hooks.process_start_time", return_value=time.time() - 5),
        ):
            assert read_hook_identity("%7", agent_pid=999) is None

    def test_fresh_record_kept_when_process_predates_it(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        self._write(tmp_path, age_s=30)
        with (
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("agent_hq.hooks.process_start_time", return_value=time.time() - 600),
        ):
            assert read_hook_identity("%7", agent_pid=999) is not None

    def test_fresh_record_kept_without_pid(self, tmp_path):
        """No pid to check against (non-discovery callers) → freshness alone."""
        from agent_hq.hooks import read_hook_identity

        self._write(tmp_path, age_s=30)
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            assert read_hook_identity("%7") is not None

    def test_process_start_time_is_cached_per_pid(self, tmp_path):
        from agent_hq.hooks import _clear_process_start_cache, read_hook_identity

        _clear_process_start_cache()
        self._write(tmp_path, age_s=30)
        with (
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("agent_hq.hooks._process_start_time_uncached", return_value=1.0) as probe,
        ):
            read_hook_identity("%7", agent_pid=999)
            read_hook_identity("%7", agent_pid=999)
        assert probe.call_count == 1


class TestRestartAndInjectionGating:
    """Final confirmation round: (a) a TUI restarted in the same pane must
    be able to claim it — the lazy rollout means its SessionStart skips the
    identity write, so its first STATE event is the announcement; (b) the
    phase instruction must not be injected into sessions we deliberately
    ignore (codex exec / subagents)."""

    def _rollout(self, tmp_path, name, *, thread_id, originator="codex-tui", thread_source="user"):
        p = tmp_path / name
        meta = {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "cwd": "/work/tree",
                "originator": originator,
                "thread_source": thread_source,
            },
        }
        p.write_text(json.dumps(meta) + "\n")
        return p

    def _run(self, state_dir, rollout, *, event, session_id):
        payload = {
            **CODEX_PAYLOAD_BASE,
            "hook_event_name": event,
            "session_id": session_id,
            "transcript_path": str(rollout),
        }
        if event == "SessionStart":
            payload["source"] = "startup"
        with (
            patch("sys.stdin", StringIO(json.dumps(payload))),
            patch.dict("os.environ", {"TMUX_PANE": "%7"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
            patch("sys.argv", ["agent-hq-hook", "--harness", "codex"]),
            patch("sys.stdout", new_callable=StringIO) as out,
        ):
            handle_hook_event()
        return out.getvalue()

    def test_restarted_tui_claims_pane_from_fresh_dead_session(self, tmp_path):
        first = self._rollout(tmp_path, "a.jsonl", thread_id=CODEX_THREAD_A)
        self._run(tmp_path, first, event="UserPromptSubmit", session_id=CODEX_THREAD_A)
        # Restart: new session, its own new rollout, still within the
        # staleness window of the previous record.
        second = self._rollout(tmp_path, "b.jsonl", thread_id=CODEX_THREAD_B)
        self._run(tmp_path, second, event="UserPromptSubmit", session_id=CODEX_THREAD_B)
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["session_id"] == CODEX_THREAD_B
        assert data["transcript_path"] == str(second)

    def test_event_whose_session_does_not_match_its_rollout_is_dropped(self, tmp_path):
        first = self._rollout(tmp_path, "a.jsonl", thread_id=CODEX_THREAD_A)
        self._run(tmp_path, first, event="UserPromptSubmit", session_id=CODEX_THREAD_A)
        # Payload claims session B but points at A's rollout — incoherent,
        # so it must not take the pane.
        self._run(tmp_path, first, event="Stop", session_id=CODEX_THREAD_B)
        data = json.loads((tmp_path / "7.json").read_text())
        assert data["session_id"] == CODEX_THREAD_A
        assert data["state"] == "running"

    def test_phase_instruction_injected_for_interactive(self, tmp_path):
        rollout = self._rollout(tmp_path, "tui.jsonl", thread_id=CODEX_THREAD_A)
        out = self._run(
            tmp_path,
            rollout,
            event="SessionStart",
            session_id=CODEX_THREAD_A,
        )
        assert "Agent HQ is monitoring" in out

    def test_phase_instruction_not_injected_for_exec(self, tmp_path):
        rollout = self._rollout(
            tmp_path, "exec.jsonl", thread_id=CODEX_THREAD_A, originator="codex_exec"
        )
        out = self._run(
            tmp_path,
            rollout,
            event="SessionStart",
            session_id=CODEX_THREAD_A,
        )
        assert out.strip() == "{}"

    def test_phase_instruction_not_injected_for_subagent(self, tmp_path):
        rollout = self._rollout(
            tmp_path, "sub.jsonl", thread_id=CODEX_THREAD_A, thread_source="subagent"
        )
        out = self._run(
            tmp_path,
            rollout,
            event="SessionStart",
            session_id=CODEX_THREAD_A,
        )
        assert out.strip() == "{}"


class TestPhaseInjectionFailsOpen:
    """Identity and injection need OPPOSITE failure modes.

    Identity must fail CLOSED (positive proof of an interactive rollout).
    Injection must fail OPEN: the TUI creates its rollout lazily, so at
    SessionStart there is usually nothing to read — requiring proof there
    would silently stop injecting the phase instruction into every real
    interactive session, which is the whole feature. `codex exec` DOES
    have its rollout on disk at SessionStart (Stage 0 probe), so
    "known non-interactive" is decidable exactly when it matters.
    """

    def _run_sessionstart(self, tmp_path, transcript_path):
        payload = {
            **CODEX_PAYLOAD_BASE,
            "hook_event_name": "SessionStart",
            "source": "startup",
            "transcript_path": str(transcript_path),
        }
        with (
            patch("sys.stdin", StringIO(json.dumps(payload))),
            patch.dict("os.environ", {"TMUX_PANE": "%7"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("sys.argv", ["agent-hq-hook", "--harness", "codex"]),
            patch("sys.stdout", new_callable=StringIO) as out,
        ):
            handle_hook_event()
        return out.getvalue()

    def test_injects_when_rollout_not_yet_written(self, tmp_path):
        # The real TUI case: SessionStart fires before the rollout exists.
        out = self._run_sessionstart(tmp_path, tmp_path / "not-yet.jsonl")
        assert "Agent HQ is monitoring" in out

    def test_injects_for_known_interactive(self, tmp_path):
        rollout = _write_tui_rollout(tmp_path)
        out = self._run_sessionstart(tmp_path, rollout)
        assert "Agent HQ is monitoring" in out

    def test_skips_for_known_exec(self, tmp_path):
        rollout = tmp_path / "exec.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": CODEX_THREAD_A,
                        "cwd": "/work/tree",
                        "originator": "codex_exec",
                        "thread_source": "user",
                    },
                }
            )
            + "\n"
        )
        out = self._run_sessionstart(tmp_path, rollout)
        assert out.strip() == "{}"

    def test_skips_for_known_subagent(self, tmp_path):
        rollout = tmp_path / "sub.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": CODEX_THREAD_A,
                        "cwd": "/work/tree",
                        "originator": "codex-tui",
                        "thread_source": "subagent",
                    },
                }
            )
            + "\n"
        )
        out = self._run_sessionstart(tmp_path, rollout)
        assert out.strip() == "{}"


class TestProcessStartTimeResolution:
    """`ps lstart` has whole-second resolution while the record timestamp
    has fractions, so a pane reused within the same second could make the
    NEW process look like it predates the OLD record and inherit its
    thread. Identity must fail closed on that ambiguity."""

    def _write(self, tmp_path, *, timestamp):
        (tmp_path / "7.json").write_text(
            json.dumps(
                {
                    "revision": 1,
                    "harness": "codex",
                    "session_id": CODEX_THREAD_A,
                    "transcript_path": "/tmp/rollout.jsonl",
                    "cwd": "/work/tree",
                    "model": "gpt-5.6-sol",
                    "timestamp": timestamp,
                }
            )
        )

    def test_same_second_start_is_rejected(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        now = time.time()
        self._write(tmp_path, timestamp=now)
        # ps truncates to the second: the real start could be anywhere in
        # that second, including AFTER the record was written.
        with (
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("agent_hq.hooks.process_start_time", return_value=float(int(now))),
        ):
            assert read_hook_identity("%7", agent_pid=999) is None

    def test_clearly_earlier_start_is_accepted(self, tmp_path):
        from agent_hq.hooks import read_hook_identity

        now = time.time()
        self._write(tmp_path, timestamp=now)
        with (
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("agent_hq.hooks.process_start_time", return_value=now - 30),
        ):
            assert read_hook_identity("%7", agent_pid=999) is not None


_ABSENT = object()


class TestCodexEventRequiresSessionId:
    """Controller review 3a: a payload with no session_id must be dropped,
    not persisted as a partial (``session_id: None``) record — that record
    would drop enrichment to the cwd/mtime heuristic and state to regex
    until the next good event. Every real 0.144.x payload carries the id
    (Stage 0 V4), so nothing legitimate is lost by requiring it."""

    def _run(self, state_dir, rollout, *, event, session_id):
        payload = {
            **CODEX_PAYLOAD_BASE,
            "hook_event_name": event,
            "session_id": session_id,
            "transcript_path": str(rollout),
        }
        if session_id is _ABSENT:
            del payload["session_id"]
        with (
            patch("sys.stdin", StringIO(json.dumps(payload))),
            patch.dict("os.environ", {"TMUX_PANE": "%7"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
            patch("sys.argv", ["agent-hq-hook", "--harness", "codex"]),
            patch("sys.stdout", new_callable=StringIO),
        ):
            handle_hook_event()

    def test_state_event_without_session_id_writes_nothing(self, tmp_path):
        rollout = _write_tui_rollout(tmp_path)
        self._run(tmp_path, rollout, event="UserPromptSubmit", session_id=_ABSENT)
        assert not (tmp_path / "7.json").exists()

    def test_state_event_with_empty_session_id_writes_nothing(self, tmp_path):
        rollout = _write_tui_rollout(tmp_path)
        self._run(tmp_path, rollout, event="UserPromptSubmit", session_id="")
        assert not (tmp_path / "7.json").exists()

    def test_existing_record_survives_malformed_event(self, tmp_path):
        rollout = _write_tui_rollout(tmp_path)
        self._run(tmp_path, rollout, event="UserPromptSubmit", session_id=CODEX_THREAD_A)
        before = (tmp_path / "7.json").read_text()
        self._run(tmp_path, rollout, event="Stop", session_id=_ABSENT)
        assert (tmp_path / "7.json").read_text() == before


class TestParseEtime:
    """``ps etime`` parsing (lstart replacement — controller review 3b:
    numeric elapsed time is locale-independent and immune to the DST
    fall-back hour where a wall-clock start time is ambiguous)."""

    def test_mm_ss(self):
        from agent_hq.hooks import _parse_etime

        assert _parse_etime("05:09") == 5 * 60 + 9

    def test_hh_mm_ss(self):
        from agent_hq.hooks import _parse_etime

        assert _parse_etime("03:05:09") == 3 * 3600 + 5 * 60 + 9

    def test_dd_hh_mm_ss(self):
        from agent_hq.hooks import _parse_etime

        assert _parse_etime("2-03:05:09") == 2 * 86400 + 3 * 3600 + 5 * 60 + 9

    def test_garbage_returns_none(self):
        from agent_hq.hooks import _parse_etime

        assert _parse_etime("") is None
        assert _parse_etime("Mon Jul 20 10:00:00 2026") is None
        assert _parse_etime("x-01:02:03") is None
        assert _parse_etime("42") is None

    def test_uncached_probe_against_real_pid(self):
        """The live ``ps -o etime=`` path: our own pid must resolve to a
        plausible start time in the past."""
        import os as _os
        import time as _time

        from agent_hq.hooks import _process_start_time_uncached

        started = _process_start_time_uncached(_os.getpid())
        assert started is not None
        assert 0 < started <= _time.time() + 1

    def test_unknown_pid_returns_none(self):
        from agent_hq.hooks import _process_start_time_uncached

        # pid_max on macOS is 99998; 2**22 is safely nonexistent.
        assert _process_start_time_uncached(2**22) is None


class TestFindCodexAncestorPid:
    """``_find_codex_ancestor_pid`` walks UP the process tree from the hook's
    parent looking for the codex TUI process (#208). The pid it returns must
    be the codex process itself — the same pid tmux-assistant-resurrect finds
    walking the pane's tree downward — or ``None`` when there is no codex
    ancestor (hook fired outside a codex process tree)."""

    def _fake_ps(self, table):
        """subprocess.run fake serving ``ps -o ppid=,comm= -p <pid>`` from a
        ``{pid: (ppid, comm)}`` table."""

        class Result:
            def __init__(self, returncode, stdout):
                self.returncode = returncode
                self.stdout = stdout

        def run(args, **kwargs):
            assert args[:3] == ["ps", "-o", "ppid=,comm="]
            pid = int(args[-1])
            if pid not in table:
                return Result(1, "")
            ppid, comm = table[pid]
            return Result(0, f"  {ppid} {comm}\n")

        return run

    def test_start_pid_itself_is_codex(self):
        from agent_hq.hooks import _find_codex_ancestor_pid

        run = self._fake_ps({400: (300, "codex")})
        with patch("agent_hq.hooks.subprocess.run", side_effect=run):
            assert _find_codex_ancestor_pid(400) == 400

    def test_walks_up_through_shell_to_full_path_codex(self):
        from agent_hq.hooks import _find_codex_ancestor_pid

        run = self._fake_ps({400: (300, "sh"), 300: (200, "/opt/homebrew/bin/codex")})
        with patch("agent_hq.hooks.subprocess.run", side_effect=run):
            assert _find_codex_ancestor_pid(400) == 300

    def test_no_codex_ancestor_returns_none(self):
        from agent_hq.hooks import _find_codex_ancestor_pid

        run = self._fake_ps({400: (300, "sh"), 300: (1, "tmux"), 1: (0, "launchd")})
        with patch("agent_hq.hooks.subprocess.run", side_effect=run):
            assert _find_codex_ancestor_pid(400) is None

    def test_codex_named_binary_with_suffix_not_matched(self):
        """``codex-helper`` or ``not-codex`` must not count as the TUI."""
        from agent_hq.hooks import _find_codex_ancestor_pid

        run = self._fake_ps({400: (300, "codex-helper"), 300: (1, "not-codex")})
        with patch("agent_hq.hooks.subprocess.run", side_effect=run):
            assert _find_codex_ancestor_pid(400) is None

    def test_ps_oserror_returns_none(self):
        from agent_hq.hooks import _find_codex_ancestor_pid

        with patch("agent_hq.hooks.subprocess.run", side_effect=OSError("no ps")):
            assert _find_codex_ancestor_pid(400) is None

    def test_vanished_pid_mid_walk_returns_none(self):
        from agent_hq.hooks import _find_codex_ancestor_pid

        run = self._fake_ps({400: (300, "sh")})  # 300 not in table -> rc 1
        with patch("agent_hq.hooks.subprocess.run", side_effect=run):
            assert _find_codex_ancestor_pid(400) is None

    def test_walk_depth_is_bounded(self):
        from agent_hq.hooks import _find_codex_ancestor_pid

        # 20-deep chain of shells with codex only at the far top: the walk
        # must give up (bounded) rather than crawl arbitrarily far.
        table = {400 + i: (400 + i + 1, "sh") for i in range(20)}
        table[420] = (421, "codex")
        run = self._fake_ps(table)
        with patch("agent_hq.hooks.subprocess.run", side_effect=run):
            assert _find_codex_ancestor_pid(400) is None


class TestCodexSessionTags:
    """Resurrection tagging (#208): every coherent codex event ensures a
    ``{"pid": ..., "session": ...}`` line exists in
    ``CODEX_SESSION_TAGS_FILE`` so tmux-assistant-resurrect's
    ``get_codex_session()`` can resolve a fresh codex pane. Best-effort:
    failures must never change hook output or state writes."""

    CODEX_PID = 4242

    def _run(self, event, tmp_path, extra=None, *, ancestor_pid=CODEX_PID, argv=None):
        session = (extra or {}).get("session_id", CODEX_THREAD_A)
        rollout = _write_tui_rollout(tmp_path, thread_id=session, name=f"rollout-{session}.jsonl")
        payload = {
            **CODEX_PAYLOAD_BASE,
            "transcript_path": str(rollout),
            "hook_event_name": event,
            **(extra or {}),
        }
        state_dir = tmp_path / "states"
        state_dir.mkdir(exist_ok=True)
        with (
            patch("sys.stdin", StringIO(json.dumps(payload))),
            patch.dict("os.environ", {"TMUX_PANE": "%7"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
            patch(
                "agent_hq.hooks.CODEX_SESSION_TAGS_FILE",
                tmp_path / "codex" / "session-tags.jsonl",
            ),
            patch("agent_hq.hooks._find_codex_ancestor_pid", return_value=ancestor_pid),
            patch("sys.argv", argv or ["agent-hq-hook", "--harness", "codex"]),
            patch("sys.stdout", new_callable=StringIO) as mock_stdout,
        ):
            handle_hook_event()
        return mock_stdout.getvalue()

    def _tags(self, tmp_path):
        path = tmp_path / "codex" / "session-tags.jsonl"
        if not path.exists():
            return []
        records = []
        for line in path.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def test_tag_written_on_coherent_sessionstart(self, tmp_path):
        self._run("SessionStart", tmp_path, {"source": "startup"})
        tags = self._tags(tmp_path)
        assert len(tags) == 1
        assert tags[0]["pid"] == self.CODEX_PID
        assert tags[0]["session"] == CODEX_THREAD_A
        assert tags[0]["cwd"] == "/work/tree"
        assert "timestamp" in tags[0]

    def test_tag_written_on_state_event_without_sessionstart(self, tmp_path):
        """Lazy rollout creation means identity often first lands on a state
        event — the tag must not depend on SessionStart having fired."""
        self._run("PreToolUse", tmp_path)
        tags = self._tags(tmp_path)
        assert len(tags) == 1
        assert tags[0]["session"] == CODEX_THREAD_A

    def test_same_pid_and_session_deduped(self, tmp_path):
        self._run("SessionStart", tmp_path, {"source": "startup"})
        self._run("UserPromptSubmit", tmp_path)
        self._run("Stop", tmp_path, {"last_assistant_message": "done"})
        assert len(self._tags(tmp_path)) == 1

    def test_resume_with_new_pid_appends_second_line(self, tmp_path):
        self._run("UserPromptSubmit", tmp_path)
        self._run("UserPromptSubmit", tmp_path, ancestor_pid=5555)
        tags = self._tags(tmp_path)
        assert [t["pid"] for t in tags] == [self.CODEX_PID, 5555]
        assert all(t["session"] == CODEX_THREAD_A for t in tags)

    def test_no_codex_ancestor_writes_nothing(self, tmp_path):
        out = self._run("UserPromptSubmit", tmp_path, ancestor_pid=None)
        assert self._tags(tmp_path) == []
        assert out.strip() == "{}"
        # The state write itself is unaffected.
        data = json.loads((tmp_path / "states" / "7.json").read_text())
        assert data["state"] == "running"

    def test_malformed_existing_lines_are_tolerated(self, tmp_path):
        tags_file = tmp_path / "codex" / "session-tags.jsonl"
        tags_file.parent.mkdir(parents=True)
        tags_file.write_text('not json\n{"pid": 1111, "session": "other"}\n')
        self._run("UserPromptSubmit", tmp_path)
        raw_lines = [line for line in tags_file.read_text().splitlines() if line.strip()]
        assert len(raw_lines) == 3  # garbage + existing + ours, nothing lost
        tags = self._tags(tmp_path)  # skips the garbage line
        assert tags[-1]["pid"] == self.CODEX_PID

    def test_growth_is_capped_keeping_newest(self, tmp_path):
        from agent_hq.hooks import _SESSION_TAGS_MAX_LINES

        tags_file = tmp_path / "codex" / "session-tags.jsonl"
        tags_file.parent.mkdir(parents=True)
        tags_file.write_text(
            "".join(
                json.dumps({"pid": 10000 + i, "session": f"s-{i}"}) + "\n"
                for i in range(_SESSION_TAGS_MAX_LINES + 100)
            )
        )
        self._run("UserPromptSubmit", tmp_path)
        lines = [line for line in tags_file.read_text().splitlines() if line.strip()]
        assert len(lines) == _SESSION_TAGS_MAX_LINES
        assert json.loads(lines[-1])["pid"] == self.CODEX_PID  # newest kept
        assert json.loads(lines[0])["pid"] == 10101  # oldest dropped

    def test_tag_write_failure_never_breaks_the_hook(self, tmp_path):
        # Parent path is a FILE -> mkdir of the tags dir fails.
        (tmp_path / "codex").write_text("in the way")
        out = self._run("UserPromptSubmit", tmp_path)
        assert out.strip() == "{}"
        data = json.loads((tmp_path / "states" / "7.json").read_text())
        assert data["state"] == "running"


class TestCodexSessionTagsNegativePaths:
    """Sessions the coherence gate refuses (codex exec, subagents) and the
    claude flavor must never become resurrection targets."""

    def _run(self, tmp_path, *, originator="codex-tui", thread_source="user", argv=None):
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": CODEX_THREAD_A,
                        "cwd": "/work/tree",
                        "originator": originator,
                        "thread_source": thread_source,
                    },
                }
            )
            + "\n"
        )
        payload = {
            **CODEX_PAYLOAD_BASE,
            "transcript_path": str(rollout),
            "hook_event_name": "UserPromptSubmit",
        }
        state_dir = tmp_path / "states"
        state_dir.mkdir(exist_ok=True)
        with (
            patch("sys.stdin", StringIO(json.dumps(payload))),
            patch.dict("os.environ", {"TMUX_PANE": "%7"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
            patch(
                "agent_hq.hooks.CODEX_SESSION_TAGS_FILE",
                tmp_path / "codex" / "session-tags.jsonl",
            ),
            patch("agent_hq.hooks._find_codex_ancestor_pid", return_value=4242),
            patch("sys.argv", argv or ["agent-hq-hook", "--harness", "codex"]),
            patch("sys.stdout", new_callable=StringIO),
        ):
            handle_hook_event()
        return tmp_path / "codex" / "session-tags.jsonl"

    def test_exec_rollout_writes_no_tag(self, tmp_path):
        tags = self._run(tmp_path, originator="codex_exec")
        assert not tags.exists()

    def test_subagent_rollout_writes_no_tag(self, tmp_path):
        tags = self._run(tmp_path, thread_source="subagent")
        assert not tags.exists()

    def test_claude_flavor_writes_no_tag(self, tmp_path):
        tags = self._run(tmp_path, argv=["agent-hq-hook"])
        assert not tags.exists()


class TestSessionTagsPluginContract:
    """Pin the cross-tool contract: tmux-assistant-resurrect resolves a codex
    pane's session with

        grep "\\"pid\\": *<pid>[,}]" session-tags.jsonl | tail -1 \\
            | jq -r '.session // empty'

    (scripts/save-assistant-sessions.sh::get_codex_session, Method 1). Run
    the REAL pipeline against a file our writer produced — if our JSON shape
    drifts (key rename, string pid, spacing the grep can't see), this fails
    even though every pure-python test stays green."""

    def _lookup(self, tags_file, pid):
        import subprocess as sp

        script = (
            f'grep "\\"pid\\": *{pid}[,}}]" "{tags_file}" | tail -1 | jq -r \'.session // empty\''
        )
        result = sp.run(["bash", "-c", script], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    @pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
    def test_plugin_pipeline_resolves_each_pid(self, tmp_path, monkeypatch):
        from agent_hq.hooks import _record_codex_session_tag

        tags_file = tmp_path / "codex" / "session-tags.jsonl"
        monkeypatch.setattr("agent_hq.hooks.CODEX_SESSION_TAGS_FILE", tags_file)
        with patch("agent_hq.hooks._find_codex_ancestor_pid", return_value=4242):
            _record_codex_session_tag({"session_id": CODEX_THREAD_A, "cwd": "/work/a"})
        with patch("agent_hq.hooks._find_codex_ancestor_pid", return_value=5555):
            _record_codex_session_tag({"session_id": CODEX_THREAD_B, "cwd": "/work/b"})

        assert self._lookup(tags_file, 4242) == CODEX_THREAD_A
        assert self._lookup(tags_file, 5555) == CODEX_THREAD_B
        # A pid with no tag resolves to empty — the plugin then falls back
        # to parsing `codex resume <id>` out of the process args.
        assert self._lookup(tags_file, 9999) == ""


class TestCodexSessionTagsReviewFindings:
    """Fixes from the #208 branch review (Codex, 2026-08-02)."""

    CODEX_PID = 4242

    def _run(self, tmp_path, *, ancestor_pid=CODEX_PID, session=None, event="UserPromptSubmit"):
        session = session or CODEX_THREAD_A
        rollout = _write_tui_rollout(tmp_path, thread_id=session, name=f"rollout-{session}.jsonl")
        payload = {
            **CODEX_PAYLOAD_BASE,
            "session_id": session,
            "transcript_path": str(rollout),
            "hook_event_name": event,
        }
        state_dir = tmp_path / "states"
        state_dir.mkdir(exist_ok=True)
        with (
            patch("sys.stdin", StringIO(json.dumps(payload))),
            patch.dict("os.environ", {"TMUX_PANE": "%7"}, clear=False),
            patch("agent_hq.hooks.HOOK_STATE_DIR", state_dir),
            patch(
                "agent_hq.hooks.CODEX_SESSION_TAGS_FILE",
                tmp_path / "codex" / "session-tags.jsonl",
            ),
            patch("agent_hq.hooks._find_codex_ancestor_pid", return_value=ancestor_pid),
            patch("sys.argv", ["agent-hq-hook", "--harness", "codex"]),
            patch("sys.stdout", new_callable=StringIO),
        ):
            handle_hook_event()

    def _raw_lines(self, tmp_path):
        path = tmp_path / "codex" / "session-tags.jsonl"
        if not path.exists():
            return []
        return [line for line in path.read_text().splitlines() if line.strip()]

    def test_pid_reuse_reappearing_session_appends_again(self, tmp_path):
        """A(pid) -> B(pid reused) -> A(pid reused again): the last match for
        the pid must be A afterwards — dedup compares against the pid's LAST
        record, not membership anywhere in the file (review finding 1)."""
        tags_file = tmp_path / "codex" / "session-tags.jsonl"
        tags_file.parent.mkdir(parents=True)
        tags_file.write_text(
            json.dumps({"pid": self.CODEX_PID, "session": CODEX_THREAD_A}) + "\n"
            + json.dumps({"pid": self.CODEX_PID, "session": CODEX_THREAD_B}) + "\n"
        )
        self._run(tmp_path, session=CODEX_THREAD_A)
        lines = self._raw_lines(tmp_path)
        assert len(lines) == 3
        assert json.loads(lines[-1]) == {
            **json.loads(lines[-1]),
            "pid": self.CODEX_PID,
            "session": CODEX_THREAD_A,
        }

    def test_last_record_for_pid_matching_still_dedupes(self, tmp_path):
        tags_file = tmp_path / "codex" / "session-tags.jsonl"
        tags_file.parent.mkdir(parents=True)
        tags_file.write_text(
            json.dumps({"pid": 1111, "session": CODEX_THREAD_B}) + "\n"
            + json.dumps({"pid": self.CODEX_PID, "session": CODEX_THREAD_A}) + "\n"
        )
        self._run(tmp_path, session=CODEX_THREAD_A)
        assert len(self._raw_lines(tmp_path)) == 2

    def test_non_dict_json_line_does_not_suppress_tagging(self, tmp_path):
        """`42` parses as valid JSON but is not a dict — it must be skipped
        like garbage, not blow up the whole tag write (review finding 5)."""
        tags_file = tmp_path / "codex" / "session-tags.jsonl"
        tags_file.parent.mkdir(parents=True)
        tags_file.write_text('42\n"just a string"\n[1, 2]\n')
        self._run(tmp_path)
        lines = self._raw_lines(tmp_path)
        assert len(lines) == 4
        assert json.loads(lines[-1])["pid"] == self.CODEX_PID

    def test_oversized_preexisting_file_trimmed_even_when_deduped(self, tmp_path):
        """The cap applies on every write pass, including one that appends
        nothing because the tag already exists (review finding 4)."""
        from agent_hq.hooks import _SESSION_TAGS_MAX_LINES

        tags_file = tmp_path / "codex" / "session-tags.jsonl"
        tags_file.parent.mkdir(parents=True)
        filler = [
            json.dumps({"pid": 10000 + i, "session": f"s-{i}"})
            for i in range(_SESSION_TAGS_MAX_LINES + 100)
        ]
        filler.append(json.dumps({"pid": self.CODEX_PID, "session": CODEX_THREAD_A}))
        tags_file.write_text("\n".join(filler) + "\n")
        self._run(tmp_path, session=CODEX_THREAD_A)
        lines = self._raw_lines(tmp_path)
        assert len(lines) == _SESSION_TAGS_MAX_LINES
        assert json.loads(lines[-1])["pid"] == self.CODEX_PID  # newest survives

    def test_tag_write_happens_after_state_write(self, tmp_path):
        """The authoritative state write must never wait on tagging (review
        finding 3): at tag-call time the pane state file already carries
        this event's state."""
        seen = {}

        def spy(hook_input):
            state_file = tmp_path / "states" / "7.json"
            seen["state_written"] = (
                state_file.exists() and json.loads(state_file.read_text()).get("state") == "running"
            )

        with patch("agent_hq.hooks._record_codex_session_tag", side_effect=spy):
            self._run(tmp_path)
        assert seen == {"state_written": True}

    def test_tag_write_happens_after_identity_write_on_sessionstart(self, tmp_path):
        seen = {}

        def spy(hook_input):
            state_file = tmp_path / "states" / "7.json"
            seen["identity_written"] = (
                state_file.exists()
                and json.loads(state_file.read_text()).get("harness") == "codex"
            )

        with patch("agent_hq.hooks._record_codex_session_tag", side_effect=spy):
            self._run(tmp_path, event="SessionStart")
        assert seen == {"identity_written": True}


class TestStateFilePermissions:
    """Hook state carries the last assistant message, which can repeat
    secrets; files are private regardless of umask or an older 0644 file
    (#263 F6)."""

    def test_new_state_file_is_private(self, tmp_path):
        from agent_hq.hooks import _atomic_write

        target = tmp_path / "5.json"
        old = os.umask(0o022)
        try:
            _atomic_write(target, {"latest_summary": "secret"})
        finally:
            os.umask(old)
        assert target.stat().st_mode & 0o777 == 0o600

    def test_existing_world_readable_file_is_tightened(self, tmp_path):
        from agent_hq.hooks import _atomic_write

        target = tmp_path / "5.json"
        target.write_text("{}")
        target.chmod(0o644)
        _atomic_write(target, {"latest_summary": "secret"})
        assert target.stat().st_mode & 0o777 == 0o600
