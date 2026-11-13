"""Remote-mode faults reach the unified logger through eventlog (#396).

``logger_events`` implements the hooks the vendored notes-graph ``auth.py``
and ``request_log.py`` call (#180 upstream), mapped onto agent-hq's eventlog;
``EventingRequestLog`` forwards the security-relevant request-log events.
Metadata only: never logins, paths, tokens, arguments or titles.
"""

import logging

import httpx
import pytest

from agent_hq import eventlog
from agent_hq.remote import logger_events
from agent_hq.remote.events import EventingRequestLog


@pytest.fixture
def emitted(monkeypatch):
    calls = []

    def fake_emit(level, message, json_extra=None, *, min_interval_s=60.0, sync=False):
        calls.append((level, message, dict(json_extra or {}), sync))
        return True

    monkeypatch.setattr(eventlog, "emit", fake_emit)
    return calls


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# --- exit-time reports, throttled across launchd restarts ---------------


def test_config_failure_is_reported_once_per_window(emitted):
    clock = _Clock()
    logger_events.report_config_failure(ValueError("secret /path"), clock)
    logger_events.report_config_failure(ValueError("again"), clock)
    assert emitted == [
        ("error", "remote config or secret failed to load", {"error_type": "ValueError"}, True)
    ]


def test_config_failure_reports_again_after_the_window(emitted):
    clock = _Clock()
    logger_events.report_config_failure(ValueError(), clock)
    clock.t += logger_events.RESTART_WINDOW_S + 1
    logger_events.report_config_failure(ValueError(), clock)
    assert len(emitted) == 2


def test_stamp_file_survives_a_new_process(emitted):
    logger_events.report_server_failure("serve", RuntimeError("x"), _Clock())
    assert logger_events.STAMP_FILE.exists()
    logger_events.report_server_failure("serve", RuntimeError("y"), _Clock(1001.0))
    assert len(emitted) == 1
    assert emitted[0][:3] == (
        "error",
        "remote server exited with an error",
        {"phase": "serve", "error_type": "RuntimeError"},
    )


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(0), SystemExit(None)])
def test_clean_exits_are_not_reported(emitted, exc):
    logger_events.report_server_failure("serve", exc, _Clock())
    assert emitted == []


def test_bind_failure_exit_is_reported(emitted):
    logger_events.report_server_failure("serve", SystemExit(1), _Clock())
    assert emitted[0][2] == {"phase": "serve", "error_type": "SystemExit"}


# --- unhandled exceptions -------------------------------------------------


def _record(exc, name="mcp.server", level=logging.ERROR):
    try:
        raise exc
    except BaseException as e:  # noqa: BLE001 - building a real exc_info
        import sys

        return logging.LogRecord(name, level, __file__, 1, "boom %s", ("t",), sys.exc_info()), e


def test_unhandled_exception_is_reported_with_type_only(emitted):
    handler = logger_events.UnhandledExceptionHandler()
    record, _ = _record(KeyError("Secret Issue Title"))
    handler.emit(record)
    assert emitted == [
        (
            "error",
            "remote server: unhandled exception",
            {"logger": "mcp.server", "level": "ERROR", "error_type": "KeyError"},
            False,
        )
    ]


def test_records_without_exc_info_and_reported_faults_are_skipped(emitted):
    handler = logger_events.UnhandledExceptionHandler()
    handler.emit(logging.LogRecord("x", logging.ERROR, __file__, 1, "plain", None, None))
    record, exc = _record(RuntimeError())
    eventlog.mark_reported(exc)
    handler.emit(record)
    assert emitted == []


def test_repeated_unhandled_exception_is_throttled(emitted):
    clock = _Clock()
    handler = logger_events.UnhandledExceptionHandler(clock=clock)
    for _ in range(3):
        handler.emit(_record(RuntimeError())[0])
    assert len(emitted) == 1
    clock.t += logger_events.UNHANDLED_WINDOW_S + 1
    handler.emit(_record(RuntimeError())[0])
    assert len(emitted) == 2


