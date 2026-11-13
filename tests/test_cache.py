"""Tests for discovery cache module."""

import json
import os
import time
from unittest.mock import patch

from agent_hq.cache import SCHEMA_VERSION, get_agents, read_cache, write_cache
from agent_hq.cli import create_parser
from agent_hq.models import Agent, AgentState, AgentType


def _v2_payload(agents, *, timestamp=None):
    """Hand-built v2 cache envelope for tests that need a custom timestamp."""
    return {
        "schema_version": SCHEMA_VERSION,
        "producer": "daemon",
        "producer_epoch": timestamp if timestamp is not None else time.time(),
        "sequence": 1,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "monotonic_ns": time.monotonic_ns(),
        "pid": os.getpid(),
        "agents": [a.to_dict() for a in agents],
    }


def _make_agent(pane_id="%5", state=AgentState.RUNNING) -> Agent:
    """Helper to create a test Agent."""
    return Agent(
        pane_id=pane_id,
        session_name="main",
        window_index=1,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd="/home/user/project",
        repo_root="/home/user/project",
        repo_name="my-project",
        branch="main",
        worktree_name=None,
        raw_command="claude",
        state=state,
    )


class TestWriteCache:
    """Tests for write_cache()."""

    def test_writes_json_file(self, tmp_path):
        """Should write agents to a JSON cache file with the v2 envelope."""
        cache_file = tmp_path / "cache.json"
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            agents = [_make_agent("%5"), _make_agent("%8")]
            write_cache(
                agents,
                producer="daemon",
                producer_epoch=123.0,
                sequence=42,
                timestamp=456.0,
                monotonic_ns=789,
            )

        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        assert data["schema_version"] == SCHEMA_VERSION
        assert data["producer"] == "daemon"
        assert data["producer_epoch"] == 123.0
        assert data["sequence"] == 42
        assert data["timestamp"] == 456.0
        assert data["monotonic_ns"] == 789
        assert "pid" in data
        assert len(data["agents"]) == 2
        assert data["agents"][0]["pane_id"] == "%5"

    def test_atomic_write(self, tmp_path):
        """Should use atomic write (no partial reads)."""
        cache_file = tmp_path / "cache.json"
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            write_cache([_make_agent()])

        # tmp file should not exist after write completes
        tmp_file = tmp_path / "cache.json.tmp"
        assert not tmp_file.exists()
        assert cache_file.exists()

    def test_concurrent_writers_never_fail_or_publish_partial_json(self, tmp_path):
        """Two daemons writing at once (#317): no FileNotFoundError, no torn file.

        2026-10-18..20 an orphan daemon ran beside the live one. Both wrote
        through one shared ``cache.json.tmp``: one's rename found the temp
        already moved (FileNotFoundError, the sentinel's "daemon poll loop
        failing"), and a rename could publish a temp the other was still
        writing — a reader then saw a torn cache.json, i.e. zero agents (#338).
        """
        import threading

        cache_file = tmp_path / "cache.json"
        agents = [_make_agent(f"%{n}") for n in range(60)]
        errors: list[BaseException] = []
        torn: list[str] = []
        stop = threading.Event()

        def writer() -> None:
            try:
                for _ in range(200):
                    write_cache(agents)
            except BaseException as exc:  # noqa: BLE001 - recorded for the assert
                errors.append(exc)

        def reader() -> None:
            while not stop.is_set():
                try:
                    raw = cache_file.read_text()
                except FileNotFoundError:
                    continue
                try:
                    json.loads(raw)
                except ValueError:
                    torn.append(raw[:80])

        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            write_cache(agents)
            threads = [threading.Thread(target=writer) for _ in range(2)]
            watcher = threading.Thread(target=reader)
            watcher.start()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            stop.set()
            watcher.join()

        assert errors == []
        assert torn == []
        assert len(json.loads(cache_file.read_text())["agents"]) == 60
        assert sorted(p.name for p in tmp_path.iterdir()) == ["cache.json"]

    def test_creates_parent_directory(self, tmp_path):
        """Should create parent directory if it doesn't exist."""
        cache_file = tmp_path / "subdir" / "cache.json"
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            write_cache([_make_agent()])

        assert cache_file.exists()


