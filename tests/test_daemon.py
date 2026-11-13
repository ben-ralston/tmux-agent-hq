"""Tests for daemon module."""

import os
import signal
from unittest.mock import MagicMock, patch

import pytest

from agent_hq.daemon import (
    _CACHE_WRITER_SAMPLES,
    daemon_status,
    find_orphan_daemon_pids,
    is_daemon_running,
    kill_orphan_daemons,
    read_pid,
    remove_pid,
    run_poll_loop,
    start_daemon,
    stop_daemon,
    write_pid,
)
from agent_hq.pinned_store import PinnedSessionsStore

_DASHBOARD_CMDLINE = (
    "/Users/me/.local/share/uv/tools/agent-hq/bin/python3 /Users/me/.local/bin/agent-hq --dashboard"
)


class _StubRuntimeStatusCollector:
    """``run_poll_loop`` starts a real ``RuntimeStatusCollector`` thread whose
    ``ps``/``claude`` subprocesses poll with ``time.sleep`` — the same global
    these tests patch to stop the loop. On a slow CI runner that thread ate
    the tick counter (or took the KeyboardInterrupt itself), cutting the
    daemon loop short. The collector has its own suite; stub it here."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def set_claude_present(self, present):
        pass


@pytest.fixture(autouse=True)
def _stub_runtime_status_collector(monkeypatch):
    monkeypatch.setattr(
        "agent_hq.status_collector.RuntimeStatusCollector", _StubRuntimeStatusCollector
    )


@pytest.fixture(autouse=True)
def _isolate_snapshot_file(tmp_path_factory, monkeypatch):
    """Redirect the durable snapshot default to tmp so the real ``run_poll_loop``
    exercised in this file never writes the user's real
    ``~/.config/agent-hq/agent-snapshots.jsonl``. #125"""
    snap = tmp_path_factory.mktemp("snap") / "agent-snapshots.jsonl"
    monkeypatch.setattr("agent_hq.snapshots._DEFAULT_SNAPSHOTS_FILE", snap)


class TestPidManagement:
    """Tests for PID file operations."""

    def test_write_pid(self, tmp_path):
        """Should write current PID to file."""
        pid_file = tmp_path / "daemon.pid"
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            write_pid(12345)
        assert pid_file.read_text().strip() == "12345"

    def test_read_pid(self, tmp_path):
        """Should read PID from file."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("12345\n")
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            assert read_pid() == 12345

    def test_read_pid_missing_file(self, tmp_path):
        """Should return None if PID file doesn't exist."""
        pid_file = tmp_path / "nonexistent.pid"
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            assert read_pid() is None

    def test_read_pid_corrupt_file(self, tmp_path):
        """Should return None for non-integer PID."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("not-a-pid\n")
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            assert read_pid() is None

    def test_remove_pid(self, tmp_path):
        """Should delete PID file."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("12345\n")
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            remove_pid()
        assert not pid_file.exists()

    def test_remove_pid_missing_file(self, tmp_path):
        """Should not raise if PID file doesn't exist."""
        pid_file = tmp_path / "nonexistent.pid"
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            remove_pid()  # Should not raise

    def test_is_daemon_running_with_live_process(self, tmp_path):
        """Should return True if PID file exists and process is alive."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text(f"{os.getpid()}\n")  # Current process is alive
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            assert is_daemon_running() is True

    def test_is_daemon_running_with_dead_process(self, tmp_path):
        """Should return False if PID file exists but process is dead."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("999999\n")
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            with patch("os.kill", side_effect=ProcessLookupError):
                assert is_daemon_running() is False

    def test_is_daemon_running_no_pid_file(self, tmp_path):
        """Should return False if no PID file."""
        pid_file = tmp_path / "nonexistent.pid"
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            assert is_daemon_running() is False


