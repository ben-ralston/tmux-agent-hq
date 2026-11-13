"""Guard tests for global test isolation (#133).

The suite must never read or write the real machine state the live
daemon / web service maintains:

* ``/tmp/agent-hq/`` — cache.json, states/, overrides.json, pid/lock
  files. The launchd ``agent-hq web`` service rewrites these every ~2 s,
  so an unpatched read races against live machine state (the
  ``test_web_watcher.py`` failures in #133).
* Real ``$HOME`` state — ``~/.config/agent-hq/``, ``~/.cache/agent-hq/``,
  ``~/.claude/`` — user-owned files that tests must neither depend on
  nor clobber (e.g. ``test_idea_handler`` failing because the user's
  real ``personal_inbox.json`` leaked into ``list_recent``).

``tests/conftest.py`` redirects every such module-level path constant
into the test's ``tmp_path`` via an autouse fixture. These tests pin
that contract: if a new real-path constant is added (or a redirect is
dropped), this file fails rather than the suite going flaky.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# Every module-level constant that points at live machine state.
# (module, attribute) — conftest's autouse fixture must redirect each
# one into the test's tmp_path.
ISOLATED_PATH_CONSTANTS = [
    # /tmp/agent-hq — owned by the daemon + hook system.
    ("agent_hq.runtime_dir", "RUNTIME_ROOT"),
    ("agent_hq.cache", "CACHE_FILE"),
    ("agent_hq.codex_runtime", "RUNTIME_DIR"),
    ("agent_hq.status_collector", "STATUS_FILE"),
    ("agent_hq.daemon", "CACHE_FILE"),  # separate from-import binding
    ("agent_hq.web.watcher", "CACHE_FILE"),  # separate from-import binding
    ("agent_hq.daemon", "PID_FILE"),
    ("agent_hq.daemon", "DAEMON_LOCK_FILE"),
    ("agent_hq.hooks", "HOOK_STATE_DIR"),
    ("agent_hq.web.watcher", "HOOKS_DIR"),
    ("agent_hq.overrides", "OVERRIDES_FILE"),
    ("agent_hq.overrides", "OVERRIDES_LOCK"),
    # Real $HOME state.
    ("agent_hq.overrides", "FEEDBACK_FILE"),
    ("agent_hq.transcript", "CLAUDE_PROJECTS_DIR"),
    ("agent_hq.enrichment", "CLAUDE_SESSIONS_DIR"),
    ("agent_hq.runtime_status", "CLAUDE_SESSIONS_DIR"),
    ("agent_hq.enrichment", "CODEX_SESSIONS_DIR"),
    ("agent_hq.hooks", "CODEX_SESSION_TAGS_FILE"),
    ("agent_hq.codex_hooks", "CODEX_HOOKS_FILE"),
    ("agent_hq.snapshots", "_DEFAULT_SNAPSHOTS_FILE"),
    ("agent_hq.state", "STATE_DIR"),
    ("agent_hq.state", "ACCESS_FILE"),
    ("agent_hq.state", "INACTIVE_FILE"),
    ("agent_hq.config", "CONFIG_PATH"),
    ("agent_hq.github", "_CACHE_FILE"),
    ("agent_hq.gh_index", "_DEFAULT_INDEX_FILE"),
    ("agent_hq.pinned_store", "_DEFAULT_PINNED_FILE"),
    ("agent_hq.ui_prefs", "_DEFAULT_PREFS_FILE"),
    ("agent_hq.remote.config", "STATE_DIR"),
    ("agent_hq.remote.config", "LOG_DIR"),
    ("agent_hq.remote.config", "REQUEST_LOG"),
    ("agent_hq.remote.logger_events", "STAMP_FILE"),
    ("agent_hq.remote.service", "LAUNCH_AGENTS_DIR"),
    ("agent_hq.remote.service", "SERVER_LOG"),
    ("agent_hq.remote.service", "TUNNEL_LOG"),
    ("agent_hq.remote.service", "TUNNEL_CONFIG"),
    ("agent_hq.logclient", "_DEFAULT_TOKEN_FILE"),
    ("agent_hq.logclient", "_DEFAULT_QUEUE_FILE"),
    ("agent_hq.gh_meter", "_DEFAULT_COUNTER_FILE"),
    ("agent_hq.enrichment", "ENRICH_ERRORS_FILE"),
    ("agent_hq.logreader", "_DEFAULT_ADMIN_TOKEN_FILE"),
    ("agent_hq.idea.sent_store", "_DEFAULT_SENT_FILE"),
    ("agent_hq.idea.labels", "DEFAULT_CACHE_PATH"),
    ("agent_hq.idea.handler", "DEFAULT_CACHE_PATH"),  # separate from-import binding
    ("agent_hq.idea.cli", "DEFAULT_TOKEN_FILE"),
    ("agent_hq.capabilities", "DEFAULT_LAUNCHER_PATH"),
    ("agent_hq.autopilot", "LAUNCH_SCRIPT"),
    ("agent_hq.autopilot", "GOAL_TEMPLATE"),
    ("agent_hq.autopilot_cli", "GOAL_TEMPLATE"),  # separate from-import binding
]


@pytest.mark.parametrize(
    "module_name,attr",
    ISOLATED_PATH_CONSTANTS,
    ids=[f"{m}.{a}" for m, a in ISOLATED_PATH_CONSTANTS],
)
def test_path_constant_is_redirected_into_tmp(module_name, attr, tmp_path_factory):
    """Each real-path constant must point inside pytest's tmp base dir."""
    module = importlib.import_module(module_name)
    value = getattr(module, attr)
    assert isinstance(value, Path), f"{module_name}.{attr} is not a Path: {value!r}"
    assert value.is_relative_to(tmp_path_factory.getbasetemp()), (
        f"{module_name}.{attr} = {value} escapes the test sandbox — "
        "tests would read/write live machine state (see #133)"
    )