class TestReadCache:
    """Tests for read_cache()."""

    def test_reads_valid_cache(self, tmp_path):
        """Should read and deserialize a valid cache file with its timestamp."""
        cache_file = tmp_path / "cache.json"
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            write_cache([_make_agent("%5"), _make_agent("%8")])
            result = read_cache()

        assert result is not None
        agents, ts = result
        assert len(agents) == 2
        assert agents[0].pane_id == "%5"
        assert agents[0].agent_type == AgentType.CLAUDE
        assert agents[0].state == AgentState.RUNNING
        assert ts > 0

    def test_returns_none_for_missing_file(self, tmp_path):
        """Should return None if cache file doesn't exist."""
        cache_file = tmp_path / "nonexistent.json"
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            assert read_cache() is None

    def test_returns_none_for_stale_cache(self, tmp_path):
        """Should return None if cache is older than max_age."""
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(
            json.dumps(_v2_payload([_make_agent()], timestamp=time.time() - 10.0))
        )

        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            assert read_cache(max_age=5.0) is None

    def test_returns_none_for_corrupt_json(self, tmp_path):
        """Should return None for invalid JSON."""
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("not valid json{{{")

        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            assert read_cache() is None

    def test_returns_none_for_missing_timestamp(self, tmp_path):
        """Should return None if timestamp field is missing."""
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "agents": []}))

        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            assert read_cache() is None

    def test_rejects_v1_cache_without_schema_version(self, tmp_path):
        """A pre-#73 cache (no ``schema_version``) must be rejected — the
        per-agent shape changed (``phase_snapshot`` replaced flat
        ``phase``)."""
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "timestamp": time.time(),
                    "pid": os.getpid(),
                    "agents": [{"pane_id": "%5", "phase": "implement"}],
                }
            )
        )
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            assert read_cache() is None

    def test_rejects_wrong_schema_version(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        payload = _v2_payload([_make_agent()])
        payload["schema_version"] = 99
        cache_file.write_text(json.dumps(payload))
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            assert read_cache() is None

    def test_respects_custom_max_age(self, tmp_path):
        """Should use the provided max_age parameter."""
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps(_v2_payload([_make_agent()], timestamp=time.time() - 3.0)))

        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            # 2s max_age: stale
            assert read_cache(max_age=2.0) is None
            # 10s max_age: fresh
            result = read_cache(max_age=10.0)
            assert result is not None
            agents, _ = result
            assert len(agents) == 1

    def test_roundtrip_preserves_phase_snapshot(self, tmp_path):
        """An agent with a populated phase_snapshot round-trips through
        write_cache → read_cache."""
        from agent_hq.models import DevPhase, InputsObserved, PhaseSnapshot

        agent = _make_agent("%5")
        agent.phase_snapshot = PhaseSnapshot(
            detected_phase=DevPhase.IMPLEMENT,
            effective_phase=DevPhase.IMPLEMENT,
            phase_source="tag",
            phase_session_id="trans-1",
            feedback=None,
            inputs_observed=InputsObserved(overrides_revision=5, hook_state_revision=12),
            producer="daemon",
            producer_epoch=1.0,
            sequence=3,
            monotonic_ns=99,
            timestamp=2.0,
        )
        cache_file = tmp_path / "cache.json"
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            write_cache([agent], producer="daemon")
            result = read_cache(max_age=3600)
        assert result is not None
        agents, _ = result
        assert agents[0].phase_snapshot.phase_source == "tag"
        assert agents[0].phase_snapshot.inputs_observed == InputsObserved(5, 12)


class TestGetAgents:
    """Tests for get_agents() unified accessor."""

    def test_returns_cached_agents_when_available(self, tmp_path):
        """Should return cached agents and the cache's timestamp without calling discovery."""
        cache_file = tmp_path / "cache.json"
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            write_cache([_make_agent("%5")])

            with patch("agent_hq.cache.discover_agents") as mock_discover:
                agents, ts = get_agents()

        assert len(agents) == 1
        assert agents[0].pane_id == "%5"
        assert ts > 0
        mock_discover.assert_not_called()

    def test_falls_back_to_live_discovery(self, tmp_path):
        """Should call discovery when cache is missing and stamp result with current time."""
        cache_file = tmp_path / "nonexistent.json"
        raw_agents = [
            {
                "session_name": "main",
                "window_index": 1,
                "pane_index": 0,
                "pane_id": "%5",
                "pane_pid": 1234,
                "command": "claude",
            }
        ]
        before = time.time()
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            with patch("agent_hq.cache.discover_agents", return_value=raw_agents) as mock_discover:
                with patch(
                    "agent_hq.cache.enrich_all_agents", return_value=[_make_agent("%5")]
                ) as mock_enrich:
                    agents, ts = get_agents()

        assert len(agents) == 1
        assert ts >= before
        mock_discover.assert_called_once()
        mock_enrich.assert_called_once_with(raw_agents)

    def test_filters_cached_agents_by_command(self, tmp_path):
        """Should filter cached agents when filter_commands is provided."""
        cache_file = tmp_path / "cache.json"
        agents_list = [
            _make_agent("%5"),  # claude
            Agent(
                pane_id="%8",
                session_name="main",
                window_index=1,
                pane_index=1,
                agent_type=AgentType.CODEX,
                cwd="/tmp",
                raw_command="codex",
                state=AgentState.RUNNING,
            ),
        ]
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            write_cache(agents_list)
            agents, _ = get_agents(filter_commands={"claude"})

        assert len(agents) == 1
        assert agents[0].agent_type == AgentType.CLAUDE


class TestDaemonCLI:
    """Tests for daemon subcommand argument parsing."""

    def test_daemon_start_parsed(self):
        """Should parse 'daemon start' subcommand."""
        parser = create_parser()
        args = parser.parse_args(["daemon", "start"])
        assert args.daemon_action == "start"
        assert args.interval == 2.0

    def test_daemon_start_with_interval(self):
        """Should parse 'daemon start --interval 5'."""
        parser = create_parser()
        args = parser.parse_args(["daemon", "start", "--interval", "5"])
        assert args.daemon_action == "start"
        assert args.interval == 5.0

    def test_daemon_stop_parsed(self):
        """Should parse 'daemon stop' subcommand."""
        parser = create_parser()
        args = parser.parse_args(["daemon", "stop"])
        assert args.daemon_action == "stop"

    def test_daemon_status_parsed(self):
        """Should parse 'daemon status' subcommand."""
        parser = create_parser()
        args = parser.parse_args(["daemon", "status"])
        assert args.daemon_action == "status"


class TestGetAgentsEdgeCases:
    """Edge case tests for get_agents()."""

    def test_empty_cache(self, tmp_path):
        """Should return empty list when cache has no agents."""
        cache_file = tmp_path / "cache.json"
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            write_cache([])
            agents, _ = get_agents()
        assert agents == []

    def test_filter_with_no_matches(self, tmp_path):
        """Should return empty list when filter matches nothing."""
        cache_file = tmp_path / "cache.json"
        with patch("agent_hq.cache.CACHE_FILE", cache_file):
            write_cache([_make_agent("%5")])  # claude
            agents, _ = get_agents(filter_commands={"codex"})
        assert agents == []