class TestPollLoop:
    """Tests for the polling loop (#73 reorder).

    Per tick the loop must:
    get_all_panes → discover_agents → enrich_all_agents →
    prune_stale_overrides → prune_stale_hook_state_files →
    read_overrides → resolve_phase_snapshot per agent → write_cache.
    """

    def _patches(self, *, raw_agents, agents, panes, overrides=None):
        """Context-manager bundle patching every poll-loop dependency."""
        from contextlib import ExitStack

        overrides = overrides if overrides is not None else ({}, 0)
        stack = ExitStack()
        mocks = {}
        mocks["get_all_panes"] = stack.enter_context(
            patch("agent_hq.daemon.get_all_panes", return_value=panes)
        )
        mocks["discover"] = stack.enter_context(
            patch("agent_hq.daemon.discover_agents", return_value=raw_agents)
        )
        mocks["enrich"] = stack.enter_context(
            patch("agent_hq.daemon.enrich_all_agents", return_value=agents)
        )
        mocks["prune_overrides"] = stack.enter_context(
            patch("agent_hq.daemon.prune_stale_overrides")
        )
        mocks["prune_hooks"] = stack.enter_context(
            patch("agent_hq.daemon.prune_stale_hook_state_files")
        )
        mocks["migrate_pins"] = stack.enter_context(patch("agent_hq.daemon.migrate_legacy_pins"))
        mocks["read_overrides"] = stack.enter_context(
            patch("agent_hq.daemon.read_overrides", return_value=overrides)
        )
        mocks["resolve"] = stack.enter_context(
            patch("agent_hq.daemon.resolve_phase_snapshot", return_value=MagicMock())
        )
        mocks["write_cache"] = stack.enter_context(patch("agent_hq.daemon.write_cache"))
        mocks["cleanup_stale"] = stack.enter_context(patch("agent_hq.daemon.cleanup_stale_entries"))
        mocks["cleanup_inactive"] = stack.enter_context(
            patch("agent_hq.daemon.cleanup_inactive_entries")
        )
        return stack, mocks

    def test_polls_and_writes_cache(self):
        """One tick: discover, enrich, prune both, read overrides, resolve, write."""
        raw_agents = [{"pane_id": "%5", "command": "claude"}]
        agents = [MagicMock(pane_id="%5")]
        panes = [{"pane_id": "%5"}]

        call_count = 0

        def stop_after_one(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise KeyboardInterrupt

        stack, m = self._patches(raw_agents=raw_agents, agents=agents, panes=panes)
        with stack:
            with patch("time.sleep", side_effect=stop_after_one):
                run_poll_loop(interval=2.0)

        m["discover"].assert_called_once()
        # First tick: no previous agents to stand in for a failed one (#358).
        m["enrich"].assert_called_once_with(raw_agents, previous={})
        m["prune_overrides"].assert_called_once_with(agents)
        # #205 Codex review: legacy-form pins are re-keyed on captured
        # stable ids each tick, right after the overrides prune.
        migrate_store, migrate_agents = m["migrate_pins"].call_args.args
        assert isinstance(migrate_store, PinnedSessionsStore)
        assert migrate_agents == agents
        m["prune_hooks"].assert_called_once()
        # active_pane_ids built from get_all_panes, agents_by_pane from agents.
        active_ids, agents_by_pane = m["prune_hooks"].call_args.args
        assert active_ids == {"%5"}
        assert agents_by_pane == {"%5": agents[0]}
        m["read_overrides"].assert_called_once()
        m["resolve"].assert_called_once()
        m["write_cache"].assert_called_once()
        # write_cache gets producer="daemon" + epoch/sequence/timestamp/monotonic_ns.
        _, kwargs = m["write_cache"].call_args
        assert kwargs["producer"] == "daemon"
        assert "producer_epoch" in kwargs
        assert kwargs["sequence"] == 1
        assert "monotonic_ns" in kwargs

    def test_tick_continues_and_writes_cache_when_prune_overrides_raises(self):
        """AHQ-014 follow-up: prune_stale_overrides now raises OSError on a
        genuine overrides.json read failure (instead of silently degrading
        to empty). That must cost only the prune, not the whole tick — a
        persistently unreadable overrides.json must not stop write_cache/
        snapshot_store from running every tick, which would be a bigger
        regression than the original clobber bug."""
        raw_agents = [{"pane_id": "%5", "command": "claude"}]
        agents = [MagicMock(pane_id="%5")]
        panes = [{"pane_id": "%5"}]

        def stop_after_one(*args, **kwargs):
            raise KeyboardInterrupt

        stack, m = self._patches(raw_agents=raw_agents, agents=agents, panes=panes)
        with stack:
            m["prune_overrides"].side_effect = OSError("cannot read overrides.json")
            with patch("time.sleep", side_effect=stop_after_one):
                run_poll_loop(interval=2.0)

        m["prune_overrides"].assert_called_once_with(agents)
        m["write_cache"].assert_called_once()

    def test_one_agents_enrichment_failure_does_not_abort_the_tick(self, tmp_path):
        """#358: a TypeError in one agent's enrichment used to escape
        ``enrich_all_agents`` and abort the whole tick, so cache.json stopped
        updating for every agent. Now the other agents are still written and
        the failing one keeps its previous git context."""
        import json

        from agent_hq.models import Agent, AgentState, AgentType

        def agent(pane_id, branch):
            return Agent(
                pane_id=pane_id, session_name="s", window_index=0, pane_index=0,
                agent_type=AgentType.CLAUDE, cwd="/w", repo_root="/w", repo_name="r",
                branch=branch, state=AgentState.IDLE, agent_pid=int(pane_id[1:]),
            )

        raw_agents = [
            {"pane_id": p, "session_name": "s", "window_index": 0, "pane_index": 0,
             "command": "claude", "agent_pid": int(p[1:])}
            for p in ("%1", "%2")
        ]
        ticks = {"n": 0}

        def enrich_agent(raw):
            if raw["pane_id"] == "%2" and ticks["n"] >= 1:
                raise TypeError('unexpected "<class \'_pygit2.Commit\'>"')
            return agent(raw["pane_id"], f"tick{ticks['n']}-{raw['pane_id'][1:]}")

        def sleep(*_args, **_kwargs):
            ticks["n"] += 1
            if ticks["n"] >= 2:
                raise KeyboardInterrupt

        cache_file = tmp_path / "cache.json"
        stack, m = self._patches(raw_agents=raw_agents, agents=[], panes=[{"pane_id": "%1"}])
        with stack:
            from agent_hq.enrichment import enrich_all_agents

            stack.enter_context(patch("agent_hq.daemon.enrich_all_agents", enrich_all_agents))
            stack.enter_context(patch("agent_hq.enrichment.enrich_agent", side_effect=enrich_agent))
            stack.enter_context(patch("agent_hq.enrichment.get_pane_cwd", return_value="/w"))
            m["resolve"].return_value = None
            from agent_hq.cache import write_cache

            stack.enter_context(patch("agent_hq.daemon.write_cache", write_cache))
            stack.enter_context(patch("agent_hq.cache.CACHE_FILE", cache_file))
            with patch("time.sleep", side_effect=sleep):
                run_poll_loop(interval=0.0)

        data = json.loads(cache_file.read_text())
        assert data["sequence"] == 2
        branches = {a["pane_id"]: a["branch"] for a in data["agents"]}
        assert branches == {"%1": "tick1-1", "%2": "tick0-2"}

    def test_orphan_set_uses_all_panes_not_just_agents(self):
        """Codex v5 finding: a tmux pane with no discovered agent must
        still be in ``active_pane_ids`` so its state file isn't unlinked
        as an orphan when discovery transiently miss-classifies it."""
        raw_agents = [{"pane_id": "%5", "command": "claude"}]
        agents = [MagicMock(pane_id="%5")]
        # %9 is a live pane with no discovered agent.
        panes = [{"pane_id": "%5"}, {"pane_id": "%9"}]

        def stop_after_one(*args, **kwargs):
            raise KeyboardInterrupt

        stack, m = self._patches(raw_agents=raw_agents, agents=agents, panes=panes)
        with stack:
            with patch("time.sleep", side_effect=stop_after_one):
                run_poll_loop(interval=2.0)

        active_ids, agents_by_pane = m["prune_hooks"].call_args.args
        assert active_ids == {"%5", "%9"}
        # %9 not a discovered agent → absent from agents_by_pane.
        assert "%9" not in agents_by_pane

    def test_sequence_increments_per_tick(self):
        """``_sequence`` bumps each tick; ``_epoch`` is fixed at loop start."""
        raw_agents = [{"pane_id": "%5", "command": "claude"}]
        agents = [MagicMock(pane_id="%5")]
        panes = [{"pane_id": "%5"}]

        ticks = 0

        def stop_after_three(*args, **kwargs):
            nonlocal ticks
            ticks += 1
            if ticks >= 3:
                raise KeyboardInterrupt

        stack, m = self._patches(raw_agents=raw_agents, agents=agents, panes=panes)
        with stack:
            with patch("time.sleep", side_effect=stop_after_three):
                run_poll_loop(interval=0.0)

        seqs = [c.kwargs["sequence"] for c in m["write_cache"].call_args_list]
        assert seqs == [1, 2, 3]
        epochs = {c.kwargs["producer_epoch"] for c in m["write_cache"].call_args_list}
        assert len(epochs) == 1  # stable across ticks

    def test_resolve_called_per_agent(self):
        raw_agents = [{"pane_id": "%5", "command": "claude"},
                      {"pane_id": "%6", "command": "codex"}]
        agents = [MagicMock(pane_id="%5"), MagicMock(pane_id="%6")]
        panes = [{"pane_id": "%5"}, {"pane_id": "%6"}]

        def stop_after_one(*args, **kwargs):
            raise KeyboardInterrupt

        stack, m = self._patches(raw_agents=raw_agents, agents=agents, panes=panes)
        with stack:
            with patch("time.sleep", side_effect=stop_after_one):
                run_poll_loop(interval=2.0)

        assert m["resolve"].call_count == 2

    def test_state_cleanup_runs_on_first_tick_with_all_pane_ids(self):
        """#13: the long-running daemon must prune stale access/inactive
        entries. The valid set is ALL live panes (not just discovered
        agents) so a transiently miss-classified pane keeps its entries."""
        raw_agents = [{"pane_id": "%5", "command": "claude"}]
        agents = [MagicMock(pane_id="%5")]
        # %9 is a live pane with no discovered agent.
        panes = [{"pane_id": "%5"}, {"pane_id": "%9"}]

        def stop_after_one(*args, **kwargs):
            raise KeyboardInterrupt

        stack, m = self._patches(raw_agents=raw_agents, agents=agents, panes=panes)
        with stack:
            with patch("time.sleep", side_effect=stop_after_one):
                run_poll_loop(interval=2.0)

        m["cleanup_stale"].assert_called_once_with({"%5", "%9"})
        m["cleanup_inactive"].assert_called_once_with({"%5", "%9"})

    def test_state_cleanup_gated_to_every_n_ticks(self):
        """#13: cleanup fires on tick 1, then every STATE_CLEANUP_TICKS
        ticks — not on every 2-second tick (needless disk I/O)."""
        from agent_hq.daemon import STATE_CLEANUP_TICKS

        raw_agents = [{"pane_id": "%5", "command": "claude"}]
        agents = [MagicMock(pane_id="%5")]
        panes = [{"pane_id": "%5"}]

        ticks = 0
        total = STATE_CLEANUP_TICKS + 1  # tick 1 fires, tick N+1 fires again

        def stop_after_total(*args, **kwargs):
            nonlocal ticks
            ticks += 1
            if ticks >= total:
                raise KeyboardInterrupt

        stack, m = self._patches(raw_agents=raw_agents, agents=agents, panes=panes)
        with stack:
            with patch("time.sleep", side_effect=stop_after_total):
                run_poll_loop(interval=0.0)

        assert m["cleanup_stale"].call_count == 2
        assert m["cleanup_inactive"].call_count == 2

    def test_state_cleanup_skipped_when_no_panes(self):
        """#13: a transiently empty pane set must not wipe every entry
        (same lesson as #118/#125 — empty rosters are treated as suspect)."""

        def stop_after_one(*args, **kwargs):
            raise KeyboardInterrupt

        stack, m = self._patches(raw_agents=[], agents=[], panes=[])
        with stack:
            with patch("time.sleep", side_effect=stop_after_one):
                run_poll_loop(interval=2.0)

        m["cleanup_stale"].assert_not_called()
        m["cleanup_inactive"].assert_not_called()

    def test_continues_on_discovery_error(self, caplog):
        """An exception mid-tick keeps the loop going AND is logged at
        WARNING (not silently swallowed at DEBUG) — a wedged daemon must
        be diagnosable from the default-level log."""
        call_count = 0

        def fail_then_succeed(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("tmux failed")
            return []

        def stop_after_two(*args, **kwargs):
            if call_count >= 2:
                raise KeyboardInterrupt

        with (
            patch("agent_hq.daemon.get_all_panes", return_value=[]),
            patch("agent_hq.daemon.discover_agents", side_effect=fail_then_succeed),
            patch("agent_hq.daemon.enrich_all_agents", return_value=[]),
            patch("agent_hq.daemon.prune_stale_overrides"),
            patch("agent_hq.daemon.prune_stale_hook_state_files"),
            patch("agent_hq.daemon.read_overrides", return_value=({}, 0)),
            patch("agent_hq.daemon.write_cache") as mock_write,
            patch("time.sleep", side_effect=stop_after_two),
            caplog.at_level("WARNING", logger="agent_hq.daemon"),
        ):
            run_poll_loop(interval=1.0)

        # Wrote cache once (second iteration succeeded).
        assert mock_write.call_count == 1
        assert any(
            r.levelname == "WARNING" and "poll loop tick failed" in r.getMessage()
            for r in caplog.records
        )


class TestDaemonLifecycle:
    """Tests for daemon start/stop/status."""

    def test_start_refuses_if_already_running(self, tmp_path, capsys):
        """Should refuse to start if daemon is already running."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text(f"{os.getpid()}\n")
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            result = start_daemon(interval=2.0)
        assert result == 1
        assert "already running" in capsys.readouterr().err.lower()

    def test_stop_when_not_running(self, tmp_path, capsys):
        """Should report that daemon is not running."""
        pid_file = tmp_path / "nonexistent.pid"
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            result = stop_daemon()
        assert result == 1
        assert "not running" in capsys.readouterr().err.lower()

    def test_stop_sends_sigterm(self, tmp_path, capsys):
        """Should send SIGTERM to daemon process."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("12345\n")
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            with (
                patch("os.kill") as mock_kill,
                patch("agent_hq.daemon._ps_cmdline", return_value=_DASHBOARD_CMDLINE),
            ):
                mock_kill.return_value = None
                result = stop_daemon()
        assert result == 0
        mock_kill.assert_any_call(12345, signal.SIGTERM)

    def test_stop_refuses_to_signal_a_non_agent_hq_process(self, tmp_path, capsys):
        """A PID file naming some other live process (a reused PID, or one
        planted in a shared /tmp) must never be signalled (#263 F4)."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("12345\n")
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            with (
                patch("os.kill") as mock_kill,
                patch("agent_hq.daemon._ps_cmdline", return_value="/opt/homebrew/bin/tmux"),
            ):
                result = stop_daemon()
        assert result == 1
        assert not any(c.args[1] == signal.SIGTERM for c in mock_kill.call_args_list)
        assert not pid_file.exists()
        assert "not an agent-hq process" in capsys.readouterr().err

    def test_stop_fails_closed_when_ps_cannot_answer(self, tmp_path, capsys):
        """A ps timeout says nothing about the PID: keep the PID file and
        signal nothing, never orphan a live daemon (#263 review)."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("12345\n")
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            with (
                patch("os.kill") as mock_kill,
                patch("agent_hq.daemon._ps_cmdline", return_value=None),
            ):
                result = stop_daemon()
        assert result == 1
        assert not any(c.args[1] == signal.SIGTERM for c in mock_kill.call_args_list)
        assert pid_file.exists()
        assert "could not verify" in capsys.readouterr().err.lower()

    def test_stop_signals_daemons_forked_from_any_agent_hq_entry_point(self, tmp_path):
        """The daemon is forked from whichever agent-hq process started it,
        so its argv may read ``web serve`` or ``--dashboard``."""
        for cmdline in (
            _DASHBOARD_CMDLINE,
            "/u/.venv/bin/python3 /u/.local/bin/agent-hq web serve --bind 100.64.0.1",
            "/usr/bin/python3 -m agent_hq daemon start",
        ):
            pid_file = tmp_path / "daemon.pid"
            pid_file.write_text("12345\n")
            with patch("agent_hq.daemon.PID_FILE", pid_file):
                with (
                    patch("os.kill") as mock_kill,
                    patch("agent_hq.daemon._ps_cmdline", return_value=cmdline),
                ):
                    assert stop_daemon() == 0, cmdline
            mock_kill.assert_any_call(12345, signal.SIGTERM)

    def test_write_pid_does_not_follow_a_planted_symlink(self, tmp_path):
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me")
        pid_file = tmp_path / "daemon.pid"
        pid_file.symlink_to(victim)
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            write_pid(4242)
        assert victim.read_text() == "keep me"
        assert not pid_file.is_symlink()
        assert pid_file.read_text().strip() == "4242"

    def test_status_when_running(self, tmp_path, capsys):
        """Should report daemon status when running."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text(f"{os.getpid()}\n")
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            result = daemon_status()
        assert result == 0
        output = capsys.readouterr().out
        assert "running" in output.lower()

    def test_status_when_stopped(self, tmp_path, capsys):
        """Should report daemon is stopped."""
        pid_file = tmp_path / "nonexistent.pid"
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            result = daemon_status()
        assert result == 1
        output = capsys.readouterr().out
        assert "not running" in output.lower()

    def test_start_silent_suppresses_success_output(self, capsys):
        """Silent start should not print parent success message."""
        with (
            patch("agent_hq.daemon.is_daemon_running", return_value=False),
            patch("agent_hq.daemon.remove_pid"),
            patch("agent_hq.daemon.os.fork", return_value=12345),
        ):
            result = start_daemon(interval=2.0, silent=True)

        assert result == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_start_silent_suppresses_already_running_output(self, capsys):
        """Silent start should not print already-running message."""
        with (
            patch("agent_hq.daemon.is_daemon_running", return_value=True),
            patch("agent_hq.daemon.read_pid", return_value=12345),
        ):
            result = start_daemon(interval=2.0, silent=True)

        assert result == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestDaemonEdgeCases:
    """Edge case tests for daemon operations."""

    def test_start_cleans_stale_pid_file(self, tmp_path):
        """Should detect stale PID when old daemon is dead."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("999999\n")
        with patch("agent_hq.daemon.PID_FILE", pid_file):
            with patch("os.kill", side_effect=ProcessLookupError):
                running = is_daemon_running()
        assert running is False

    def test_stop_handles_race_condition(self, tmp_path, capsys):
        """Should handle process dying between check and kill."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("12345\n")

        def mock_kill(pid, sig):
            if sig == 0:
                return  # Process exists check succeeds
            raise ProcessLookupError  # But dies before SIGTERM arrives

        with patch("agent_hq.daemon.PID_FILE", pid_file):
            with (
                patch("os.kill", side_effect=mock_kill),
                patch("agent_hq.daemon._ps_cmdline", return_value=_DASHBOARD_CMDLINE),
            ):
                result = stop_daemon()
        assert result == 0  # Should still succeed


class TestOrphanCleanup:
    """find_orphan_daemon_pids + kill_orphan_daemons + start integration."""

    def _fake_pgrep(self, pids: list[int]) -> MagicMock:
        """Build a MagicMock subprocess.run result that lists ``pids`` like pgrep."""
        result = MagicMock()
        result.stdout = "\n".join(
            f"{pid} /opt/Python /Users/x/agent-hq daemon start" for pid in pids
        )
        return result

    def test_find_orphan_pids_excludes_self(self):
        """Our own PID must be filtered out even if pgrep returns it."""
        self_pid = os.getpid()
        with (
            patch(
                "agent_hq.daemon.subprocess.run",
                return_value=self._fake_pgrep([self_pid, 111, 222]),
            ),
            patch("agent_hq.daemon._sample_cache_writer_pids", return_value=[]),
        ):
            assert find_orphan_daemon_pids() == [111, 222]

    def test_find_orphan_pids_returns_empty_on_pgrep_error(self):
        """pgrep missing or failing must not propagate — startup hook is best-effort."""
        with (
            patch("agent_hq.daemon.subprocess.run", side_effect=FileNotFoundError),
            patch("agent_hq.daemon._sample_cache_writer_pids", return_value=[]),
        ):
            assert find_orphan_daemon_pids() == []

    def test_find_orphan_pids_returns_empty_on_no_matches(self):
        """pgrep exits 1 with empty stdout when nothing matches; that's normal."""
        result = MagicMock()
        result.stdout = ""
        with (
            patch("agent_hq.daemon.subprocess.run", return_value=result),
            patch("agent_hq.daemon._sample_cache_writer_pids", return_value=[]),
        ):
            assert find_orphan_daemon_pids() == []

    def test_find_orphan_pids_includes_cache_writer(self):
        """Cache-writer detection catches dashboard-forked daemons that pgrep misses."""
        result = MagicMock()
        result.stdout = ""  # pgrep finds nothing
        with (
            patch("agent_hq.daemon.subprocess.run", return_value=result),
            patch("agent_hq.daemon._sample_cache_writer_pids", return_value=[333]),
        ):
            assert find_orphan_daemon_pids() == [333]

    def test_find_orphan_pids_dedupes_pgrep_and_cache(self):
        """A PID found by both signals appears only once."""
        with (
            patch(
                "agent_hq.daemon.subprocess.run",
                return_value=self._fake_pgrep([111, 222]),
            ),
            patch("agent_hq.daemon._sample_cache_writer_pids", return_value=[222, 333]),
        ):
            assert find_orphan_daemon_pids() == [111, 222, 333]

    def test_find_orphan_pids_filters_shell_self_match(self):
        """Bash/sh shells whose argv contains the pgrep regex must NOT be killed.

        Reproduces a live bug: ``pgrep -af "agent-hq.*daemon"`` matches the
        bash process whose command line is the pgrep invocation itself.
        We filter by checking the first argv token is a Python interpreter.
        """
        result = MagicMock()
        result.stdout = (
            "111 /opt/Python /Users/x/agent-hq daemon start\n"
            "222 /bin/bash -c pgrep -af agent-hq.*daemon\n"
            "333 /opt/Python /Users/x/agent-hq daemon start"
        )
        with (
            patch("agent_hq.daemon.subprocess.run", return_value=result),
            patch("agent_hq.daemon._sample_cache_writer_pids", return_value=[]),
        ):
            assert find_orphan_daemon_pids() == [111, 333]

    def test_kill_orphan_daemons_sigterms_and_returns_pids(self):
        """SIGTERM is sent to every orphan; return list matches input."""
        with (
            patch("agent_hq.daemon.find_orphan_daemon_pids", return_value=[111, 222]),
            patch("agent_hq.daemon.os.kill") as mock_kill,
            patch("agent_hq.daemon._process_alive", return_value=False),
        ):
            killed = kill_orphan_daemons(silent=True)

        assert killed == [111, 222]
        mock_kill.assert_any_call(111, signal.SIGTERM)
        mock_kill.assert_any_call(222, signal.SIGTERM)

    def test_kill_orphan_daemons_escalates_to_sigkill(self):
        """If a process refuses to exit after SIGTERM, escalate to SIGKILL."""
        with (
            patch("agent_hq.daemon.find_orphan_daemon_pids", return_value=[111]),
            patch("agent_hq.daemon.os.kill") as mock_kill,
            # Process stays alive through the whole TERM wait
            patch("agent_hq.daemon._process_alive", return_value=True),
            patch("agent_hq.daemon.time.sleep"),
            # Make the deadline expire immediately to skip the 2s wait in tests
            patch(
                "agent_hq.daemon.time.monotonic",
                side_effect=[0.0, 9999.0, 9999.0, 9999.0],
            ),
        ):
            kill_orphan_daemons(silent=True)

        mock_kill.assert_any_call(111, signal.SIGTERM)
        mock_kill.assert_any_call(111, signal.SIGKILL)

    def test_kill_orphan_daemons_prints_summary_when_not_silent(self, capsys):
        """Non-silent mode prints to stderr so the user sees the cleanup."""
        with (
            patch("agent_hq.daemon.find_orphan_daemon_pids", return_value=[111, 222]),
            patch("agent_hq.daemon.os.kill"),
            patch("agent_hq.daemon._process_alive", return_value=False),
        ):
            kill_orphan_daemons(silent=False)

        err = capsys.readouterr().err
        assert "Killed 2 orphan" in err
        assert "111" in err and "222" in err

    def test_kill_orphan_daemons_silent_suppresses_print(self, capsys):
        """Silent mode (used by run_dashboard auto-start) writes nothing to stderr."""
        with (
            patch("agent_hq.daemon.find_orphan_daemon_pids", return_value=[111]),
            patch("agent_hq.daemon.os.kill"),
            patch("agent_hq.daemon._process_alive", return_value=False),
        ):
            kill_orphan_daemons(silent=True)

        assert capsys.readouterr().err == ""

    def test_kill_orphan_daemons_returns_empty_when_none_found(self):
        """No orphans → no print, no kill, empty return."""
        with (
            patch("agent_hq.daemon.find_orphan_daemon_pids", return_value=[]),
            patch("agent_hq.daemon.os.kill") as mock_kill,
        ):
            assert kill_orphan_daemons(silent=False) == []
        mock_kill.assert_not_called()

    def test_start_daemon_kills_orphans_before_fork(self):
        """start_daemon must cull orphans before forking the new daemon."""
        with (
            patch("agent_hq.daemon.is_daemon_running", return_value=False),
            patch("agent_hq.daemon.remove_pid"),
            patch("agent_hq.daemon.kill_orphan_daemons", return_value=[999]) as mock_kill,
            patch("agent_hq.daemon.os.fork", return_value=12345) as mock_fork,
        ):
            start_daemon(interval=2.0, silent=True)

        # Cleanup runs (no legit daemon to preserve), then fork happens
        mock_kill.assert_called_once_with(silent=True, exclude_pid=None)
        mock_fork.assert_called_once()

    def test_validator_accepts_canonical_daemon_cmdlines(self):
        """Real daemon cmdlines (start + dashboard variants) must validate."""
        from agent_hq.daemon import _validate_daemon_cmdline

        assert _validate_daemon_cmdline(
            "/Users/u/.venv/bin/python3 /Users/u/.venv/bin/agent-hq daemon start"
        )
        assert _validate_daemon_cmdline("/opt/Python /Users/u/.venv/bin/agent-hq --dashboard")
        assert _validate_daemon_cmdline("/usr/bin/python -m agent_hq daemon start")

    def test_validator_rejects_unrelated_python_processes(self):
        """A pytest run, REPL, or unrelated script must not look like a daemon —
        even if its argv mentions agent-hq and daemon as plain words."""
        from agent_hq.daemon import _validate_daemon_cmdline

        # The exact PID-reuse footgun: a Python process whose argv
        # incidentally contains the same words pgrep matches on.
        assert not _validate_daemon_cmdline(
            "/usr/bin/python /tmp/myscript.py talking about agent-hq daemon"
        )
        # bash self-match — first token isn't python
        assert not _validate_daemon_cmdline("/bin/bash -c pgrep -af agent-hq.*daemon")
        # Python with agent-hq token but no daemon/--dashboard role
        assert not _validate_daemon_cmdline(
            "/usr/bin/python /Users/u/.venv/bin/agent-hq --dashboard-status"
        )
        # Python with daemon token but no agent-hq token
        assert not _validate_daemon_cmdline("/usr/bin/python /tmp/other-tool daemon start")

    def test_find_orphan_pids_excludes_unvalidated_cache_pid(self, tmp_path):
        """A cache.json PID belonging to a recycled (non-daemon) process must
        NOT be included in the kill list. This is the PID-reuse footgun."""
        cache = tmp_path / "cache.json"
        cache.write_text("{}")  # exists so the fast-path proceeds to polling
        result = MagicMock()
        result.stdout = ""  # pgrep finds nothing
        with (
            patch("agent_hq.daemon.subprocess.run", return_value=result),
            patch("agent_hq.daemon.CACHE_FILE", cache),
            patch("agent_hq.daemon._read_cache_writer_pid", return_value=4242),
            # ps reports the PID is now an unrelated process.
            patch(
                "agent_hq.daemon._ps_cmdline",
                return_value="/usr/bin/python /tmp/totally-different.py",
            ),
            patch("agent_hq.daemon.time.sleep"),  # skip 1.5s wait
        ):
            assert find_orphan_daemon_pids() == []

    def test_find_orphan_pids_includes_validated_cache_pid(self, tmp_path):
        """A cache.json PID whose process IS a real daemon must be included."""
        cache = tmp_path / "cache.json"
        cache.write_text("{}")
        result = MagicMock()
        result.stdout = ""
        with (
            patch("agent_hq.daemon.subprocess.run", return_value=result),
            patch("agent_hq.daemon.CACHE_FILE", cache),
            patch("agent_hq.daemon._read_cache_writer_pid", return_value=777),
            patch(
                "agent_hq.daemon._ps_cmdline",
                return_value="/opt/Python /Users/u/.venv/bin/agent-hq --dashboard",
            ),
            patch("agent_hq.daemon.time.sleep"),
        ):
            assert find_orphan_daemon_pids() == [777]

    def test_sample_cache_writer_skips_polling_when_clean_state(self, tmp_path):
        """Clean-state launch (no PID file, no cache file) → no polling.
        Saves 1.5s on every dashboard start in the common case."""
        from agent_hq.daemon import _sample_cache_writer_pids

        with (
            patch("agent_hq.daemon.PID_FILE", tmp_path / "missing.pid"),
            patch("agent_hq.daemon.CACHE_FILE", tmp_path / "missing.json"),
            patch("agent_hq.daemon.time.sleep") as mock_sleep,
            patch("agent_hq.daemon._read_cache_writer_pid") as mock_read,
        ):
            assert _sample_cache_writer_pids(set()) == []
        mock_sleep.assert_not_called()
        mock_read.assert_not_called()

    def test_sample_cache_writer_polls_when_cache_exists(self, tmp_path):
        """If cache.json exists, polling proceeds — there might be a writer PID."""
        from agent_hq.daemon import _sample_cache_writer_pids

        cache = tmp_path / "cache.json"
        cache.write_text("{}")
        with (
            patch("agent_hq.daemon.PID_FILE", tmp_path / "missing.pid"),
            patch("agent_hq.daemon.CACHE_FILE", cache),
            patch("agent_hq.daemon.time.sleep"),
            patch("agent_hq.daemon._read_cache_writer_pid", return_value=None) as mock_read,
        ):
            _sample_cache_writer_pids(set())
        # Polled even though every read returned None.
        assert mock_read.call_count == _CACHE_WRITER_SAMPLES

    def test_pgrep_filter_rejects_unrelated_python_match(self):
        """pgrep can match an unrelated python process whose argv contains
        'agent-hq' and 'daemon' as words. The validator must reject it."""
        result = MagicMock()
        result.stdout = (
            "111 /usr/bin/python /Users/u/.venv/bin/agent-hq daemon start\n"
            "222 /usr/bin/python /tmp/other.py talking about agent-hq daemon\n"
        )
        with (
            patch("agent_hq.daemon.subprocess.run", return_value=result),
            patch("agent_hq.daemon._sample_cache_writer_pids", return_value=[]),
        ):
            assert find_orphan_daemon_pids() == [111]

    def test_start_daemon_kills_orphans_even_when_legit_running(self):
        """An already-running legit daemon doesn't suppress orphan cleanup.

        Without this, dashboard-forked sibling daemons would keep racing
        the legit daemon on cache.json forever, since `start` sees the
        PID file matches a live process and returns early.
        """
        with (
            patch("agent_hq.daemon.is_daemon_running", return_value=True),
            patch("agent_hq.daemon.read_pid", return_value=42),
            patch("agent_hq.daemon.kill_orphan_daemons", return_value=[]) as mock_kill,
            patch("agent_hq.daemon.os.fork") as mock_fork,
        ):
            result = start_daemon(interval=2.0, silent=True)

        # Cleanup runs with legit_pid excluded, no fork happens
        mock_kill.assert_called_once_with(silent=True, exclude_pid=42)
        mock_fork.assert_not_called()
        assert result == 1  # already-running exit code


class TestEnsureRunning:
    """Tests for daemon.ensure_running() — the idempotent web-launch helper.

    The web server calls this at boot so a fresh launch from any worktree
    Just Works. The lock-based serialization is what protects two
    simultaneous ``agent-hq web`` launches from racing on ``start_daemon``
    and producing duplicate writers.
    """

    def test_returns_true_when_already_running(self):
        """Daemon is already alive → no fork, return True immediately."""
        from agent_hq.daemon import ensure_running

        with (
            patch("agent_hq.daemon.is_daemon_running", return_value=True),
            patch("agent_hq.daemon.start_daemon") as mock_start,
        ):
            assert ensure_running() is True
        mock_start.assert_not_called()

    def test_starts_daemon_when_not_running(self, tmp_path):
        """No daemon → acquire lock, start, return True on success."""
        from agent_hq.daemon import ensure_running

        lock_file = tmp_path / "daemon.lock"
        with (
            patch("agent_hq.daemon.is_daemon_running", side_effect=[False, False]),
            patch("agent_hq.daemon.start_daemon", return_value=0) as mock_start,
            patch("agent_hq.daemon.DAEMON_LOCK_FILE", lock_file),
        ):
            result = ensure_running()

        assert result is True
        mock_start.assert_called_once()

    def test_returns_false_when_start_daemon_fails(self, tmp_path):
        """start_daemon returns nonzero → ensure_running returns False."""
        from agent_hq.daemon import ensure_running

        lock_file = tmp_path / "daemon.lock"
        with (
            patch("agent_hq.daemon.is_daemon_running", side_effect=[False, False]),
            patch("agent_hq.daemon.start_daemon", return_value=1),
            patch("agent_hq.daemon.DAEMON_LOCK_FILE", lock_file),
        ):
            result = ensure_running()

        assert result is False

    def test_double_check_after_lock(self, tmp_path):
        """If a competing caller starts the daemon while we wait for the
        lock, the second caller's post-lock recheck finds the daemon up
        and skips the duplicate ``start_daemon``."""
        from agent_hq.daemon import ensure_running

        lock_file = tmp_path / "daemon.lock"
        # First check (before lock): not running
        # Second check (after lock): running (someone else started it)
        with (
            patch(
                "agent_hq.daemon.is_daemon_running",
                side_effect=[False, True],
            ),
            patch("agent_hq.daemon.start_daemon") as mock_start,
            patch("agent_hq.daemon.DAEMON_LOCK_FILE", lock_file),
        ):
            assert ensure_running() is True

        mock_start.assert_not_called()

    def test_concurrent_callers_serialized(self, tmp_path):
        """Two threads calling ``ensure_running`` simultaneously should
        result in exactly one ``start_daemon`` call. The advisory lock
        serializes them; whichever loses the race sees ``is_daemon_running``
        return True on the post-lock recheck."""
        import threading

        from agent_hq.daemon import ensure_running

        lock_file = tmp_path / "daemon.lock"
        start_count = 0
        start_count_lock = threading.Lock()
        daemon_running = [False]

        def fake_start_daemon(*args, **kwargs):
            nonlocal start_count
            with start_count_lock:
                start_count += 1
            # simulate daemon coming up
            daemon_running[0] = True
            return 0

        def fake_is_running():
            return daemon_running[0]

        with (
            patch("agent_hq.daemon.is_daemon_running", side_effect=fake_is_running),
            patch("agent_hq.daemon.start_daemon", side_effect=fake_start_daemon),
            patch("agent_hq.daemon.DAEMON_LOCK_FILE", lock_file),
        ):
            results = []

            def call():
                results.append(ensure_running())

            t1 = threading.Thread(target=call)
            t2 = threading.Thread(target=call)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        assert all(r is True for r in results)
        assert start_count == 1


class TestPgrepPlatformFlags:
    """#170 audit AHQ-008: BSD/macOS pgrep -a means 'include ancestors' and
    prints PID-only lines, so every line failed the two-part split and the
    pgrep orphan source silently returned []. ``-fl`` prints 'PID full-cmdline'
    on both BSD and procps."""

    def test_pgrep_uses_fl_flags(self):
        from agent_hq.daemon import _pgrep_daemon_pids

        result = MagicMock()
        result.stdout = "123 /opt/python /Users/x/bin/agent-hq daemon start\n"
        with patch("agent_hq.daemon.subprocess.run", return_value=result) as run:
            pids = _pgrep_daemon_pids(excluded=set())
        cmd = run.call_args[0][0]
        assert "-fl" in cmd
        assert "-af" not in cmd and "-a" not in cmd
        assert pids == [123]


class TestChildDeregistration:
    """#170 audit AHQ-007: a SIGTERM'd orphan daemon exiting gracefully must
    not delete the live singleton's PID file and cache."""

    def test_owner_deregisters(self, tmp_path):
        from agent_hq.daemon import _deregister_if_owner

        pid_file = tmp_path / "daemon.pid"
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("{}")
        with (
            patch("agent_hq.daemon.PID_FILE", pid_file),
            patch("agent_hq.cache.CACHE_FILE", cache_file),
        ):
            write_pid(os.getpid())
            _deregister_if_owner()
        assert not pid_file.exists()
        assert not cache_file.exists()

    def test_non_owner_leaves_registration_intact(self, tmp_path):
        from agent_hq.daemon import _deregister_if_owner

        pid_file = tmp_path / "daemon.pid"
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("{}")
        with (
            patch("agent_hq.daemon.PID_FILE", pid_file),
            patch("agent_hq.cache.CACHE_FILE", cache_file),
        ):
            write_pid(99999999)  # another process owns the registration
            _deregister_if_owner()
            assert pid_file.exists(), "non-owner must not delete the owner's PID file"
            assert cache_file.exists(), "non-owner must not delete the owner's cache"

    def test_missing_pid_file_still_cleans_cache(self, tmp_path):
        """stop_daemon unlinks the PID file before SIGTERM lands, so the
        dying owner sees no PID file — it must still remove the cache
        (Codex review of AHQ-007)."""
        from agent_hq.daemon import _deregister_if_owner

        pid_file = tmp_path / "daemon.pid"
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("{}")
        with (
            patch("agent_hq.daemon.PID_FILE", pid_file),
            patch("agent_hq.cache.CACHE_FILE", cache_file),
        ):
            _deregister_if_owner()
        assert not cache_file.exists()