def test_real_locations_not_referenced():
    """Belt-and-braces: no isolated constant resolves under the real
    /tmp/agent-hq or the user's home directory."""
    home = Path.home()
    real_tmp = Path("/tmp/agent-hq")
    for module_name, attr in ISOLATED_PATH_CONSTANTS:
        value = getattr(importlib.import_module(module_name), attr)
        assert not value.is_relative_to(real_tmp), f"{module_name}.{attr} -> {value}"
        assert not value.is_relative_to(home), f"{module_name}.{attr} -> {value}"


class TestTmuxMutationGuard:
    """The suite must never mutate the live tmux server (#212).

    Path redirection covers files, but tmux is an external service: a
    route or helper that shells out to ``tmux new-window`` reaches the
    user's real sessions no matter where the isolated paths point. #212
    was exactly that — an unmocked ``POST /personal/dispatch`` spawned a
    real Claude session in the user's ``personal`` session on every
    full-suite run.

    Every target below names a session that cannot exist, so these tests
    stay harmless even if the guard itself regresses.
    """

    ABSENT = "agent-hq-guard-target-does-not-exist"

    def test_new_window_is_blocked(self):
        import subprocess

        with pytest.raises(RuntimeError, match="tmux new-window"):
            subprocess.run(
                ["tmux", "new-window", "-t", self.ABSENT],
                capture_output=True,
                text=True,
            )

    def test_send_keys_is_blocked(self):
        import subprocess

        with pytest.raises(RuntimeError, match="tmux send-keys"):
            subprocess.run(["tmux", "send-keys", "-t", self.ABSENT, "echo hi", "Enter"])

    def test_kill_session_is_blocked(self):
        import subprocess

        with pytest.raises(RuntimeError, match="tmux kill-session"):
            subprocess.run(["tmux", "kill-session", "-t", self.ABSENT])

    def test_paste_buffer_is_blocked(self):
        """A paste writes into a live pane with no ``send-keys`` (the old
        personal-dispatch seed path, #345) — same damage class, blocked."""
        import subprocess

        with pytest.raises(RuntimeError, match="tmux paste-buffer"):
            subprocess.run(["tmux", "paste-buffer", "-t", self.ABSENT])

    def test_load_buffer_is_blocked(self):
        import subprocess

        with pytest.raises(RuntimeError, match="tmux load-buffer"):
            subprocess.run(["tmux", "load-buffer", "-b", "buf", "/dev/null"])

    def test_popen_is_guarded_too(self):
        """``dispatch`` uses ``subprocess.run``, but Popen is the same
        escape hatch — guard both entry points."""
        import subprocess

        with pytest.raises(RuntimeError, match="tmux new-session"):
            subprocess.Popen(["tmux", "new-session", "-d", "-s", self.ABSENT])

    def test_global_socket_option_does_not_bypass(self):
        """A value-taking global option before the subcommand
        (``tmux -L sock new-session``) must not let the mutation slip
        through — a custom-socket test rig would otherwise bypass the
        guard entirely."""
        import subprocess

        with pytest.raises(RuntimeError, match="tmux new-session"):
            subprocess.run(["tmux", "-L", "sock", "new-session", "-d", "-s", self.ABSENT])

    def test_run_shell_is_blocked(self):
        """`tmux run-shell` runs an arbitrary command under the live
        server; it stays blocked though no app code runs it since #399."""
        import subprocess

        with pytest.raises(RuntimeError, match="tmux run-shell"):
            subprocess.run(["tmux", "run-shell", "-b", "true"])

    def test_absolute_path_tmux_is_guarded(self):
        import subprocess

        with pytest.raises(RuntimeError, match="tmux kill-window"):
            subprocess.run(["/opt/homebrew/bin/tmux", "kill-window", "-t", self.ABSENT])

    def test_read_only_subcommand_is_allowed(self):
        """Read-only tmux calls must still pass the guard — it must not turn
        every tmux-shelling test into a failure. Asserts the guard does not
        raise; tolerates a missing tmux binary (the guard runs before exec,
        so this test is really about the guard, not tmux's presence)."""
        import subprocess

        try:
            subprocess.run(
                ["tmux", "has-session", "-t", self.ABSENT],
                capture_output=True,
                text=True,
            )
        except RuntimeError as exc:  # pragma: no cover - guard misfire
            pytest.fail(f"read-only tmux call was blocked by the guard: {exc}")
        except FileNotFoundError:
            pass  # no tmux binary on this host — the guard still let it through

    def test_real_gh_is_blocked(self):
        """`gh` is an external service in the same class as tmux (#255).

        Path redirection cannot contain it, and an unmocked call does real
        damage twice over: gh_budget's probe alone is five reads at a 10s
        timeout each, and every call spends the user's real rate-limit
        budget — the exact resource #255 exists to protect.
        """
        import subprocess

        with pytest.raises(FileNotFoundError, match="real `gh`"):
            subprocess.run(["gh", "api", "rate_limit"], capture_output=True)

    def test_absolute_path_gh_is_blocked(self):
        import subprocess

        with pytest.raises(FileNotFoundError, match="real `gh`"):
            subprocess.run(["/opt/homebrew/bin/gh", "issue", "list"], capture_output=True)

    def test_gh_block_raises_what_callers_already_handle(self):
        """FileNotFoundError, not RuntimeError, on purpose.

        Every gh caller in the codebase already catches it (gh may genuinely
        be absent), so a missed mock degrades the way a missing binary would
        instead of erroring out of an unrelated code path — while the message
        still names the real cause.
        """
        from agent_hq import gh_budget

        assert gh_budget._read_once() is None

    def test_non_tmux_command_is_untouched(self):
        import subprocess

        result = subprocess.run(["echo", "ok"], capture_output=True, text=True)
        assert result.returncode == 0
        assert result.stdout.strip() == "ok"