# --- GitHub token exchange streaks -----------------------------------------


def test_exchange_monitor_reports_once_per_failure_streak(emitted):
    monitor = logger_events.TokenExchangeMonitor(clock=_Clock())
    for _ in range(5):
        monitor.failure(httpx.ConnectError("down"))
    assert len(emitted) == 1
    level, message, extra, _sync = emitted[0]
    assert (level, message) == ("error", "remote GitHub token exchange failing repeatedly")
    assert extra == {"failures": 3, "error_type": "ConnectError"}


def test_exchange_monitor_success_resets_the_streak(emitted):
    monitor = logger_events.TokenExchangeMonitor(clock=_Clock())
    monitor.failure(httpx.ConnectError("x"))
    monitor.failure(httpx.ConnectError("x"))
    monitor.success()
    monitor.failure(httpx.ConnectError("x"))
    assert emitted == []


def test_exchange_monitor_ignores_bad_verification_codes(emitted):
    from agent_hq.remote.auth import GitHubTokenError

    monitor = logger_events.TokenExchangeMonitor(clock=_Clock())
    for _ in range(5):
        monitor.failure(GitHubTokenError("bad_verification_code"))
    assert emitted == []


def test_exchange_monitor_records_safe_github_error_code_only(emitted):
    from agent_hq.remote.auth import GitHubTokenError

    monitor = logger_events.TokenExchangeMonitor(threshold=1, clock=_Clock())
    monitor.failure(GitHubTokenError("incorrect_client_credentials"))
    assert emitted[0][2]["upstream_error"] == "incorrect_client_credentials"
    monitor2 = logger_events.TokenExchangeMonitor(threshold=1, clock=_Clock())
    monitor2.failure(GitHubTokenError("<script>"))
    assert "upstream_error" not in emitted[1][2]


# --- request-log bridge ----------------------------------------------------


@pytest.mark.parametrize(
    "event, fields, expected",
    [
        (
            "sign_in",
            {"decision": "deny", "reason": "identity not allowed", "login": "eve"},
            ("warn", "remote sign-in denied: identity not allowed"),
        ),
        (
            "token",
            {"decision": "deny", "reason": "reuse detected: token family revoked"},
            ("error", "remote token reuse detected"),
        ),
        (
            "tool",
            {
                "decision": "allow",
                "outcome": "error:KeyError",
                "tool": "list_issues",
                "subject": "b",
            },
            ("warn", "remote tool failed"),
        ),
    ],
)
def test_bridge_forwards_security_events(emitted, tmp_path, event, fields, expected):
    log = EventingRequestLog(tmp_path / "requests.log")
    log.event(event, **fields)
    assert len(emitted) == 1
    level, message, extra, _ = emitted[0]
    assert (level, message) == expected
    assert "login" not in extra and "subject" not in extra
    assert (tmp_path / "requests.log").read_text().count("\n") == 1  # still written locally


def test_bridge_ignores_routine_events(emitted, tmp_path):
    log = EventingRequestLog(tmp_path / "requests.log")
    log.event("tool", decision="allow", outcome="ok", tool="list_agents")
    log.event("sign_in", decision="allow", login="ben-ralston")
    log.event("http", method="GET", path="/mcp", status=200)
    assert emitted == []


def test_eventlog_sync_delivers_inline(monkeypatch):
    delivered = []

    class Transport:
        @staticmethod
        def log(app, level, message, extra):
            delivered.append((level, message))

    monkeypatch.setattr(eventlog, "_transport", lambda: Transport)
    monkeypatch.setattr(eventlog, "_role", "remote")
    monkeypatch.setattr(eventlog, "_last_emit", {})
    assert eventlog.emit("error", "remote test sync", sync=True)
    assert delivered == [("error", "remote test sync")]
