"""Tests for the launchd LaunchAgent service module (issue #100)."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_hq import service
from agent_hq.service import (
    LABEL,
    ServiceError,
    build_plist,
)


def _load(xml: str) -> dict:
    """Parse a built plist string back into a dict for assertions."""
    return plistlib.loads(xml.encode("utf-8"))


class TestBuildPlist:
    """The pure plist-builder — no I/O, no subprocess."""

    def _build(self, **overrides) -> dict:
        kwargs = dict(
            executable="/Users/me/.local/bin/agent-hq",
            bind="100.64.1.5",
            port=8765,
            token_file=Path("/Users/me/.config/agent-hq/web-token"),
        )
        kwargs.update(overrides)
        return _load(build_plist(**kwargs))

    def test_label(self):
        assert self._build()["Label"] == LABEL

    def test_program_arguments_absolute_executable(self):
        args = self._build()["ProgramArguments"]
        assert args[0] == "/Users/me/.local/bin/agent-hq"
        assert Path(args[0]).is_absolute()

    def test_program_arguments_invoke_web_with_flags(self):
        args = self._build()["ProgramArguments"]
        assert "web" in args
        assert args[args.index("--bind") + 1] == "100.64.1.5"
        assert args[args.index("--port") + 1] == "8765"
        assert args[args.index("--token-file") + 1] == "/Users/me/.config/agent-hq/web-token"

    def test_program_arguments_omit_no_daemon(self):
        # The service must let `run_web` auto-start the discovery daemon.
        assert "--no-daemon" not in self._build()["ProgramArguments"]

    def test_program_arguments_include_quiet(self):
        # Service mode must suppress the token/QR banner so the secret never
        # lands in the persistent log file.
        assert "--quiet" in self._build()["ProgramArguments"]

    def test_bind_value_passes_through(self):
        args = self._build(bind="0.0.0.0")["ProgramArguments"]
        assert args[args.index("--bind") + 1] == "0.0.0.0"

    def test_path_includes_homebrew_and_system_and_local(self):
        path = self._build()["EnvironmentVariables"]["PATH"]
        assert "/opt/homebrew/bin" in path
        assert "/usr/local/bin" in path  # Intel Homebrew prefix
        assert "/usr/bin" in path
        assert str(Path.home() / ".local" / "bin") in path

    def test_run_at_load_true(self):
        assert self._build()["RunAtLoad"] is True

    def test_keepalive_true(self):
        assert self._build()["KeepAlive"] is True

    def test_throttle_interval(self):
        assert self._build()["ThrottleInterval"] == 10

    def test_process_type_interactive(self):
        assert self._build()["ProcessType"] == "Interactive"

    def test_log_paths(self):
        plist = self._build()
        assert plist["StandardOutPath"].endswith("web.out.log")
        assert plist["StandardErrorPath"].endswith("web.err.log")

    def test_working_directory_is_home(self):
        assert self._build()["WorkingDirectory"] == str(Path.home())

    def test_is_valid_plist_xml(self):
        xml = build_plist(
            executable="/abs/agent-hq",
            bind="100.64.1.5",
            port=8765,
            token_file=Path("/tmp/tok"),
        )
        assert xml.startswith("<?xml")
        plistlib.loads(xml.encode("utf-8"))  # must not raise

    def test_custom_port_passes_through(self):
        args = self._build(port=9000)["ProgramArguments"]
        assert args[args.index("--port") + 1] == "9000"


def test_service_error_is_runtime_error():
    assert issubclass(ServiceError, RuntimeError)


@pytest.fixture
def svc_env(monkeypatch, tmp_path):
    """darwin + uid 501 + agent-hq on PATH + mocked launchctl + tmp paths."""
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 501)
    monkeypatch.setattr(service.shutil, "which", lambda name: "/abs/agent-hq")
    plist = tmp_path / "LaunchAgents" / f"{LABEL}.plist"
    logdir = tmp_path / "Logs" / "agent-hq"
    monkeypatch.setattr(service, "PLIST_PATH", plist)
    monkeypatch.setattr(service, "LOG_DIR", logdir)
    run_mock = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(service.subprocess, "run", run_mock)
    return SimpleNamespace(run=run_mock, plist=plist, logdir=logdir)


def _launchctl_calls(run_mock) -> list[list[str]]:
    """The launchctl argv lists passed to each subprocess.run call."""
    return [call.args[0] for call in run_mock.call_args_list]


class TestInstall:
    def test_writes_plist_file(self, svc_env):
        rc = service.install_service(bind="100.64.1.5", port=8765, token_file=Path("/tmp/tok"))
        assert rc == 0
        assert svc_env.plist.exists()
        plist = plistlib.loads(svc_env.plist.read_bytes())
        assert plist["Label"] == LABEL
        assert "--bind" in plist["ProgramArguments"]

    def test_creates_log_dir(self, svc_env):
        service.install_service(bind="100.64.1.5", port=8765, token_file=Path("/tmp/tok"))
        assert svc_env.logdir.is_dir()

    def test_calls_bootstrap_with_domain(self, svc_env):
        service.install_service(bind="100.64.1.5", port=8765, token_file=Path("/tmp/tok"))
        calls = _launchctl_calls(svc_env.run)
        assert ["launchctl", "bootstrap", "gui/501", str(svc_env.plist)] in calls

    def test_idempotent_reload_boots_out_then_bootstraps(self, svc_env):
        svc_env.plist.parent.mkdir(parents=True, exist_ok=True)
        svc_env.plist.write_text("stale")
        service.install_service(bind="100.64.1.5", port=8765, token_file=Path("/tmp/tok"))
        calls = _launchctl_calls(svc_env.run)
        bootout_idx = calls.index(["launchctl", "bootout", "gui/501/" + LABEL])
        bootstrap_idx = next(i for i, c in enumerate(calls) if c[:2] == ["launchctl", "bootstrap"])
        assert bootout_idx < bootstrap_idx

    def test_raises_when_agent_hq_not_on_path(self, svc_env, monkeypatch):
        monkeypatch.setattr(service.shutil, "which", lambda name: None)
        with pytest.raises(ServiceError, match="install-global"):
            service.install_service(bind="100.64.1.5", port=8765, token_file=Path("/tmp/tok"))

    def test_raises_when_bootstrap_fails(self, svc_env):
        svc_env.run.return_value = subprocess.CompletedProcess([], 1, "", "boom")
        with pytest.raises(ServiceError, match="bootstrap failed"):
            service.install_service(bind="100.64.1.5", port=8765, token_file=Path("/tmp/tok"))

    def test_bootstrap_eio_tolerated_when_service_loads(self, svc_env):
        # launchd can return "5: Input/output error" from `bootstrap` right
        # after a `bootout` (unload not settled) even though the service does
        # load. Treat a nonzero bootstrap as success if the job is loaded.
        svc_env.run.side_effect = [
            subprocess.CompletedProcess([], 0, "", ""),  # bootout
            subprocess.CompletedProcess(
                [], 5, "", "Bootstrap failed: 5: Input/output error"
            ),  # bootstrap
            subprocess.CompletedProcess([], 0, "pid = 99\n", ""),  # print -> loaded
        ]
        assert (
            service.install_service(bind="100.64.1.5", port=8765, token_file=Path("/tmp/tok")) == 0
        )

    def test_bootstrap_failure_raises_when_loaded_but_not_eio(self, svc_env):
        # A non-EIO bootstrap failure (e.g. a stuck old job still loaded) must
        # NOT be swallowed just because something is loaded.
        svc_env.run.side_effect = [
            subprocess.CompletedProcess([], 0, "", ""),  # bootout
            subprocess.CompletedProcess(
                [], 5, "", "Bootstrap failed: 17: Service is already loaded"
            ),  # bootstrap (not the EIO signature)
        ]
        with pytest.raises(ServiceError, match="bootstrap failed"):
            service.install_service(bind="100.64.1.5", port=8765, token_file=Path("/tmp/tok"))

    def test_bootstrap_failure_raises_when_not_loaded(self, svc_env):
        # bootstrap fails AND the job is not loaded -> genuine failure.
        svc_env.run.side_effect = [
            subprocess.CompletedProcess([], 0, "", ""),  # bootout
            subprocess.CompletedProcess([], 5, "", "boom"),  # bootstrap
            subprocess.CompletedProcess([], 1, "", "Could not find service"),  # print -> not loaded
        ]
        with pytest.raises(ServiceError, match="bootstrap failed"):
            service.install_service(bind="100.64.1.5", port=8765, token_file=Path("/tmp/tok"))

    def test_bootout_failure_is_tolerated(self, svc_env):
        # Fresh install: `bootout` returns nonzero ("nothing loaded") but
        # `bootstrap` succeeds — install must still return 0. Pins the
        # bootout-then-bootstrap idempotency contract.
        svc_env.run.side_effect = [
            subprocess.CompletedProcess([], 3, "", "No such process"),  # bootout
            subprocess.CompletedProcess([], 0, "", ""),  # bootstrap
        ]
        rc = service.install_service(bind="100.64.1.5", port=8765, token_file=Path("/tmp/tok"))
        assert rc == 0
        assert svc_env.plist.exists()

    def test_auto_detect_tailnet_ip_when_bind_none(self, svc_env, monkeypatch):
        monkeypatch.setattr("agent_hq.cli.detect_tailnet_ip", lambda: "100.99.1.2")
        service.install_service(bind=None, port=8765, token_file=Path("/tmp/tok"))
        plist = plistlib.loads(svc_env.plist.read_bytes())
        args = plist["ProgramArguments"]
        assert args[args.index("--bind") + 1] == "100.99.1.2"

    def test_guards_against_localhost_only_service(self, svc_env, monkeypatch):
        monkeypatch.setattr("agent_hq.cli.detect_tailnet_ip", lambda: "127.0.0.1")
        with pytest.raises(ServiceError, match="Tailscale IP not detected"):
            service.install_service(bind=None, port=8765, token_file=Path("/tmp/tok"))

    def test_explicit_bind_skips_tailnet_guard(self, svc_env):
        # An explicit --bind (even 0.0.0.0) is honored without detection.
        rc = service.install_service(bind="0.0.0.0", port=8765, token_file=Path("/tmp/tok"))
        assert rc == 0
        plist = plistlib.loads(svc_env.plist.read_bytes())
        args = plist["ProgramArguments"]
        assert args[args.index("--bind") + 1] == "0.0.0.0"


class TestUninstall:
    def test_calls_bootout_with_domain_target(self, svc_env, monkeypatch):
        monkeypatch.setattr("agent_hq.daemon.stop_daemon", lambda: 0)
        service.uninstall_service()
        assert ["launchctl", "bootout", "gui/501/" + LABEL] in _launchctl_calls(svc_env.run)

    def test_removes_plist(self, svc_env, monkeypatch):
        monkeypatch.setattr("agent_hq.daemon.stop_daemon", lambda: 0)
        svc_env.plist.parent.mkdir(parents=True, exist_ok=True)
        svc_env.plist.write_text("x")
        service.uninstall_service()
        assert not svc_env.plist.exists()

    def test_stops_daemon(self, svc_env, monkeypatch):
        stop = MagicMock(return_value=0)
        monkeypatch.setattr("agent_hq.daemon.stop_daemon", stop)
        service.uninstall_service()
        stop.assert_called_once()

    def test_missing_plist_is_ok(self, svc_env, monkeypatch):
        monkeypatch.setattr("agent_hq.daemon.stop_daemon", lambda: 0)
        assert service.uninstall_service() == 0  # plist never written


class TestStatus:
    def _write_plist(self, svc_env, *, bind="100.64.1.5", port=8765):
        svc_env.plist.parent.mkdir(parents=True, exist_ok=True)
        svc_env.plist.write_text(
            build_plist(
                executable="/abs/agent-hq",
                bind=bind,
                port=port,
                token_file=Path("/tmp/tok"),
            )
        )

    def test_loaded_running_parses_pid(self, svc_env, capsys):
        self._write_plist(svc_env)
        svc_env.run.return_value = subprocess.CompletedProcess(
            [], 0, "pid = 12345\nstate = running\n", ""
        )
        assert service.service_status() == 0
        out = capsys.readouterr().out
        assert "12345" in out and "running" in out

    def test_not_loaded_returns_1(self, svc_env, capsys):
        svc_env.run.return_value = subprocess.CompletedProcess([], 113, "", "no such service")
        assert service.service_status() == 1
        assert "not loaded" in capsys.readouterr().out

    def test_reports_bind_url_from_plist(self, svc_env, capsys):
        self._write_plist(svc_env, bind="100.64.1.5", port=9000)
        svc_env.run.return_value = subprocess.CompletedProcess([], 0, "pid = 1\n", "")
        service.service_status()
        out = capsys.readouterr().out
        assert "100.64.1.5:9000/agents" in out

    def test_tolerates_malformed_plist(self, svc_env):
        # A manually-edited plist with a trailing flag (no value) must not
        # crash status with IndexError — the URL is just omitted.
        svc_env.plist.parent.mkdir(parents=True, exist_ok=True)
        bad = {"Label": LABEL, "ProgramArguments": ["/abs/agent-hq", "web", "--bind"]}
        svc_env.plist.write_bytes(plistlib.dumps(bad))
        svc_env.run.return_value = subprocess.CompletedProcess([], 0, "pid = 1\n", "")
        assert service.service_status() == 0  # must not raise


class TestRestart:
    def test_calls_kickstart_dash_k(self, svc_env):
        assert service.restart_service() == 0
        assert ["launchctl", "kickstart", "-k", "gui/501/" + LABEL] in _launchctl_calls(svc_env.run)

    def test_raises_on_failure(self, svc_env):
        svc_env.run.return_value = subprocess.CompletedProcess([], 1, "", "no such service")
        with pytest.raises(ServiceError, match="kickstart failed"):
            service.restart_service()


class TestMacOSGuard:
    @pytest.mark.parametrize(
        "fn",
        [
            lambda: service.install_service(bind="1.2.3.4", port=1, token_file=Path("/x")),
            service.uninstall_service,
            service.service_status,
            service.restart_service,
        ],
    )
    def test_errors_on_non_darwin(self, fn, monkeypatch):
        monkeypatch.setattr(service.sys, "platform", "linux")
        with pytest.raises(ServiceError, match="only supported on macOS"):
            fn()