class TestDaemonProcessScanGuard:
    """#333: ``start_daemon`` culls "orphan" daemons it finds with a real
    ``pgrep``. Path redirection hides the live daemon's PID file, so under
    test every live daemon on the machine looked like an orphan and was
    SIGTERM'd — each full suite run stopped the dashboard's daemon."""

    def test_real_pgrep_is_blocked(self):
        import subprocess

        with pytest.raises(FileNotFoundError, match="real `pgrep`"):
            subprocess.run(["pgrep", "-fl", "agent-hq.*daemon"], capture_output=True)

    def test_absolute_path_pgrep_is_blocked(self):
        import subprocess

        with pytest.raises(FileNotFoundError, match="real `pgrep`"):
            subprocess.run(["/usr/bin/pgrep", "-fl", "x"], capture_output=True)

    def test_orphan_scan_cannot_see_live_daemons(self):
        """A real process shaped like a daemon must stay invisible to the
        orphan scan, so ``kill_orphan_daemons`` has nothing to signal."""
        import subprocess
        import sys

        from agent_hq.daemon import _validate_daemon_cmdline, find_orphan_daemon_pids

        argv = [sys.executable, "-c", "import time; time.sleep(60)", "/x/agent-hq", "daemon"]
        assert _validate_daemon_cmdline(" ".join(argv))
        decoy = subprocess.Popen(argv)
        try:
            assert find_orphan_daemon_pids() == []
        finally:
            # The signal guard lets a test signal its own children.
            decoy.kill()
            decoy.wait()

    def test_popen_pgrep_is_blocked(self):
        import subprocess

        with pytest.raises(FileNotFoundError, match="real `pgrep`"):
            subprocess.Popen(["pgrep", "-fl", "agent-hq.*daemon"])


