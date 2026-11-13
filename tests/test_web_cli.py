"""Tests for the ``agent-hq web`` entry-point glue.

Covers tailnet detection, QR-code printing, and the daemon-running
guard. The actual uvicorn run is mocked out — we only assert that the
CLI wires the right arguments through.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from agent_hq.cli import detect_tailnet_ip, run_web


class TestDetectTailnetIp:
    def test_returns_tailscale_ip_when_available(self):
        completed = subprocess.CompletedProcess(
            args=["tailscale", "ip", "-4"],
            returncode=0,
            stdout="100.64.1.5\n",
            stderr="",
        )
        with patch("agent_hq.cli.subprocess.run", return_value=completed):
            assert detect_tailnet_ip() == "100.64.1.5"

    def test_first_line_only_when_multiple(self):
        completed = subprocess.CompletedProcess(
            args=["tailscale", "ip", "-4"],
            returncode=0,
            stdout="100.64.1.5\nfd7a:115c::1\n",
            stderr="",
        )
        with patch("agent_hq.cli.subprocess.run", return_value=completed):
            assert detect_tailnet_ip() == "100.64.1.5"

    def test_falls_back_to_loopback_when_tailscale_missing(self):
        with patch("agent_hq.cli.subprocess.run", side_effect=FileNotFoundError):
            assert detect_tailnet_ip() == "127.0.0.1"

    def test_falls_back_to_loopback_on_nonzero_exit(self):
        with patch(
            "agent_hq.cli.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "tailscale"),
        ):
            assert detect_tailnet_ip() == "127.0.0.1"

    def test_falls_back_to_loopback_on_timeout(self):
        with patch(
            "agent_hq.cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired("tailscale", 2),
        ):
            assert detect_tailnet_ip() == "127.0.0.1"

    def test_falls_back_to_loopback_on_blank_stdout(self):
        completed = subprocess.CompletedProcess(
            args=["tailscale", "ip", "-4"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("agent_hq.cli.subprocess.run", return_value=completed):
            assert detect_tailnet_ip() == "127.0.0.1"


class TestRunWeb:
    """Boot path: ensure_running, QR print, uvicorn run."""

    @pytest.fixture
    def stub_modules(self, tmp_path, monkeypatch):
        """Patch out the heavy collaborators so we just observe wiring."""
        monkeypatch.setattr("agent_hq.daemon.is_daemon_running", lambda: True)
        monkeypatch.setattr("agent_hq.daemon.ensure_running", lambda: True)
        # Stub uvicorn.run so the test doesn't actually start a server
        import uvicorn

        run_calls = []
        monkeypatch.setattr(
            uvicorn,
            "run",
            lambda app, **kw: run_calls.append((app, kw)),
        )
        # Stub create_app so we don't spin up a real FastAPI watcher
        from agent_hq.web import app as app_mod

        captured_token = []

        def fake_create_app(*, token_file, **kwargs):
            captured_token.append(token_file)
            from fastapi import FastAPI

            return FastAPI()

        monkeypatch.setattr(app_mod, "create_app", fake_create_app)
        return {"uvicorn_calls": run_calls, "token_paths": captured_token}

    def test_prints_qr_with_login_url(self, stub_modules, tmp_path, capsys):
        token_file = tmp_path / "token"
        run_web(
            bind="100.64.1.5",
            port=8765,
            token_file=token_file,
            no_daemon=False,
            verbose=False,
        )
        out = capsys.readouterr().out
        assert "100.64.1.5" in out
        assert "8765" in out
        # Token gets created and printed
        assert token_file.exists()
        token = token_file.read_text().strip()
        assert token in out
        # QR code rendered (ASCII block characters or "Scan" prompt)
        assert "Scan" in out

    def test_quiet_suppresses_token_and_qr(self, stub_modules, tmp_path, capsys):
        # Service mode (--quiet) must NOT print the token or QR/login URL,
        # since the LaunchAgent redirects stdout to a persistent log file.
        token_file = tmp_path / "token"
        run_web(
            bind="100.64.1.5",
            port=8765,
            token_file=token_file,
            no_daemon=False,
            verbose=False,
            quiet=True,
        )
        out = capsys.readouterr().out
        assert token_file.exists()
        token = token_file.read_text().strip()
        assert token not in out  # secret must not be logged
        assert "Scan" not in out  # no QR prompt
        assert "login?token=" not in out

    def test_passes_bind_and_port_to_uvicorn(self, stub_modules, tmp_path):
        token_file = tmp_path / "token"
        run_web(
            bind="100.64.1.5",
            port=8765,
            token_file=token_file,
            no_daemon=False,
            verbose=False,
        )
        calls = stub_modules["uvicorn_calls"]
        assert len(calls) == 1
        _, kwargs = calls[0]
        assert kwargs["host"] == "100.64.1.5"
        assert kwargs["port"] == 8765
        # Hard requirement: never spawn workers > 1 (would split SSE state)
        assert kwargs["workers"] == 1
        assert kwargs["reload"] is False

    def test_no_daemon_with_no_running_daemon_exits_nonzero(self, monkeypatch, tmp_path, capsys):
        """``--no-daemon`` is opt-in. If the daemon isn't already up,
        we must NOT start one; surface a clear error and exit 1."""
        monkeypatch.setattr("agent_hq.daemon.is_daemon_running", lambda: False)
        monkeypatch.setattr(
            "agent_hq.daemon.ensure_running",
            lambda: pytest.fail("ensure_running should NOT be called with --no-daemon"),
        )
        token_file = tmp_path / "token"
        rc = run_web(
            bind="127.0.0.1",
            port=8765,
            token_file=token_file,
            no_daemon=True,
            verbose=False,
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "agent-hq daemon start" in err

    def test_no_daemon_with_running_daemon_proceeds(self, monkeypatch, tmp_path):
        monkeypatch.setattr("agent_hq.daemon.is_daemon_running", lambda: True)
        monkeypatch.setattr(
            "agent_hq.daemon.ensure_running",
            lambda: pytest.fail("ensure_running should NOT be called with --no-daemon"),
        )
        import uvicorn

        run_calls = []
        monkeypatch.setattr(
            uvicorn,
            "run",
            lambda app, **kw: run_calls.append((app, kw)),
        )
        from fastapi import FastAPI

        from agent_hq.web import app as app_mod

        monkeypatch.setattr(app_mod, "create_app", lambda **kw: FastAPI())
        token_file = tmp_path / "token"
        rc = run_web(
            bind="127.0.0.1",
            port=8765,
            token_file=token_file,
            no_daemon=True,
            verbose=False,
        )
        assert rc == 0
        assert len(run_calls) == 1

    def test_default_path_calls_ensure_running(self, monkeypatch, tmp_path):
        ensure_called = []

        def fake_ensure():
            ensure_called.append(True)
            return True

        monkeypatch.setattr("agent_hq.daemon.ensure_running", fake_ensure)
        # is_daemon_running shouldn't be touched on the auto-start path.
        monkeypatch.setattr(
            "agent_hq.daemon.is_daemon_running",
            lambda: pytest.fail("is_daemon_running should not be called on auto-start path"),
        )
        import uvicorn

        run_calls = []
        monkeypatch.setattr(
            uvicorn,
            "run",
            lambda app, **kw: run_calls.append((app, kw)),
        )
        from fastapi import FastAPI

        from agent_hq.web import app as app_mod

        monkeypatch.setattr(app_mod, "create_app", lambda **kw: FastAPI())
        token_file = tmp_path / "token"
        rc = run_web(
            bind="127.0.0.1",
            port=8765,
            token_file=token_file,
            no_daemon=False,
            verbose=False,
        )
        assert rc == 0
        assert ensure_called == [True]
