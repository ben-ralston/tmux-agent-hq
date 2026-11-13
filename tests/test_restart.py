"""Tests for agent_hq/restart.py — runtime restart core (#215).

subprocess.run is always mocked: these steps shell out to the real
agent-hq CLI and launchctl, which must never run from the suite.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from agent_hq import daemon, restart


def _completed(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


@pytest.fixture
def cache_file(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    monkeypatch.setattr(daemon, "CACHE_FILE", path)
    return path


@pytest.fixture
def fake_exe(monkeypatch):
    monkeypatch.setattr(restart, "_agent_hq_executable", lambda: "/fake/agent-hq")


def _fake_run_factory(calls=None, rc_for=None, output_for=None, raise_for=None):
    """A subprocess.run stand-in matching the file-capture contract.

    Step output is delivered by WRITING to the caller-provided ``stdout``
    file — never via CompletedProcess.stdout — mirroring how the real
    implementation must capture it (see the pipe-inheritance test below).
    """

    def fake_run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        # Regression (#215 QA finding): `daemon start` forks a long-lived
        # child that inherits the CLI's stdout/stderr. PIPE capture makes
        # subprocess.run wait for pipe EOF that may never come, so the step
        # nondeterministically times out. The implementation must hand real
        # files to subprocess.run, not pipes.
        assert "capture_output" not in kwargs
        assert kwargs.get("stdout") not in (None, subprocess.PIPE)
        assert kwargs.get("stderr") is not subprocess.PIPE
        action = cmd[-1]
        if raise_for and action in raise_for:
            raise raise_for[action]
        if output_for and action in output_for:
            kwargs["stdout"].write(output_for[action])
        return _completed((rc_for or {}).get(action, 0))

    return fake_run


class TestPerformDaemonRestart:
    def test_happy_path_three_ok_steps(self, cache_file, fake_exe, monkeypatch):
        cache_file.write_text("{}")
        calls = []
        monkeypatch.setattr(
            restart.subprocess,
            "run",
            _fake_run_factory(calls=calls, output_for={"start": "Daemon started (PID 7)"}),
        )
        steps = restart.perform_daemon_restart()
        assert [s.ok for s in steps] == [True, True, True]
        assert [s.name for s in steps] == [
            "daemon stop",
            "clear snapshot cache",
            "daemon start",
        ]
        assert steps[2].detail == "Daemon started (PID 7)"
        assert calls == [
            ["/fake/agent-hq", "daemon", "stop"],
            ["/fake/agent-hq", "daemon", "start"],
        ]
        assert not cache_file.exists()

    def test_stop_not_running_is_ok(self, cache_file, fake_exe, monkeypatch):
        monkeypatch.setattr(
            restart.subprocess,
            "run",
            _fake_run_factory(rc_for={"stop": 1}, output_for={"stop": "Daemon is not running"}),
        )
        steps = restart.perform_daemon_restart()
        assert steps[0].ok is True
        assert "not running" in steps[0].detail

    def test_stop_timeout_still_attempts_start(self, cache_file, fake_exe, monkeypatch):
        calls = []
        monkeypatch.setattr(
            restart.subprocess,
            "run",
            _fake_run_factory(
                calls=calls,
                raise_for={"stop": subprocess.TimeoutExpired(cmd="stop", timeout=15)},
            ),
        )
        steps = restart.perform_daemon_restart()
        assert steps[0].ok is False
        assert steps[2].ok is True
        assert ["/fake/agent-hq", "daemon", "start"] in calls

    def test_start_failure_reported(self, cache_file, fake_exe, monkeypatch):
        monkeypatch.setattr(
            restart.subprocess,
            "run",
            _fake_run_factory(rc_for={"start": 2}, output_for={"start": "boom"}),
        )
        steps = restart.perform_daemon_restart()
        assert steps[2].ok is False
        assert "boom" in steps[2].detail

    def test_missing_cache_file_is_ok(self, cache_file, fake_exe, monkeypatch):
        monkeypatch.setattr(restart.subprocess, "run", _fake_run_factory())
        steps = restart.perform_daemon_restart()
        assert steps[1].ok is True

    def test_no_executable_single_failed_step(self, cache_file, monkeypatch):
        monkeypatch.setattr(restart, "_agent_hq_executable", lambda: None)
        steps = restart.perform_daemon_restart()
        assert len(steps) == 1
        assert steps[0].ok is False


class TestAgentHqExecutable:
    def test_prefers_argv0_when_it_is_agent_hq(self, monkeypatch):
        monkeypatch.setattr(restart.sys, "argv", ["/opt/venv/bin/agent-hq", "web"])
        assert restart._agent_hq_executable() == "/opt/venv/bin/agent-hq"

    def test_falls_back_to_which(self, monkeypatch):
        monkeypatch.setattr(restart.sys, "argv", ["pytest"])
        monkeypatch.setattr(restart.shutil, "which", lambda name: "/global/agent-hq")
        assert restart._agent_hq_executable() == "/global/agent-hq"

    def test_none_when_nothing_found(self, monkeypatch):
        monkeypatch.setattr(restart.sys, "argv", ["pytest"])
        monkeypatch.setattr(restart.shutil, "which", lambda name: None)
        assert restart._agent_hq_executable() is None


class TestWebServiceOwnsProcess:
    def test_true_when_launchd_pid_matches(self, monkeypatch):
        monkeypatch.setattr(
            restart.service,
            "_launchctl",
            lambda args: _completed(0, stdout="\tpid = 42\n"),
        )
        assert restart.web_service_owns_process(42) is True

    def test_false_on_pid_mismatch(self, monkeypatch):
        monkeypatch.setattr(
            restart.service,
            "_launchctl",
            lambda args: _completed(0, stdout="\tpid = 42\n"),
        )
        assert restart.web_service_owns_process(43) is False

    def test_false_when_service_not_loaded(self, monkeypatch):
        monkeypatch.setattr(
            restart.service,
            "_launchctl",
            lambda args: _completed(113, stderr="Could not find service"),
        )
        assert restart.web_service_owns_process(42) is False

    def test_false_when_no_pid_line(self, monkeypatch):
        monkeypatch.setattr(
            restart.service,
            "_launchctl",
            lambda args: _completed(0, stdout="state = not running\n"),
        )
        assert restart.web_service_owns_process(42) is False

    def test_false_when_launchctl_unavailable(self, monkeypatch):
        def raise_oserror(args):
            raise FileNotFoundError("launchctl not found")

        monkeypatch.setattr(restart.service, "_launchctl", raise_oserror)
        assert restart.web_service_owns_process(42) is False


class TestKickWebService:
    def test_spawns_detached_kickstart(self, monkeypatch):
        popen_calls = []
        monkeypatch.setattr(
            restart.subprocess,
            "Popen",
            lambda cmd, **kw: popen_calls.append(cmd) or SimpleNamespace(),
        )
        monkeypatch.setattr(restart.service, "_domain_target", lambda: "gui/501/label")
        restart.kick_web_service()
        assert popen_calls == [["launchctl", "kickstart", "-k", "gui/501/label"]]

    def test_spawn_failure_does_not_raise(self, monkeypatch):
        def raise_oserror(cmd, **kw):
            raise OSError("spawn failed")

        monkeypatch.setattr(restart.subprocess, "Popen", raise_oserror)
        monkeypatch.setattr(restart.service, "_domain_target", lambda: "gui/501/label")
        restart.kick_web_service()  # must not propagate