_LIVE_DAEMON_PS = (1, "/usr/bin/python3 /Users/u/.local/bin/agent-hq daemon start")


@pytest.fixture
def sleeper(_guard_daemon_signals):
    """A child the test owns, so a guard misfire can only signal it — never
    a guessed PID that may be live (Linux pid_max reaches 4194304)."""
    import subprocess

    proc = subprocess.Popen(["sleep", "30"])
    yield proc
    _guard_daemon_signals.ps_reader = lambda pid: None  # our own child
    proc.kill()
    proc.wait()


class TestDaemonSignalGuard:
    """#333 defense in depth: whatever finds the PID, a test must never
    signal an agent-hq process it did not spawn. ``ps_reader`` is faked to
    report the test's own sleeper as a foreign process."""

    def test_signal_to_foreign_agent_hq_process_is_refused(self, _guard_daemon_signals, sleeper):
        import os
        import signal

        _guard_daemon_signals.ps_reader = lambda pid: _LIVE_DAEMON_PS
        with pytest.raises(RuntimeError, match="#333"):
            os.kill(sleeper.pid, signal.SIGTERM)
        assert sleeper.poll() is None

    def test_orphan_cleanup_cannot_signal_live_daemon(
        self, _guard_daemon_signals, sleeper, monkeypatch
    ):
        """The #333 shape: an orphan scan that reports the live daemon."""
        from agent_hq import daemon

        _guard_daemon_signals.ps_reader = lambda pid: _LIVE_DAEMON_PS
        monkeypatch.setattr(daemon, "find_orphan_daemon_pids", lambda **kw: [sleeper.pid])
        with pytest.raises(RuntimeError, match="#333"):
            daemon.kill_orphan_daemons(silent=True)
        assert sleeper.poll() is None

    def test_signal_to_non_agent_hq_process_passes_through(self, _guard_daemon_signals, sleeper):
        import os
        import signal

        _guard_daemon_signals.ps_reader = lambda pid: (1, "/bin/sleep 30")
        os.kill(sleeper.pid, signal.SIGTERM)
        assert sleeper.wait(timeout=5) == -signal.SIGTERM

    def test_signal_zero_is_not_vetted(self, _guard_daemon_signals, sleeper):
        import os

        _guard_daemon_signals.ps_reader = lambda pid: _LIVE_DAEMON_PS
        os.kill(sleeper.pid, 0)
        assert sleeper.poll() is None


def test_claude_headless_spawn_is_blocked():
    """#248: the triage worker's ``claude -p`` boundary is injected in tests;
    a real spawn is a mocking gap and must fail loudly, like tmux mutations."""
    import subprocess

    with pytest.raises(RuntimeError, match="claude -p"):
        subprocess.run(
            ["claude", "-p", "--output-format", "json"], input="x", capture_output=True, text=True
        )
    with pytest.raises(RuntimeError, match="claude -p"):
        subprocess.Popen(["/usr/local/bin/claude", "--print"])


def test_runtime_environment_is_isolated():
    import os

    assert os.environ.get("AGENT_HQ_CODEX_WRAPPED") is None
