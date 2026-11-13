"""The demo (``demo/``) must not reach real machine state, including from the
processes it starts.

``demo/_isolate.py`` redirects one process; the app also re-runs ``agent-hq``
in fresh processes (the dashboard's Restart, fzf key bindings), and a fresh
process does not inherit in-process patches. ``demo/check_isolation.py`` asks
such processes what they would use. It starts no daemon, server or tmux
session, so it is safe to run here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _demo_env(root: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_HQ_DEMO")}
    env["AGENT_HQ_DEMO_ROOT"] = str(root)
    return env


def test_demo_isolation_reaches_child_processes(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPO / "demo" / "check_isolation.py")],
        cwd=REPO,
        env=_demo_env(tmp_path / "demo-root"),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    assert "all isolation checks passed" in result.stdout


def test_demo_launcher_refuses_service_and_remote_commands(tmp_path):
    for argv in (["remote", "serve"], ["web", "service", "install"]):
        result = subprocess.run(
            [sys.executable, str(REPO / "demo" / "hq.py"), *argv],
            cwd=REPO,
            env=_demo_env(tmp_path / "demo-root"),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 2, result.stderr
        assert "the demo does not run" in result.stderr
