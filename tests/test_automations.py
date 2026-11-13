"""Tests for agent_hq.automations — scheduled-automation health (#54)."""

import os
import plistlib
import subprocess
from datetime import datetime, timedelta

import pytest

from agent_hq import automations
from agent_hq.automations import (
    AutomationsCache,
    AutomationStatus,
    collect_health,
    most_recent_fire_time,
    next_fire_time,
    parse_calendar_intervals,
    read_launchd_job,
    read_log_status,
    read_tail,
    tail_log,
)
from agent_hq.config import AutomationConfig

NOW = datetime(2026, 6, 10, 12, 0, 0)  # noon


class TestParseCalendarIntervals:
    def test_single_dict_normalized_to_list(self):
        plist = {"StartCalendarInterval": {"Hour": 6, "Minute": 0}}
        assert parse_calendar_intervals(plist) == [{"Hour": 6, "Minute": 0}]

    def test_list_of_dicts_passed_through(self):
        plist = {"StartCalendarInterval": [{"Hour": 6, "Minute": 0}, {"Hour": 18, "Minute": 30}]}
        assert parse_calendar_intervals(plist) == [
            {"Hour": 6, "Minute": 0},
            {"Hour": 18, "Minute": 30},
        ]

    def test_missing_key_returns_empty(self):
        assert parse_calendar_intervals({}) == []

    def test_interval_without_hour_skipped(self):
        # Hourly (Minute-only) and other exotic schedules are unsupported in v1.
        plist = {"StartCalendarInterval": [{"Minute": 15}, {"Hour": 3}]}
        assert parse_calendar_intervals(plist) == [{"Hour": 3}]


class TestFireTimes:
    def test_next_fire_later_today(self):
        intervals = [{"Hour": 17, "Minute": 30}]
        assert next_fire_time(intervals, NOW) == datetime(2026, 6, 10, 17, 30)

    def test_next_fire_rolls_to_tomorrow(self):
        intervals = [{"Hour": 6, "Minute": 0}]
        assert next_fire_time(intervals, NOW) == datetime(2026, 6, 11, 6, 0)

    def test_most_recent_fire_earlier_today(self):
        intervals = [{"Hour": 6, "Minute": 0}]
        assert most_recent_fire_time(intervals, NOW) == datetime(2026, 6, 10, 6, 0)

    def test_most_recent_fire_rolls_to_yesterday(self):
        intervals = [{"Hour": 17, "Minute": 30}]
        assert most_recent_fire_time(intervals, NOW) == datetime(2026, 6, 9, 17, 30)

    def test_multiple_intervals_pick_nearest(self):
        intervals = [{"Hour": 6, "Minute": 0}, {"Hour": 17, "Minute": 30}]
        assert next_fire_time(intervals, NOW) == datetime(2026, 6, 10, 17, 30)
        assert most_recent_fire_time(intervals, NOW) == datetime(2026, 6, 10, 6, 0)

    def test_minute_defaults_to_zero(self):
        intervals = [{"Hour": 3}]
        assert most_recent_fire_time(intervals, NOW) == datetime(2026, 6, 10, 3, 0)

    def test_empty_intervals_return_none(self):
        assert next_fire_time([], NOW) is None
        assert most_recent_fire_time([], NOW) is None

    def test_exact_fire_moment_counts_as_most_recent(self):
        intervals = [{"Hour": 12, "Minute": 0}]
        assert most_recent_fire_time(intervals, NOW) == datetime(2026, 6, 10, 12, 0)
        assert next_fire_time(intervals, NOW) == datetime(2026, 6, 11, 12, 0)


class TestLogReaders:
    def test_read_log_status_missing_file(self, tmp_path):
        mtime, last_line = read_log_status(tmp_path / "nope.log")
        assert mtime is None
        assert last_line is None

    def test_read_log_status_returns_mtime_and_last_nonempty_line(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_text("first line\nsecond line\n\n")
        mtime, last_line = read_log_status(log)
        assert isinstance(mtime, datetime)
        assert last_line == "second line"

    def test_read_log_status_empty_file(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_text("")
        mtime, last_line = read_log_status(log)
        assert isinstance(mtime, datetime)
        assert last_line is None

    def test_read_tail_missing_file_has_no_lines(self, tmp_path):
        assert read_tail(tmp_path / "nope.log") == ([], False)

    def test_read_tail_short_file_returns_every_line(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_text("a\nb\nc\n")
        assert read_tail(log) == (["a", "b", "c"], False)

    def test_read_tail_says_when_the_log_holds_more_than_it_read(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_text("first\n" + "x" * 99 + "\n" + ("y" * 99 + "\n") * 1000)
        lines, more_before = read_tail(log)
        assert more_before is True
        assert "first" not in lines
        assert set(lines) == {"y" * 99}  # the partial first line is dropped

    def test_tail_log_missing_file_returns_empty(self, tmp_path):
        assert tail_log(tmp_path / "nope.log") == ""

    def test_tail_log_short_file_returns_all(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_text("a\nb\nc\n")
        assert tail_log(log, n=50) == "a\nb\nc"

    def test_tail_log_returns_last_n_lines(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_text("".join(f"line{i}\n" for i in range(100)))
        result = tail_log(log, n=3)
        assert result == "line97\nline98\nline99"


def _write_plist(dir_path, label, hour, minute):
    plist_path = dir_path / f"{label}.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump(
            {"Label": label, "StartCalendarInterval": {"Hour": hour, "Minute": minute}}, f
        )
    return plist_path


def _write_log(dir_path, name, mtime, lines="run start\nrun done\n"):
    log = dir_path / f"{name}.log"
    log.write_text(lines)
    os.utime(log, (mtime.timestamp(), mtime.timestamp()))
    return log


def _config(name, tmp_path):
    return AutomationConfig(
        name=name, label=f"com.claude.{name}", log_file=tmp_path / f"{name}.log"
    )


class TestCollectHealth:
    """now = 2026-06-10 12:00. Daily job at 06:00 fired 6h ago."""

    def _collect(self, tmp_path, monkeypatch, exit_code, log_mtime, hour=6, plist=True):
        if plist:
            _write_plist(tmp_path, "com.claude.job", hour, 0)
        if log_mtime is not None:
            _write_log(tmp_path, "job", log_mtime)
        monkeypatch.setattr(automations, "read_launchd_job", lambda label, uid: _exited(exit_code))
        return collect_health(
            [_config("job", tmp_path)], now=NOW, uid=501, launch_agents_dir=tmp_path
        )[0]

    def test_ok_run_after_fire(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, 0, datetime(2026, 6, 10, 6, 5))
        assert health.status is AutomationStatus.OK
        assert health.last_run == datetime(2026, 6, 10, 6, 5)
        assert health.last_exit_code == 0
        assert health.next_run == datetime(2026, 6, 11, 6, 0)
        assert health.schedule_desc == "daily 06:00"
        assert health.last_log_line == "run done"

    def test_nonzero_exit_is_error(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, 78, datetime(2026, 6, 10, 6, 5))
        assert health.status is AutomationStatus.ERROR
        assert health.last_exit_code == 78

    def test_stale_log_past_grace_is_missed(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, 0, datetime(2026, 6, 9, 6, 5))
        assert health.status is AutomationStatus.MISSED
        assert health.last_fire == datetime(2026, 6, 10, 6, 0)

    def test_error_takes_precedence_over_missed(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, 1, datetime(2026, 6, 9, 6, 5))
        assert health.status is AutomationStatus.ERROR

    def test_within_grace_not_missed(self, tmp_path, monkeypatch):
        # Job fires at 11:50; now is 12:00, inside the 15-min grace window.
        _write_plist(tmp_path, "com.claude.job", 11, 50)
        _write_log(tmp_path, "job", datetime(2026, 6, 9, 11, 55))
        monkeypatch.setattr(automations, "read_launchd_job", lambda label, uid: _exited(0))
        health = collect_health(
            [_config("job", tmp_path)], now=NOW, uid=501, launch_agents_dir=tmp_path
        )[0]
        assert health.status is AutomationStatus.OK

    def test_never_ran_no_log_no_launchd_is_unknown(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, None, None, plist=False)
        assert health.status is AutomationStatus.UNKNOWN
        assert health.last_run is None
        assert health.next_run is None

    def test_no_plist_falls_back_to_exit_code_semantics(self, tmp_path, monkeypatch):
        # Stale log but no schedule to judge against: OK, never MISSED.
        health = self._collect(tmp_path, monkeypatch, 0, datetime(2026, 6, 1, 6, 5), plist=False)
        assert health.status is AutomationStatus.OK
        assert health.next_run is None
        assert health.schedule_desc is None

    def test_missed_with_no_log_file_at_all(self, tmp_path, monkeypatch):
        # Plist exists, job loaded (exit 0 recorded), but log never written
        # and the fire time has long passed.
        health = self._collect(tmp_path, monkeypatch, 0, None)
        assert health.status is AutomationStatus.MISSED

    def test_preserves_config_order(self, tmp_path, monkeypatch):
        _write_plist(tmp_path, "com.claude.a", 6, 0)
        _write_plist(tmp_path, "com.claude.b", 7, 0)
        _write_log(tmp_path, "a", datetime(2026, 6, 10, 6, 5))
        _write_log(tmp_path, "b", datetime(2026, 6, 10, 7, 5))
        monkeypatch.setattr(automations, "read_launchd_job", lambda label, uid: _exited(0))
        healths = collect_health(
            [_config("a", tmp_path), _config("b", tmp_path)],
            now=NOW,
            uid=501,
            launch_agents_dir=tmp_path,
        )
        assert [h.name for h in healths] == ["a", "b"]


class TestLogPatternStatus:
    """Config-driven success/failure patterns matched against the LAST log
    line override an OK verdict (#54 follow-up). They cover two real holes:
    a wrapper that exits 0 on logical failure, and launchd forgetting a
    non-zero exit after a reboot (``runs = 0`` / ``(never exited)``).

    Same time frame as TestCollectHealth: now = 2026-06-10 12:00, daily 06:00.
    """

    def _collect(
        self,
        tmp_path,
        monkeypatch,
        *,
        lines,
        failure_pattern=None,
        success_pattern=None,
        exit_code=0,
        log_mtime=datetime(2026, 6, 10, 6, 5),
    ):
        _write_plist(tmp_path, "com.claude.job", 6, 0)
        if lines is not None:
            _write_log(tmp_path, "job", log_mtime, lines=lines)
        monkeypatch.setattr(automations, "read_launchd_job", lambda label, uid: _exited(exit_code))
        config = AutomationConfig(
            name="job",
            label="com.claude.job",
            log_file=tmp_path / "job.log",
            failure_pattern=failure_pattern,
            success_pattern=success_pattern,
        )
        return collect_health([config], now=NOW, uid=501, launch_agents_dir=tmp_path)[0]

    def test_failure_pattern_match_is_error_despite_exit_zero(self, tmp_path, monkeypatch):
        health = self._collect(
            tmp_path,
            monkeypatch,
            lines="run start\nDigest finished but no file was created\n",
            failure_pattern="no file was created",
            exit_code=0,
        )
        assert health.status is AutomationStatus.ERROR

    def test_failure_pattern_match_is_error_despite_no_launchd_record(self, tmp_path, monkeypatch):
        # The reboot-amnesia case: wrapper exited 1 yesterday, but launchd
        # was rebooted since and reports no exit code at all.
        health = self._collect(
            tmp_path,
            monkeypatch,
            lines="run start\nNightly backup FAILED — no report created\n",
            failure_pattern="FAILED",
            exit_code=None,
        )
        assert health.status is AutomationStatus.ERROR

    def test_failure_pattern_no_match_is_ok(self, tmp_path, monkeypatch):
        health = self._collect(
            tmp_path,
            monkeypatch,
            lines="run start\nBackup report complete — saved to /x.md\n",
            failure_pattern="FAILED",
        )
        assert health.status is AutomationStatus.OK

    def test_success_pattern_absent_from_last_line_is_error(self, tmp_path, monkeypatch):
        health = self._collect(
            tmp_path,
            monkeypatch,
            lines="run start\nsomething unexpected happened\n",
            success_pattern="Digest complete",
        )
        assert health.status is AutomationStatus.ERROR

    def test_success_pattern_match_is_ok(self, tmp_path, monkeypatch):
        health = self._collect(
            tmp_path,
            monkeypatch,
            lines="run start\nDigest complete — saved to /x.md\n",
            success_pattern="Digest complete",
        )
        assert health.status is AutomationStatus.OK

    def test_patterns_check_only_the_last_line(self, tmp_path, monkeypatch):
        # Yesterday's FAILED line earlier in the log must not poison a run
        # whose final verdict is success.
        health = self._collect(
            tmp_path,
            monkeypatch,
            lines="Nightly backup FAILED — no report created\nBackup report complete\n",
            failure_pattern="FAILED",
        )
        assert health.status is AutomationStatus.OK

    def test_pattern_error_takes_precedence_over_missed(self, tmp_path, monkeypatch):
        # Stale log past grace AND a failure verdict: surface the ERROR,
        # matching the exit-code precedence rule.
        health = self._collect(
            tmp_path,
            monkeypatch,
            lines="run start\njob FAILED\n",
            failure_pattern="FAILED",
            log_mtime=datetime(2026, 6, 9, 6, 5),
        )
        assert health.status is AutomationStatus.ERROR

    def test_success_pattern_with_no_log_is_not_error(self, tmp_path, monkeypatch):
        # No log file at all: patterns have nothing to judge; the missed
        # rule (fire time long past, log never written) decides instead.
        health = self._collect(
            tmp_path,
            monkeypatch,
            lines=None,
            success_pattern="complete",
        )
        assert health.status is AutomationStatus.MISSED


class TestAutomationsCache:
    def test_fetched_at_is_when_the_loader_last_ran(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(automations.time, "time", lambda: clock[0])
        cache = AutomationsCache(ttl=30)
        assert cache.fetched_at() is None
        cache.fetch(lambda: ["health"])
        clock[0] = 1010.0
        cache.fetch(lambda: ["never called"])  # TTL hit keeps the stamp
        assert cache.fetched_at() == 1000.0

    def test_fetch_calls_loader_and_caches(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(automations.time, "time", lambda: clock[0])
        cache = AutomationsCache(ttl=30)
        calls = []

        def loader():
            calls.append(1)
            return ["health"]

        assert cache.fetch(loader) == ["health"]
        assert cache.fetch(loader) == ["health"]
        assert len(calls) == 1

    def test_fetch_reloads_after_ttl(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(automations.time, "time", lambda: clock[0])
        cache = AutomationsCache(ttl=30)
        calls = []

        def loader():
            calls.append(1)
            return ["health"]

        cache.fetch(loader)
        clock[0] += 31
        cache.fetch(loader)
        assert len(calls) == 2

    def test_fetch_force_bypasses_ttl(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(automations.time, "time", lambda: clock[0])
        cache = AutomationsCache(ttl=30)
        calls = []

        def loader():
            calls.append(1)
            return []

        cache.fetch(loader)
        cache.fetch(loader, force=True)
        assert len(calls) == 2

    def test_get_cached_none_before_first_fetch(self):
        cache = AutomationsCache(ttl=30)
        assert cache.get_cached() is None

    def test_get_cached_fresh_within_ttl(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(automations.time, "time", lambda: clock[0])
        cache = AutomationsCache(ttl=30)
        cache.fetch(lambda: ["health"])
        healths, fresh = cache.get_cached()
        assert healths == ["health"]
        assert fresh is True

    def test_get_cached_returns_stale_with_flag(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(automations.time, "time", lambda: clock[0])
        cache = AutomationsCache(ttl=30)
        cache.fetch(lambda: ["health"])
        clock[0] += 31
        healths, fresh = cache.get_cached()
        assert healths == ["health"]
        assert fresh is False

    def test_get_cached_never_calls_loader(self):
        cache = AutomationsCache(ttl=30)
        cache.get_cached()  # must not raise / require a loader


class TestParseCalendarIntervalsValidation:
    """Review findings: non-daily schedules must degrade, malformed must not crash."""

    def test_interval_with_weekday_dropped(self):
        # Weekly schedule — treating it as daily would false-flag MISSED.
        plist = {"StartCalendarInterval": {"Weekday": 1, "Hour": 6, "Minute": 0}}
        assert parse_calendar_intervals(plist) == []

    def test_interval_with_day_or_month_dropped(self):
        plist = {
            "StartCalendarInterval": [
                {"Day": 1, "Hour": 6},
                {"Month": 3, "Hour": 6},
                {"Hour": 6, "Minute": 30},
            ]
        }
        assert parse_calendar_intervals(plist) == [{"Hour": 6, "Minute": 30}]

    def test_non_dict_non_list_value_returns_empty(self):
        assert parse_calendar_intervals({"StartCalendarInterval": 5}) == []
        assert parse_calendar_intervals({"StartCalendarInterval": "06:00"}) == []

    def test_string_hour_dropped(self):
        plist = {"StartCalendarInterval": {"Hour": "6", "Minute": 0}}
        assert parse_calendar_intervals(plist) == []

    def test_out_of_range_hour_or_minute_dropped(self):
        plist = {"StartCalendarInterval": [{"Hour": 25}, {"Hour": 6, "Minute": 75}]}
        assert parse_calendar_intervals(plist) == []


def _exited(exit_code):
    """A job launchd has loaded and that isn't running."""
    return automations.LaunchdJob(running=False, pid=None, exit_code=exit_code)


class TestReadLaunchdJob:
    """#298: daemons need launchd's running state, not just the exit code."""

    def _completed(self, returncode, stdout=""):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)

    def test_running_job_with_previous_crash_code(self, monkeypatch):
        out = "com.claude.d = {\n\tstate = running\n\tpid = 4242\n\tlast exit code = 1\n}\n"
        monkeypatch.setattr(automations.subprocess, "run", lambda *a, **k: self._completed(0, out))
        job = read_launchd_job("com.claude.d", uid=501)
        assert (job.running, job.pid, job.exit_code) == (True, 4242, 1)

    def test_loaded_but_not_running(self, monkeypatch):
        out = "\tstate = not running\n\tlast exit code = 0\n"
        monkeypatch.setattr(automations.subprocess, "run", lambda *a, **k: self._completed(0, out))
        job = read_launchd_job("com.claude.d", uid=501)
        assert (job.running, job.pid, job.exit_code) == (False, None, 0)

    def test_never_exited_has_no_exit_code(self, monkeypatch):
        out = "\tstate = running\n\tpid = 1\n\tlast exit code = (never exited)\n"
        monkeypatch.setattr(automations.subprocess, "run", lambda *a, **k: self._completed(0, out))
        assert read_launchd_job("com.claude.d", uid=501).exit_code is None

    def test_reads_the_keepalive_property(self, monkeypatch):
        out = "\tstate = running\n\tproperties = keepalive | runatload | inferred program\n"
        monkeypatch.setattr(automations.subprocess, "run", lambda *a, **k: self._completed(0, out))
        assert read_launchd_job("com.claude.d", uid=501).keepalive is True

    def test_other_properties_arent_keepalive(self, monkeypatch):
        out = "\tjetsamproperties category = keepalive\n\tproperties = runatload\n"
        monkeypatch.setattr(automations.subprocess, "run", lambda *a, **k: self._completed(0, out))
        assert read_launchd_job("com.claude.d", uid=501).keepalive is False

    def test_not_loaded_is_a_job_that_isnt_loaded(self, monkeypatch):
        # launchctl exits 113, "Could not find service", for a job it hasn't loaded.
        monkeypatch.setattr(automations.subprocess, "run", lambda *a, **k: self._completed(113, ""))
        job = read_launchd_job("com.claude.gone", uid=501)
        assert (job.loaded, job.running, job.exit_code) == (False, False, None)

    def test_other_launchctl_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(automations.subprocess, "run", lambda *a, **k: self._completed(5, ""))
        assert read_launchd_job("com.claude.d", uid=501) is None

    def test_launchctl_timeout_returns_none(self, monkeypatch):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired("launchctl", 5)

        monkeypatch.setattr(automations.subprocess, "run", slow)
        assert read_launchd_job("com.claude.d", uid=501) is None

    def test_launchctl_missing_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("launchctl")

        monkeypatch.setattr(automations.subprocess, "run", boom)
        assert read_launchd_job("com.claude.d", uid=501) is None


class TestCollectHealthDaemon:
    """#298: entries with stale_after are KeepAlive daemons. now = 12:00."""

    def _collect(
        self,
        tmp_path,
        monkeypatch,
        job,
        log_mtime,
        lines="heartbeat ok\n",
        failure_pattern="FAILED",
    ):
        if log_mtime is not None:
            _write_log(tmp_path, "daemon", log_mtime, lines=lines)
        monkeypatch.setattr(automations, "read_launchd_job", lambda label, uid: job)
        config = AutomationConfig(
            name="daemon",
            label="com.claude.daemon",
            log_file=tmp_path / "daemon.log",
            failure_pattern=failure_pattern,
            stale_after=timedelta(minutes=3),
        )
        return collect_health([config], now=NOW, uid=501, launch_agents_dir=tmp_path)[0]

    def _job(self, running=True, exit_code=0):
        return automations.LaunchdJob(
            running=running, pid=4242 if running else None, exit_code=exit_code
        )

    def test_running_with_fresh_heartbeat_is_ok(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(), datetime(2026, 6, 10, 11, 59))
        assert health.status is AutomationStatus.OK
        assert health.running is True
        assert health.next_run is None

    def test_running_but_silent_past_stale_after_is_stale(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(), datetime(2026, 6, 10, 11, 50))
        assert health.status is AutomationStatus.STALE

    def test_not_loaded_is_stopped(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, None, datetime(2026, 6, 10, 11, 59))
        assert health.status is AutomationStatus.STOPPED
        assert health.running is False

    def test_loaded_not_running_is_stopped(self, tmp_path, monkeypatch):
        health = self._collect(
            tmp_path,
            monkeypatch,
            self._job(running=False, exit_code=1),
            datetime(2026, 6, 10, 11, 59),
        )
        assert health.status is AutomationStatus.STOPPED
        assert health.last_exit_code == 1

    def test_previous_crash_code_ignored_while_running(self, tmp_path, monkeypatch):
        health = self._collect(
            tmp_path, monkeypatch, self._job(exit_code=1), datetime(2026, 6, 10, 11, 59)
        )
        assert health.status is AutomationStatus.OK

    def test_failure_line_is_error(self, tmp_path, monkeypatch):
        health = self._collect(
            tmp_path,
            monkeypatch,
            self._job(),
            datetime(2026, 6, 10, 11, 59),
            lines="heartbeat ok\npoll FAILED: db down\n",
        )
        assert health.status is AutomationStatus.ERROR

    def test_no_log_yet_but_running_is_stale(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(), None)
        assert health.status is AutomationStatus.STALE


class TestKeepAliveExitCode:
    """launchd keeps a job's last exit code across restarts. Exit 15 is how it stops
    a job for a restart, so on a KeepAlive job that says nothing; any other code may
    be the only sign of a crash loop."""

    def _collect(self, tmp_path, monkeypatch, job, lines="fine\n"):
        _write_log(tmp_path, "d", datetime(2026, 6, 10, 11, 0), lines=lines)
        monkeypatch.setattr(automations, "read_launchd_job", lambda label, uid: job)
        config = AutomationConfig(
            name="d",
            label="com.claude.d",
            log_file=tmp_path / "d.log",
            failure_pattern="ERROR",
        )
        return collect_health([config], now=NOW, uid=501, launch_agents_dir=tmp_path)[0]

    def _job(self, exit_code=15, running=True, keepalive=True):
        return automations.LaunchdJob(
            running=running,
            pid=4242 if running else None,
            exit_code=exit_code,
            keepalive=keepalive,
        )

    def test_a_restart_isnt_an_error_while_it_runs(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job())
        assert health.status is AutomationStatus.OK

    def test_a_restart_isnt_an_error_in_the_gap_before_it_comes_back(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(running=False))
        assert health.status is AutomationStatus.OK

    def test_a_restarts_exit_code_isnt_reported_as_a_result(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job())
        assert health.last_exit_code is None

    def test_a_crash_stays_an_error_while_it_runs(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(exit_code=1))
        assert health.status is AutomationStatus.ERROR
        assert health.last_exit_code == 1

    def test_a_failure_in_the_log_is_still_an_error(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(), lines="ERROR boom\n")
        assert health.status is AutomationStatus.ERROR
        assert health.last_exit_code is None

    def test_exit_15_without_keepalive_stays_an_error(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(keepalive=False))
        assert health.status is AutomationStatus.ERROR
        assert health.last_exit_code == 15


class TestReadLaunchdSignal:
    """#427: a job killed by a signal has no exit code; launchd reports the signal."""

    def _job(self, monkeypatch, out):
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=out)
        monkeypatch.setattr(automations.subprocess, "run", lambda *a, **k: done)
        return read_launchd_job("com.claude.d", uid=501)

    def test_reads_the_terminating_signal(self, monkeypatch):
        out = "\tstate = not running\n\truns = 3\n\tlast terminating signal = Killed: 9\n"
        job = self._job(monkeypatch, out)
        assert (job.exit_code, job.signal) == (None, 9)

    def test_no_signal_line_has_no_signal(self, monkeypatch):
        job = self._job(monkeypatch, "\tstate = not running\n\tlast exit code = 0\n")
        assert job.signal is None


class TestSignalHealth:
    """#427: a signal is judged like an exit code. SIGTERM on a KeepAlive job is how
    launchd restarts it (#422's exit 15); any other signal is a failure."""

    def _collect(self, tmp_path, monkeypatch, job, lines="fine\n"):
        _write_log(tmp_path, "d", datetime(2026, 6, 10, 11, 0), lines=lines)
        monkeypatch.setattr(automations, "read_launchd_job", lambda label, uid: job)
        config = AutomationConfig(name="d", label="com.claude.d", log_file=tmp_path / "d.log")
        return collect_health([config], now=NOW, uid=501, launch_agents_dir=tmp_path)[0]

    def _job(self, signal, running=False, keepalive=False):
        return automations.LaunchdJob(
            running=running,
            pid=4242 if running else None,
            exit_code=None,
            keepalive=keepalive,
            signal=signal,
        )

    def test_a_scheduled_job_killed_by_a_signal_is_an_error(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(9))
        assert health.status is AutomationStatus.ERROR
        assert health.last_signal == 9

    def test_sigterm_on_a_scheduled_job_is_an_error(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(15))
        assert health.status is AutomationStatus.ERROR

    def test_sigterm_on_a_running_keepalive_job_is_a_restart(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(15, running=True, keepalive=True))
        assert health.status is AutomationStatus.OK
        assert health.last_signal is None

    def test_sigterm_on_a_keepalive_job_in_the_restart_gap_is_ok(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(15, keepalive=True))
        assert health.status is AutomationStatus.OK

    def test_sigkill_on_a_keepalive_job_stays_an_error_once_it_runs_again(
        self, tmp_path, monkeypatch
    ):
        # Like a crash's exit code: without a heartbeat, an OOM kill loop restarts
        # between kills and would otherwise read healthy.
        health = self._collect(tmp_path, monkeypatch, self._job(9, running=True, keepalive=True))
        assert health.status is AutomationStatus.ERROR
        assert health.last_signal == 9


def _write_keepalive_plist(dir_path, label, keepalive=True, suffix=".plist", **extra):
    with open(dir_path / f"{label}{suffix}", "wb") as f:
        plistlib.dump({"Label": label, "KeepAlive": keepalive, **extra}, f)


NOT_LOADED = automations.LaunchdJob(running=False, pid=None, exit_code=None, loaded=False)


class TestKeepAliveWithoutStaleAfter:
    """#426: a KeepAlive job with no stale_after has no heartbeat to check, but it
    must still be running. Not running, or not loaded at all, is STOPPED."""

    def _collect(self, tmp_path, monkeypatch, job, lines="fine\n"):
        _write_log(tmp_path, "d", datetime(2026, 6, 10, 11, 0), lines=lines)
        monkeypatch.setattr(automations, "read_launchd_job", lambda label, uid: job)
        config = AutomationConfig(
            name="d", label="com.claude.d", log_file=tmp_path / "d.log", failure_pattern="ERROR"
        )
        return collect_health([config], now=NOW, uid=501, launch_agents_dir=tmp_path)[0]

    def _job(self, running, exit_code=None, keepalive=True):
        return automations.LaunchdJob(
            running=running,
            pid=4242 if running else None,
            exit_code=exit_code,
            keepalive=keepalive,
        )

    def test_running_is_ok(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(running=True))
        assert health.status is AutomationStatus.OK

    def test_loaded_but_not_running_is_stopped(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(running=False, exit_code=0))
        assert health.status is AutomationStatus.STOPPED

    def test_never_started_is_stopped(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(running=False))
        assert health.status is AutomationStatus.STOPPED

    def test_unloaded_keepalive_plist_is_stopped(self, tmp_path, monkeypatch):
        _write_keepalive_plist(tmp_path, "com.claude.d")
        health = self._collect(tmp_path, monkeypatch, NOT_LOADED)
        assert health.status is AutomationStatus.STOPPED

    def test_unreadable_launchctl_isnt_called_stopped(self, tmp_path, monkeypatch):
        # A launchctl timeout says nothing about the job; it isn't an outage.
        _write_keepalive_plist(tmp_path, "com.claude.d")
        health = self._collect(tmp_path, monkeypatch, None)
        assert health.status is AutomationStatus.OK

    def test_conditional_keepalive_can_rightly_be_down(self, tmp_path, monkeypatch):
        # KeepAlive {SuccessfulExit: false} leaves the job down after a clean exit,
        # even though launchctl lists it as keepalive.
        _write_keepalive_plist(tmp_path, "com.claude.d", keepalive={"SuccessfulExit": False})
        health = self._collect(tmp_path, monkeypatch, self._job(running=False, exit_code=0))
        assert health.status is AutomationStatus.OK

    def test_killed_and_disabled_plist_is_stopped(self, tmp_path, monkeypatch):
        # `agent-hq remote kill` boots the job out and renames its plist.
        _write_keepalive_plist(tmp_path, "com.claude.d", suffix=".plist.disabled")
        health = self._collect(tmp_path, monkeypatch, NOT_LOADED)
        assert health.status is AutomationStatus.STOPPED

    def test_the_live_plist_wins_over_a_leftover_disabled_one(self, tmp_path, monkeypatch):
        _write_keepalive_plist(tmp_path, "com.claude.d", keepalive=False)
        _write_keepalive_plist(tmp_path, "com.claude.d", suffix=".plist.disabled")
        health = self._collect(tmp_path, monkeypatch, NOT_LOADED)
        assert health.status is AutomationStatus.OK

    def test_unloaded_job_without_keepalive_keeps_the_scheduled_rules(self, tmp_path, monkeypatch):
        _write_keepalive_plist(tmp_path, "com.claude.d", keepalive=False)
        health = self._collect(tmp_path, monkeypatch, NOT_LOADED)
        assert health.status is AutomationStatus.OK

    @pytest.mark.parametrize(
        "schedule",
        [
            {"StartCalendarInterval": {"Weekday": 1, "Hour": 6, "Minute": 0}},
            {"StartCalendarInterval": [{"Day": 1, "Hour": 6}]},
            {"StartInterval": 900},
        ],
    )
    def test_a_job_on_any_schedule_waits_between_runs(self, tmp_path, monkeypatch, schedule):
        # Only daily schedules are parsed into fire times; any schedule still counts.
        _write_keepalive_plist(tmp_path, "com.claude.d", **schedule)
        health = self._collect(tmp_path, monkeypatch, self._job(running=False, exit_code=0))
        assert health.status is AutomationStatus.OK

    @pytest.mark.parametrize(
        "body", [b"<plist><dict><key>K", b"<plist><integer>x</integer></plist>"]
    )
    def test_a_malformed_plist_doesnt_break_collection(self, tmp_path, monkeypatch, body):
        (tmp_path / "com.claude.d.plist").write_bytes(body)
        health = self._collect(tmp_path, monkeypatch, self._job(running=True))
        assert health.status is AutomationStatus.OK

    def test_a_crash_while_down_is_still_an_error(self, tmp_path, monkeypatch):
        health = self._collect(tmp_path, monkeypatch, self._job(running=False, exit_code=1))
        assert health.status is AutomationStatus.ERROR
        assert health.last_exit_code == 1

    def test_a_scheduled_keepalive_job_keeps_the_scheduled_rules(self, tmp_path, monkeypatch):
        with open(tmp_path / "com.claude.d.plist", "wb") as f:
            plistlib.dump(
                {
                    "Label": "com.claude.d",
                    "KeepAlive": True,
                    "StartCalendarInterval": {"Hour": 6, "Minute": 0},
                },
                f,
            )
        health = self._collect(tmp_path, monkeypatch, self._job(running=False, exit_code=0))
        assert health.status is AutomationStatus.OK


class TestNotLoadedKeepsTheOldOutcomes:
    """Review round 2: an unloaded job became LaunchdJob(loaded=False) instead of
    None; every outcome outside the KeepAlive rule stays as it was."""

    def test_unloaded_scheduled_job_with_no_log_is_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(automations, "read_launchd_job", lambda label, uid: NOT_LOADED)
        health = collect_health(
            [_config("job", tmp_path)], now=NOW, uid=501, launch_agents_dir=tmp_path
        )[0]
        assert health.status is AutomationStatus.UNKNOWN

    def test_unloaded_daemon_is_stopped_with_no_exit_code(self, tmp_path, monkeypatch):
        _write_log(tmp_path, "daemon", datetime(2026, 6, 10, 11, 59))
        monkeypatch.setattr(automations, "read_launchd_job", lambda label, uid: NOT_LOADED)
        config = AutomationConfig(
            name="daemon",
            label="com.claude.daemon",
            log_file=tmp_path / "daemon.log",
            stale_after=timedelta(minutes=3),
        )
        health = collect_health([config], now=NOW, uid=501, launch_agents_dir=tmp_path)[0]
        assert (health.status, health.running, health.last_exit_code) == (
            AutomationStatus.STOPPED,
            False,
            None,
        )
