"""Tests for the `agent-hq recover` diff + report. #125 #135"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime

import pytest

from agent_hq.models import Agent, AgentType
from agent_hq.recover import (
    CLIFF_LOOKBACK_SECONDS,
    CLIFF_MERGE_WINDOW_SECONDS,
    Baseline,
    compute_missing,
    exclude_self,
    format_json,
    format_report,
    identity,
    parse_since,
    record_identity,
    resolve_from_transcripts,
    resume_command,
    run_recover,
    select_baseline,
    split_possibly_self,
)
from agent_hq.snapshots import SnapshotStore


def _rec(
    *,
    session_id="s1",
    stable_id=None,
    tool="claude",
    cwd="/wt/a",
    repo="agent-hq",
    branch="feature/125-x",
    issue=125,
    state="running",
    phase="implement",
    tmux_session="agent-hq-125",
    pane="%5",
) -> dict:
    return {
        "session_id": session_id,
        "stable_id": stable_id if stable_id is not None else session_id,
        "tool": tool,
        "cwd": cwd,
        "repo": repo,
        "branch": branch,
        "issue": issue,
        "tmux_session": tmux_session,
        "pane": pane,
        "state": state,
        "phase": phase,
    }


def _record(*agent_recs, timestamp=100.0) -> dict:
    return {"timestamp": timestamp, "agents": list(agent_recs)}


def _live(transcript_id="s1", tool=AgentType.CLAUDE, cwd="/wt/a", pane="%9") -> Agent:
    return Agent(
        pane_id=pane,
        session_name="live",
        window_index=0,
        pane_index=0,
        agent_type=tool,
        cwd=cwd,
        raw_command="claude",
        claude_transcript_session_id=transcript_id,
    )


def _codex_agent(cwd="/wt/c", pane="%9", codex_thread_id=None) -> Agent:
    return Agent(
        pane_id=pane,
        session_name="live",
        window_index=0,
        pane_index=0,
        agent_type=AgentType.CODEX,
        cwd=cwd,
        raw_command="codex",
        codex_thread_id=codex_thread_id,
    )


def _baseline(record, reason="latest snapshot", newer=0) -> Baseline:
    return Baseline(record=record, reason=reason, newer_records=newer)


class TestIdentity:
    def test_uses_session_id_when_present(self):
        assert identity("sess", "claude", "/wt/a") == "sess"

    def test_falls_back_to_tool_and_cwd(self):
        assert identity(None, "codex", "/wt/a") == "codex:/wt/a"


class TestRecordIdentity:
    def test_record_identity_prefers_stable_id(self):
        assert (
            record_identity({"stable_id": "new", "session_id": "old", "tool": "codex", "cwd": "/w"})
            == "new"
        )

    def test_record_identity_old_record_falls_back(self):
        assert record_identity({"session_id": "sess-1", "tool": "claude", "cwd": "/w"}) == "sess-1"
        assert record_identity({"tool": "codex", "cwd": "/w"}) == "codex:/w"


class TestComputeMissing:
    def test_no_record_means_nothing_missing(self):
        assert compute_missing(None, [_live()]) == []

    def test_all_live_means_nothing_missing(self):
        rec = _record(_rec(session_id="s1"), _rec(session_id="s2", pane="%6"))
        live = [_live("s1"), _live("s2")]
        assert compute_missing(rec, live) == []

    def test_lists_agents_absent_from_live_set(self):
        rec = _record(
            _rec(session_id="s1"),
            _rec(session_id="s2", pane="%6"),
            _rec(session_id="s3", pane="%7"),
        )
        live = [_live("s2")]  # only s2 survived
        missing = compute_missing(rec, live)
        assert {m.record["session_id"] for m in missing} == {"s1", "s3"}

    def test_non_claude_matched_by_tool_and_cwd(self):
        rec = _record(_rec(session_id=None, tool="codex", cwd="/wt/c"))
        live = [_live(transcript_id=None, tool=AgentType.CODEX, cwd="/wt/c")]
        assert compute_missing(rec, live) == []

    def test_duplicate_sessionless_identity_not_collapsed(self):
        # Two session-less agents in the same cwd: one survives, one is gone.
        # A set-based diff would hide the dead one; the count must be honored. #125
        rec = _record(
            _rec(session_id=None, tool="codex", cwd="/wt/c", pane="%1"),
            _rec(session_id=None, tool="codex", cwd="/wt/c", pane="%2"),
        )
        live = [_live(transcript_id=None, tool=AgentType.CODEX, cwd="/wt/c")]  # only one survived
        assert len(compute_missing(rec, live)) == 1

    def test_compute_missing_matches_codex_by_thread_id(self):
        rec = _rec(tool="codex", session_id="uuid-7", stable_id="uuid-7", cwd="/w")
        live = _codex_agent(cwd="/w")
        live.codex_thread_id = "uuid-7"
        missing = compute_missing(_record(rec), [live])
        assert missing == []

    def test_capture_boundary_uuid_record_matches_uncaptured_live_agent(self):
        """A record's stable UUID has no live match by identity, but a live
        codex agent of the SAME tool+cwd exists uncaptured (codex_thread_id
        None) -- the capture-boundary case (Fix 3): must NOT be reported
        missing, since the live agent's id simply hasn't been captured (or
        was cleared) at this poll."""
        rec = _rec(tool="codex", session_id="uuid-7", stable_id="uuid-7", cwd="/wt/c")
        live = _codex_agent(cwd="/wt/c", codex_thread_id=None)
        assert compute_missing(_record(rec), [live]) == []

    def test_two_uuid_records_one_uncaptured_live_agent_one_still_missing(self):
        """Two distinct-UUID records in one cwd but only one (uncaptured)
        live agent there: the capture-boundary fallback must consume at
        most one record -- the other is still genuinely missing. Preserves
        existing multiset semantics (#125) even with the new fallback."""
        rec1 = _rec(tool="codex", session_id="uuid-1", stable_id="uuid-1", cwd="/wt/c", pane="%1")
        rec2 = _rec(tool="codex", session_id="uuid-2", stable_id="uuid-2", cwd="/wt/c", pane="%2")
        live = _codex_agent(cwd="/wt/c", codex_thread_id=None)
        missing = compute_missing(_record(rec1, rec2), [live])
        assert len(missing) == 1


class TestResumeCommand:
    def test_claude_with_session(self):
        assert resume_command(_rec(session_id="abc", cwd="/wt/a")) == (
            "(cd /wt/a && claude --resume abc)"
        )

    def test_claude_resume_unchanged(self):
        assert resume_command(_rec(tool="claude", session_id="s1", cwd="/w")) == (
            "(cd /w && claude --resume s1)"
        )

    def test_codex_with_id_now_resolves(self):
        # Task 17: resume_command is adapter-driven, so a codex record with a
        # captured session/stable id is resumable — this used to return None
        # unconditionally (tool != "claude"). Updating this expectation *is*
        # the point of this task.
        assert resume_command(_rec(tool="codex")) == "(cd /wt/a && agent-hq codex -- resume s1)"

    def test_codex_resume_command_rendered(self):
        rec = _rec(tool="codex", session_id="u7", cwd="/w t")  # space → quoting
        assert resume_command(rec) == "(cd '/w t' && agent-hq codex -- resume u7)"

    def test_codex_without_id_still_none(self):
        assert resume_command(_rec(tool="codex", session_id=None)) is None

    def test_claude_without_session_has_no_resume(self):
        assert resume_command(_rec(session_id=None)) is None

    def test_quotes_cwd_with_spaces(self):
        cmd = resume_command(_rec(session_id="abc", cwd="/wt/a b"))
        assert "claude --resume abc" in cmd
        assert "'/wt/a b'" in cmd


class TestParseSince:
    def test_relative_minutes(self):
        assert parse_since("45m", now=10_000.0) == 10_000.0 - 45 * 60

    def test_relative_hours(self):
        assert parse_since("2h", now=10_000.0) == 10_000.0 - 2 * 3600

    def test_relative_days(self):
        assert parse_since("1d", now=100_000.0) == 100_000.0 - 86_400

    def test_iso_datetime(self):
        expected = datetime.fromisoformat("2026-06-07 14:30:00").timestamp()
        assert parse_since("2026-06-07 14:30:00", now=0.0) == expected

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            parse_since("soonish", now=0.0)


class TestSelectBaseline:
    def _roster(self, n, timestamp):
        return _record(
            *[_rec(session_id=f"s{i}", pane=f"%{i}") for i in range(n)], timestamp=timestamp
        )

    def test_no_records(self):
        bl = select_baseline([], now=100.0)
        assert bl.record is None

    def test_no_cliff_uses_latest(self):
        records = [self._roster(3, 100.0), self._roster(3, 200.0)]
        bl = select_baseline(records, now=300.0)
        assert bl.record["timestamp"] == 200.0
        assert bl.newer_records == 0

    def test_trailing_empty_uses_last_nonempty(self):
        records = [self._roster(3, 100.0), _record(timestamp=200.0)]
        bl = select_baseline(records, now=300.0)
        assert bl.record["timestamp"] == 100.0
        assert bl.newer_records == 1

    def test_cliff_uses_record_before_collapse(self):
        # 7 agents -> 1 -> 7: the post-restore record must NOT be the baseline. #135
        records = [self._roster(7, 100.0), self._roster(1, 200.0), self._roster(7, 300.0)]
        bl = select_baseline(records, now=400.0)
        assert bl.record["timestamp"] == 100.0
        assert bl.newer_records == 2
        assert "7→1" in bl.reason

    def test_small_drop_is_not_a_cliff(self):
        # 3 -> 2 is a normal agent exit, not a crash.
        records = [self._roster(3, 100.0), self._roster(2, 200.0)]
        bl = select_baseline(records, now=300.0)
        assert bl.record["timestamp"] == 200.0

    def test_two_to_one_is_not_a_cliff(self):
        # Drops by half but only one agent gone: indistinguishable from an exit.
        records = [self._roster(2, 100.0), self._roster(1, 200.0)]
        bl = select_baseline(records, now=300.0)
        assert bl.record["timestamp"] == 200.0

    def test_old_cliff_outside_lookback_ignored(self):
        records = [self._roster(7, 100.0), self._roster(1, 200.0)]
        bl = select_baseline(records, now=200.0 + 49 * 3600)
        assert bl.record["timestamp"] == 200.0

    def test_staggered_collapse_walks_back_to_pre_crash_peak(self):
        # A crash recorded across two ticks (7 -> 4 -> 1 within seconds) must
        # baseline at the 7-roster, not the mid-collapse 4-roster. #135
        records = [
            self._roster(7, 100.0),
            self._roster(4, 105.0),
            self._roster(1, 110.0),
            self._roster(7, 200.0),
        ]
        bl = select_baseline(records, now=300.0)
        assert bl.record["timestamp"] == 100.0

    def test_gradual_decline_not_merged_into_cliff(self):
        # A drop recorded hours before the collapse reflects real exits (the
        # store writes a record within a tick of the change, so a record's
        # timestamp IS its drop time); only the collapse-adjacent drop is the
        # crash. #135, re-pinned in #274 with the drops genuinely apart.
        records = [
            self._roster(7, 100.0),
            self._roster(4, 40_000.0),
            self._roster(1, 50_005.0),
        ]
        bl = select_baseline(records, now=50_100.0)
        assert bl.record["timestamp"] == 40_000.0
        assert "4→1" in bl.reason

    def test_cliff_detected_when_self_session_inflates_post_crash_count(self):
        # 5 workers -> crash -> 2 restored + the agent-hq session. The raw
        # 5->3 drop fails the >=half gate; cliff counts must ignore self
        # agents or the control session masks the collapse. #135
        workers = [_rec(session_id=f"w{i}", pane=f"%{i}") for i in range(5)]
        hq = _rec(session_id="hq", cwd="/repo/agent-hq", branch="main", issue=None, pane="%99")
        records = [
            _record(*workers, timestamp=100.0),
            _record(hq, *workers[:2], timestamp=200.0),
        ]
        bl = select_baseline(records, now=300.0, self_ids={"hq"})
        assert bl.record["timestamp"] == 100.0

    def test_self_only_trailing_record_not_used_as_fallback_baseline(self):
        # A trailing roster holding only the agent-hq session must not become
        # the baseline and hide the dead worker (no cliff: worker drop is 1).
        hq = _rec(session_id="hq", cwd="/repo/agent-hq", branch="main", issue=None, pane="%99")
        records = [
            _record(_rec(session_id="w1", cwd="/wt/a"), hq, timestamp=100.0),
            _record(hq, timestamp=200.0),
        ]
        bl = select_baseline(records, now=300.0, self_ids={"hq"})
        assert bl.record["timestamp"] == 100.0

    def test_state_only_records_within_collapse_do_not_block_walkback(self):
        # The store appends on ANY field change, so equal-count records
        # (state/phase-only changes) appear mid-collapse: 7 -> 4 -> 4 -> 1
        # must still baseline at the 7-roster. #135
        records = [
            self._roster(7, 90.0),
            self._roster(4, 100.0),
            self._roster(4, 102.0),
            self._roster(1, 110.0),
        ]
        bl = select_baseline(records, now=200.0)
        assert bl.record["timestamp"] == 90.0

    def test_staircase_collapse_with_no_qualifying_single_step_is_a_cliff(self):
        # The 2026-09-13 reboot: 14 agents died over ~2 min at the daemon's
        # 2 s tick, so no single step drops >= 2 AND >= half (13->10 and 7->4
        # drop 3 but not half). The collapse must be judged over the whole
        # non-increasing run, baselining at the 14-roster. #274
        counts = [14, 13, 13, 10, 9, 9, 8, 7, 7, 4, 3, 3, 3, 2, 1]
        records = [self._roster(n, 100.0 + 2.0 * i) for i, n in enumerate(counts)]
        bl = select_baseline(records, now=300.0)
        assert bl.record["timestamp"] == 100.0
        assert bl.newer_records == len(counts) - 1
        assert "14→1" in bl.reason

    def test_staircase_collapse_followed_by_partial_restore(self):
        # After the staircase the daemon records the post-reboot restore; the
        # baseline is still the pre-collapse peak, not the restored roster. #274
        counts = [14, 13, 10, 7, 4, 2, 1, 5, 8]
        records = [self._roster(n, 100.0 + 2.0 * i) for i, n in enumerate(counts)]
        bl = select_baseline(records, now=300.0)
        assert bl.record["timestamp"] == 100.0
        assert "14→1" in bl.reason

    def test_windowed_drop_outside_merge_window_is_not_a_cliff(self):
        # 4 -> 3 -> 2 spread over an hour is real exits, not a crash: the
        # windowed collapse only spans records within CLIFF_MERGE_WINDOW_SECONDS.
        records = [self._roster(4, 100.0), self._roster(3, 2000.0), self._roster(2, 4000.0)]
        bl = select_baseline(records, now=4100.0)
        assert bl.record["timestamp"] == 4000.0

    def test_fallback_reason_surfaces_largest_windowed_drop(self):
        # When the heuristic falls through, the report must say what it saw
        # so a missed collapse is visible instead of reading as "nothing to
        # recover". #274
        records = [self._roster(3, 100.0), self._roster(2, 102.0), self._roster(2, 200.0)]
        bl = select_baseline(records, now=300.0)
        assert bl.record["timestamp"] == 200.0
        assert bl.reason.startswith("latest snapshot")
        assert "3→2" in bl.reason

    def test_fallback_reason_plain_when_roster_never_shrank(self):
        records = [self._roster(2, 100.0), self._roster(3, 200.0)]
        bl = select_baseline(records, now=300.0)
        assert bl.reason == "latest snapshot"

    def test_idle_roster_then_collapse_across_a_long_gap_is_a_cliff(self):
        # The store dedupes an idle roster, so the common crash shape is ONE
        # old record followed by the collapsed one, far more than the merge
        # window apart. The step into the collapsed record is never
        # window-bounded (Codex review of #274 — main selected this).
        records = [self._roster(7, 100.0), self._roster(1, 100.0 + 3 * 3600)]
        bl = select_baseline(records, now=100.0 + 3 * 3600 + 60)
        assert bl.record["timestamp"] == 100.0
        assert "7→1" in bl.reason

    def test_walkback_merges_a_drop_exactly_at_the_window_edge(self):
        # The window bounds the DROP records: 7 -> 4 -> 1 with the 4-roster's
        # drop exactly CLIFF_MERGE_WINDOW_SECONDS before the bottom merges
        # (and pulls in the 7-peak before it, whatever its age); one second
        # further does not.
        bottom = 10_000.0
        edge = [
            self._roster(7, 50.0),
            self._roster(4, bottom - CLIFF_MERGE_WINDOW_SECONDS),
            self._roster(1, bottom),
        ]
        bl = select_baseline(edge, now=bottom + 60)
        assert bl.record["timestamp"] == 50.0
        assert "7→1" in bl.reason
        beyond = [
            self._roster(7, 50.0),
            self._roster(4, bottom - CLIFF_MERGE_WINDOW_SECONDS - 1),
            self._roster(1, bottom),
        ]
        bl = select_baseline(beyond, now=bottom + 60)
        assert bl.record["timestamp"] == bottom - CLIFF_MERGE_WINDOW_SECONDS - 1
        assert "4→1" in bl.reason

    def test_idle_peak_before_a_staircase_is_the_baseline(self):
        # The realistic #274 shape: the 14-roster sat idle (deduped) for hours,
        # then died as a staircase over ~30 s. The record before the earliest
        # merged drop is the pre-crash peak however old it is — otherwise the
        # first agent to die is missing from the resume list (code-review
        # round 2 of #274).
        idle_for = 5 * 3600.0
        counts = [13, 13, 10, 9, 8, 7, 4, 3, 2, 1]
        records = [self._roster(14, 100.0)] + [
            self._roster(n, 100.0 + idle_for + 2.0 * k) for k, n in enumerate(counts)
        ]
        bl = select_baseline(records, now=100.0 + idle_for + 600.0)
        assert bl.record["timestamp"] == 100.0
        assert "14→1" in bl.reason

    def test_idle_peak_then_large_first_step_keeps_whole_roster(self):
        # 14 (idle, hours) -> 7 -> 3 -> 1 over 4 s must not baseline at 7.
        records = [
            self._roster(14, 100.0),
            self._roster(7, 20_000.0),
            self._roster(3, 20_002.0),
            self._roster(1, 20_004.0),
        ]
        bl = select_baseline(records, now=20_100.0)
        assert bl.record["timestamp"] == 100.0
        assert "14→1" in bl.reason

    def test_equal_count_record_inside_lookback_does_not_revive_expired_collapse(self):
        # 8 -> 2 happened just outside the 48 h lookback; a state-only 2-roster
        # record written inside it must not make that old collapse "the
        # crash" — only a record that is itself a drop is a candidate.
        cutoff = 100_000.0
        records = [
            self._roster(8, cutoff - 200.0),
            self._roster(2, cutoff - 100.0),
            self._roster(2, cutoff + 10.0),
        ]
        bl = select_baseline(records, now=cutoff + CLIFF_LOOKBACK_SECONDS)
        assert bl.record["timestamp"] == cutoff + 10.0
        assert bl.reason.startswith("latest snapshot")

    def test_missing_or_malformed_timestamp_in_run_does_not_raise(self):
        # Hand-edited / truncated store lines: a peak record with no usable
        # timestamp is walked over as age-unknown, never a TypeError.
        for bad in (None, "garbage"):
            peak = {**self._roster(7, 100.0), "timestamp": bad}
            records = [peak, self._roster(1, 200.0)]
            bl = select_baseline(records, now=300.0)
            assert bl.record is peak
            assert "7→1" in bl.reason

    def test_malformed_trailing_timestamp_does_not_end_the_scan(self):
        # An age-unknown record is skipped as a candidate, not treated as
        # ancient: the valid collapse just before it must still be found.
        # (Codex round 2 of #274.)
        for bad in (None, "garbage", "Infinity", "NaN"):
            records = [
                self._roster(7, 100.0),
                self._roster(1, 110.0),
                {**self._roster(1, 120.0), "timestamp": bad},
            ]
            bl = select_baseline(records, now=300.0)
            assert bl.record["timestamp"] == 100.0, bad
            assert "7→1" in bl.reason

    def test_infinite_bottom_timestamp_cannot_pass_the_lookback(self):
        # float("inf") compares newer than any cutoff; a malformed bottom must
        # not turn an ancient collapse into "the crash".
        records = [
            self._roster(7, 100.0),
            {**self._roster(1, 200.0), "timestamp": "Infinity"},
            self._roster(1, 200.0 + 49 * 3600),
        ]
        bl = select_baseline(records, now=200.0 + 49 * 3600 + 60)
        assert bl.record["timestamp"] == 200.0 + 49 * 3600
        assert bl.reason.startswith("latest snapshot")

    def test_malformed_timestamp_inside_walkback_stops_the_merge(self):
        # The window can't be judged against an age-unknown record, so the
        # walk-back stops there: 7 -> 4(unknown age) -> 1 baselines at 4, and
        # the unconditional first step still makes 4 -> 1 a cliff.
        mid = {**self._roster(4, 105.0), "timestamp": None}
        records = [self._roster(7, 100.0), mid, self._roster(1, 110.0)]
        bl = select_baseline(records, now=300.0)
        assert bl.record is mid
        assert "4→1" in bl.reason

    def test_restore_then_subthreshold_drop_keeps_earlier_collapse(self):
        # 8 -> 2 -> 4 -> 3: the later 4 -> 3 exit stops walking at the restore
        # and is no cliff; the scan continues and finds the 8 -> 2 collapse.
        records = [
            self._roster(8, 100.0),
            self._roster(2, 102.0),
            self._roster(4, 150.0),
            self._roster(3, 160.0),
        ]
        bl = select_baseline(records, now=300.0)
        assert bl.record["timestamp"] == 100.0
        assert "8→2" in bl.reason

    def test_fallback_reason_reports_the_largest_of_competing_drops(self):
        # Two sub-threshold declines: 5 -> 4 (drop 1) and later 3 -> 2 -> 2... the
        # reason names the biggest windowed drop, here 6 -> 5 -> 4 (drop 2, not half).
        records = [
            self._roster(6, 100.0),
            self._roster(5, 102.0),
            self._roster(4, 104.0),
            self._roster(4, 1000.0),
            self._roster(3, 1002.0),
        ]
        bl = select_baseline(records, now=1100.0)
        assert bl.record["timestamp"] == 1002.0
        assert bl.reason == "latest snapshot (no roster collapse; largest drop seen 6→4)"
        assert bl.newer_records == 0

    def test_equal_count_churn_baselines_at_newest_peak_record(self):
        # [A,B,C] -> [A,B,D] (C exited, D started, same tick) -> crash to [].
        # Equal-count records join the run, but the baseline is the NEWEST
        # record of the peak count: C was already gone before the crash and D
        # is the one to resume (code-review round 3 of #274).
        abc = _record(*[_rec(session_id=x, pane=f"%{x}") for x in "ABC"], timestamp=100.0)
        abd = _record(*[_rec(session_id=x, pane=f"%{x}") for x in "ABD"], timestamp=160.0)
        records = [abc, abd, _record(timestamp=220.0)]
        bl = select_baseline(records, now=300.0)
        assert bl.record is abd
        assert "3→0" in bl.reason
        assert bl.newer_records == 1

    def test_since_tolerates_malformed_timestamp(self):
        # The --since path compares timestamps too; a malformed line is
        # skipped as age-unknown there as well, never a TypeError.
        bad = {**self._roster(3, 100.0), "timestamp": "2026-09-13"}
        records = [self._roster(3, 50.0), bad, self._roster(2, 200.0)]
        bl = select_baseline(records, now=300.0, since_ts=150.0)
        assert bl.record["timestamp"] == 50.0

    def test_baseline_back_override(self):
        records = [self._roster(3, 100.0), self._roster(2, 200.0), self._roster(2, 300.0)]
        bl = select_baseline(records, now=400.0, baseline_back=2)
        assert bl.record["timestamp"] == 100.0
        assert bl.newer_records == 2

    def test_baseline_back_out_of_range_raises(self):
        with pytest.raises(ValueError):
            select_baseline([self._roster(1, 100.0)], now=200.0, baseline_back=5)

    def test_since_picks_newest_record_at_or_before(self):
        records = [self._roster(7, 100.0), self._roster(7, 200.0), self._roster(7, 300.0)]
        bl = select_baseline(records, now=400.0, since_ts=250.0)
        assert bl.record["timestamp"] == 200.0
        assert bl.newer_records == 1

    def test_since_older_than_all_records_uses_oldest(self):
        records = [self._roster(7, 100.0), self._roster(7, 200.0)]
        bl = select_baseline(records, now=400.0, since_ts=50.0)
        assert bl.record["timestamp"] == 100.0


class TestExcludeSelf:
    def test_live_agent_in_invoking_pane_excluded(self):
        live = [_live("worker", pane="%1"), _live("me", pane="%2", cwd="/wt/b")]
        record, kept, n = exclude_self(None, live, invoking_pane="%2")
        assert [a.claude_transcript_session_id for a in kept] == ["worker"]
        assert n == 1

    def test_invoking_agent_identity_also_dropped_from_baseline(self):
        # The invoking session resumed a pre-crash session id: its baseline
        # record must not surface as missing once the live side is excluded.
        rec = _record(_rec(session_id="me"), _rec(session_id="worker", pane="%6"))
        live = [_live("me", pane="%2")]
        record, kept, n = exclude_self(rec, live, invoking_pane="%2")
        assert kept == []
        assert {r["session_id"] for r in record["agents"]} == {"worker"}

    def test_other_sessions_in_self_checkout_are_not_excluded(self):
        # #410: only the invoking session is "self". Another Claude session in
        # agent-hq's own checkout is real work and stays on both sides.
        rec = _record(
            _rec(session_id="pm", cwd="/repo/agent-hq", branch="main", issue=None, pane="%101"),
            _rec(session_id="worker", cwd="/wt/a", pane="%6"),
        )
        live = [
            _live("me", cwd="/repo/agent-hq", pane="%3"),
            _live("other-hq", cwd="/repo/agent-hq", pane="%4"),
        ]
        record, kept, n = exclude_self(rec, live, invoking_pane="%3")
        assert [a.claude_transcript_session_id for a in kept] == ["other-hq"]
        assert {r["session_id"] for r in record["agents"]} == {"pm", "worker"}
        assert n == 1

    def test_sessionless_sibling_not_over_excluded(self):
        # Two sessionless codex agents share a tool:cwd identity. The invoking
        # session (pane %2) consumes only its own pane's record — the dead
        # sibling is still a recovery target.
        rec = _record(
            _rec(session_id=None, tool="codex", cwd="/wt/c", pane="%1"),
            _rec(session_id=None, tool="codex", cwd="/wt/c", pane="%2"),
        )
        live = [_live(None, tool=AgentType.CODEX, cwd="/wt/c", pane="%2")]
        record, kept, _ = exclude_self(rec, live, invoking_pane="%2")
        assert kept == []
        assert [r["pane"] for r in record["agents"]] == ["%1"]

    def test_same_identity_on_both_sides_counted_once(self):
        # The invoking agent present in both the baseline and the live set is
        # ONE excluded agent, not two — the count is unique identities.
        rec = _record(_rec(session_id="me", cwd="/repo/agent-hq"))
        live = [_live("me", cwd="/repo/agent-hq", pane="%2")]
        _, _, n = exclude_self(rec, live, invoking_pane="%2")
        assert n == 1

    def test_nothing_excluded_without_pane(self):
        # Outside tmux there is no invoking pane: nothing is "self".
        rec = _record(_rec(session_id="a", cwd="/repo/agent-hq"))
        live = [_live("a", cwd="/repo/agent-hq")]
        record, kept, n = exclude_self(rec, live, invoking_pane=None)
        assert len(kept) == 1
        assert len(record["agents"]) == 1
        assert n == 0


class TestSplitPossiblySelf:
    """Missing agents in agent-hq's own checkout are flagged, never hidden. #410"""

    def test_missing_agent_in_self_checkout_is_possibly_self(self):
        rec = _record(
            _rec(session_id="pm", cwd="/repo/agent-hq", branch="main", issue=None),
            _rec(session_id="worker", cwd="/wt/a", pane="%6"),
        )
        missing, possibly = split_possibly_self(compute_missing(rec, []), "/repo/agent-hq")
        assert [m.record["session_id"] for m in missing] == ["worker"]
        assert [m.record["session_id"] for m in possibly] == ["pm"]
        assert possibly[0].resume_command == "(cd /repo/agent-hq && claude --resume pm)"

    def test_subdirectory_of_self_root_is_possibly_self(self):
        rec = _record(_rec(session_id="pm", cwd="/repo/agent-hq/agent_hq"))
        missing, possibly = split_possibly_self(compute_missing(rec, []), "/repo/agent-hq")
        assert missing == []
        assert len(possibly) == 1

    def test_sibling_worktree_is_a_plain_missing_agent(self):
        # Dispatched workers live in <repo>-worktrees/ next to the main
        # checkout — they are NOT agent-hq's own checkout.
        rec = _record(_rec(session_id="w", cwd="/repo/agent-hq-worktrees/agent-hq.feature-1-x"))
        missing, possibly = split_possibly_self(compute_missing(rec, []), "/repo/agent-hq")
        assert len(missing) == 1
        assert possibly == []

    def test_no_self_root_flags_nothing(self):
        rec = _record(_rec(session_id="pm", cwd="/repo/agent-hq"))
        missing, possibly = split_possibly_self(compute_missing(rec, []), None)
        assert len(missing) == 1
        assert possibly == []

    def test_report_lists_possibly_self_with_resume_command(self):
        rec = _record(_rec(session_id="pm", cwd="/repo/agent-hq", branch="main", issue=None))
        missing, possibly = split_possibly_self(compute_missing(rec, []), "/repo/agent-hq")
        out = format_report(_baseline(rec), missing, now=160.0, possibly_self=possibly)
        assert "agent-hq's own checkout" in out
        assert "verify" in out
        assert "(cd /repo/agent-hq && claude --resume pm)" in out
        assert "Nothing to recover" not in out

    def test_json_lists_possibly_self_separately(self):
        rec = _record(_rec(session_id="pm", cwd="/repo/agent-hq"), timestamp=100.0)
        missing, possibly = split_possibly_self(compute_missing(rec, []), "/repo/agent-hq")
        data = json.loads(format_json(_baseline(rec), missing, possibly_self=possibly))
        assert data["missing"] == []
        assert [p["session_id"] for p in data["possibly_self"]] == ["pm"]
        assert data["possibly_self"][0]["resume_command"] == (
            "(cd /repo/agent-hq && claude --resume pm)"
        )


def _write_transcript(cwd: str, session_id: str, mtime: float) -> None:
    import agent_hq.transcript as transcript

    encoded = cwd.replace("/", "-").replace(".", "-").replace("_", "-")
    directory = transcript.CLAUDE_PROJECTS_DIR / encoded
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text('{"type": "user"}\n')
    os.utime(path, (mtime, mtime))


class TestResolveFromTranscripts:
    """A Claude record with no session id gets one from its transcript. #410"""

    CWD = "/repo/agent-hq"

    def _missing(self, *recs):
        return compute_missing(_record(*recs), [])

    def test_resolves_newest_transcript_written_before_cutoff(self):
        _write_transcript(self.CWD, "old-1", mtime=1_000.0)
        _write_transcript(self.CWD, "the-one", mtime=9_000.0)
        _write_transcript(self.CWD, "post-crash", mtime=12_000.0)
        missing = self._missing(_rec(session_id=None, cwd=self.CWD, pane="%101"))
        [m] = resolve_from_transcripts(missing, claimed=set(), cutoff=10_000.0)
        assert m.resolved_session_id == "the-one"
        assert m.resume_command == "(cd /repo/agent-hq && claude --resume the-one)"
        assert m.candidates == []

    def test_claimed_transcripts_are_skipped(self):
        # A transcript belonging to a live agent or another baseline record is
        # that session's, never this one's.
        _write_transcript(self.CWD, "older", mtime=8_000.0)
        _write_transcript(self.CWD, "live-session", mtime=9_500.0)
        missing = self._missing(_rec(session_id=None, cwd=self.CWD))
        [m] = resolve_from_transcripts(missing, claimed={"live-session"}, cutoff=10_000.0)
        assert m.resolved_session_id == "older"

    def test_close_candidates_are_ambiguous(self):
        _write_transcript(self.CWD, "a", mtime=9_000.0)
        _write_transcript(self.CWD, "b", mtime=9_100.0)
        _write_transcript(self.CWD, "ancient", mtime=100.0)
        missing = self._missing(_rec(session_id=None, cwd=self.CWD))
        [m] = resolve_from_transcripts(missing, claimed=set(), cutoff=10_000.0)
        assert m.resolved_session_id is None
        assert m.resume_command is None
        assert [c.session_id for c in m.candidates] == ["b", "a"]
        assert m.candidates[0].resume_command == "(cd /repo/agent-hq && claude --resume b)"

    def test_two_sessionless_records_in_one_cwd_list_candidates_not_guess(self):
        # Which pane wrote which transcript is unknowable: both records get
        # the candidates instead of an arbitrary in-order match.
        _write_transcript(self.CWD, "ancient", mtime=100.0)
        _write_transcript(self.CWD, "first", mtime=2_000.0)
        _write_transcript(self.CWD, "second", mtime=9_000.0)
        missing = self._missing(
            _rec(session_id=None, cwd=self.CWD, pane="%1"),
            _rec(session_id=None, cwd=self.CWD, pane="%2"),
        )
        resolved = resolve_from_transcripts(missing, claimed=set(), cutoff=10_000.0)
        assert [m.resolved_session_id for m in resolved] == [None, None]
        for m in resolved:
            assert [c.session_id for c in m.candidates] == ["second", "first"]

    def test_sessionless_records_in_different_cwds_resolve_independently(self):
        _write_transcript("/wt/one", "x", mtime=9_000.0)
        _write_transcript("/wt/two", "y", mtime=9_000.0)
        missing = self._missing(
            _rec(session_id=None, cwd="/wt/one", pane="%1"),
            _rec(session_id=None, cwd="/wt/two", pane="%2"),
        )
        resolved = resolve_from_transcripts(missing, claimed=set(), cutoff=10_000.0)
        assert [m.resolved_session_id for m in resolved] == ["x", "y"]

    def test_no_transcript_dir_leaves_record_unresolved(self):
        missing = self._missing(_rec(session_id=None, cwd="/nowhere"))
        [m] = resolve_from_transcripts(missing, claimed=set(), cutoff=10_000.0)
        assert m.resolved_session_id is None
        assert m.resume_command is None
        assert m.candidates == []

    def test_records_with_an_id_and_non_claude_records_untouched(self):
        _write_transcript(self.CWD, "t", mtime=9_000.0)
        missing = self._missing(
            _rec(session_id="known", cwd=self.CWD),
            _rec(session_id=None, tool="codex", cwd=self.CWD),
        )
        resolved = resolve_from_transcripts(missing, claimed=set(), cutoff=10_000.0)
        assert [m.resolved_session_id for m in resolved] == [None, None]
        assert resolved[0].resume_command == "(cd /repo/agent-hq && claude --resume known)"
        assert resolved[1].resume_command is None

    def test_report_marks_resolved_and_lists_ambiguous_candidates(self):
        _write_transcript("/wt/one", "picked", mtime=9_000.0)
        _write_transcript("/wt/two", "a", mtime=9_000.0)
        _write_transcript("/wt/two", "b", mtime=9_050.0)
        rec = _record(
            _rec(session_id=None, cwd="/wt/one", pane="%1"),
            _rec(session_id=None, cwd="/wt/two", pane="%2"),
        )
        missing = resolve_from_transcripts(compute_missing(rec, []), claimed=set(), cutoff=10_000.0)
        out = format_report(_baseline(rec), missing, now=10_000.0)
        assert "(cd /wt/one && claude --resume picked)  (resolved from transcript" in out
        assert "2 candidate transcript(s)" in out
        assert "(cd /wt/two && claude --resume b)" in out
        assert "(cd /wt/two && claude --resume a)" in out

    def test_json_carries_resolution(self):
        _write_transcript(self.CWD, "picked", mtime=9_000.0)
        rec = _record(_rec(session_id=None, cwd=self.CWD), timestamp=100.0)
        missing = resolve_from_transcripts(compute_missing(rec, []), claimed=set(), cutoff=10_000.0)
        data = json.loads(format_json(_baseline(rec), missing))
        entry = data["missing"][0]
        assert entry["session_id"] is None
        assert entry["resolved_session_id"] == "picked"
        assert entry["resume"] == {"cwd": self.CWD, "argv": ["claude", "--resume", "picked"]}
        assert entry["transcript_candidates"] == []


class TestTranscriptResolutionReviewRound1:
    """Codex review of #410: two ways a confident pick could be wrong."""

    HQ = "/repo/agent-hq"

    def test_unidentified_live_sibling_makes_match_ambiguous(self):
        # One of two sessionless agents in a cwd survived: compute_missing
        # consumes a live slot, so only one record is missing, but the
        # survivor's transcript is unclaimed (its id is unknown). It must not
        # be offered as the dead agent's confident match.
        _write_transcript(self.HQ, "survivor", mtime=9_500.0)
        _write_transcript(self.HQ, "dead", mtime=2_000.0)
        missing = compute_missing(_record(_rec(session_id=None, cwd=self.HQ)), [])
        [m] = resolve_from_transcripts(
            missing, claimed=set(), cutoff=10_000.0, unidentified_live={self.HQ: 1}
        )
        assert m.resolved_session_id is None
        assert [c.session_id for c in m.candidates] == ["survivor", "dead"]

    def test_run_recover_counts_sessionless_live_agents(self, tmp_path):
        path = tmp_path / "snaps.jsonl"
        path.write_text(
            json.dumps(
                {
                    "timestamp": 1_000.0,
                    "agents": [
                        _rec(session_id=None, cwd="/wt/x", pane="%1"),
                        _rec(session_id=None, cwd="/wt/x", pane="%2"),
                    ],
                }
            )
            + "\n"
        )
        _write_transcript("/wt/x", "survivor", mtime=900.0)
        _write_transcript("/wt/x", "dead", mtime=100.0)
        out = io.StringIO()
        rc = run_recover(
            store=SnapshotStore(path),
            live_agents=[_live(None, cwd="/wt/x", pane="%2")],
            out=out,
            now=1_100.0,
            as_json=True,
            self_root=None,
            invoking_pane=None,
        )
        assert rc == 0
        [entry] = json.loads(out.getvalue())["missing"]
        assert entry["resolved_session_id"] is None
        assert [c["session_id"] for c in entry["transcript_candidates"]] == ["survivor", "dead"]

    def test_explicit_baseline_does_not_bound_by_the_next_record(self, tmp_path):
        # --baseline picks a record followed by an ordinary state update, not
        # a crash: that update's time must not cut off the agent's transcript.
        pm = _rec(session_id=None, cwd="/wt/x", pane="%1", state="running")
        path = tmp_path / "snaps.jsonl"
        path.write_text(
            json.dumps({"timestamp": 1_000.0, "agents": [pm]})
            + "\n"
            + json.dumps({"timestamp": 1_060.0, "agents": [{**pm, "state": "idle"}]})
            + "\n"
        )
        _write_transcript("/wt/x", "unrelated-old", mtime=500.0)
        _write_transcript("/wt/x", "the-one", mtime=5_000.0)
        out = io.StringIO()
        rc = run_recover(
            store=SnapshotStore(path),
            live_agents=[],
            out=out,
            now=6_000.0,
            as_json=True,
            baseline_back=1,
            self_root=None,
            invoking_pane=None,
        )
        assert rc == 0
        [entry] = json.loads(out.getvalue())["missing"]
        assert entry["resolved_session_id"] == "the-one"

    def test_detected_collapse_records_when_agents_began_dying(self):
        records = [
            _record(*[_rec(session_id=f"w{i}", pane=f"%{i}") for i in range(4)], timestamp=100.0),
            _record(_rec(session_id="w0", pane="%0"), timestamp=200.0),
        ]
        bl = select_baseline(records, now=300.0)
        assert bl.record["timestamp"] == 100.0
        assert bl.collapse_ts == 200.0

    def test_override_and_fallback_baselines_have_no_collapse_time(self):
        records = [
            _record(_rec(session_id="a"), timestamp=100.0),
            _record(_rec(session_id="a", state="idle"), timestamp=200.0),
        ]
        assert select_baseline(records, now=300.0, baseline_back=1).collapse_ts is None
        assert select_baseline(records, now=300.0, since_ts=150.0).collapse_ts is None
        assert select_baseline(records, now=300.0).collapse_ts is None


class TestReviewRound1CodeReview:
    """code-review of #410 (round 1)."""

    HQ = "/repo/agent-hq"

    def test_main_checkout_sessions_count_toward_a_collapse(self):
        # Three main-checkout sessions and one worker die; the worker is
        # restored. Only the invoking session is discounted, so the 4→1 drop
        # is a collapse and the dead main-checkout sessions reach the report.
        hq = [
            _rec(session_id=f"hq{i}", cwd=self.HQ, branch="main", issue=None, pane=f"%{i}")
            for i in range(3)
        ]
        worker = _rec(session_id="w", cwd="/wt/a", pane="%9")
        records = [
            _record(*hq, worker, timestamp=100.0),
            _record(timestamp=200.0),
            _record(worker, timestamp=250.0),
        ]
        bl = select_baseline(records, now=300.0, self_ids={"me"})
        assert bl.record["timestamp"] == 100.0

    def test_transcript_older_than_the_agent_is_a_candidate_not_a_match(self):
        # The agent was first seen at 5000; the only unclaimed transcript was
        # last written at 1000, before it existed: an unrelated session.
        _write_transcript(self.HQ, "unrelated", mtime=1_000.0)
        pm = _rec(session_id=None, cwd=self.HQ, pane="%101")
        records = [_record(pm, timestamp=5_000.0), _record(pm, timestamp=6_000.0)]
        missing = compute_missing(records[-1], [])
        [m] = resolve_from_transcripts(
            missing, claimed=set(), cutoff=10_000.0, records=records, baseline_index=1
        )
        assert m.resolved_session_id is None
        assert [c.session_id for c in m.candidates] == ["unrelated"]

    def test_transcript_written_while_the_agent_was_seen_resolves(self):
        _write_transcript(self.HQ, "real", mtime=5_500.0)
        pm = _rec(session_id=None, cwd=self.HQ, pane="%101")
        records = [_record(pm, timestamp=5_000.0), _record(pm, timestamp=6_000.0)]
        [m] = resolve_from_transcripts(
            compute_missing(records[-1], []),
            claimed=set(),
            cutoff=10_000.0,
            records=records,
            baseline_index=1,
        )
        assert m.resolved_session_id == "real"

    def test_ids_from_other_slots_in_any_record_are_claimed(self):
        # Session X ran in the same cwd in another pane and exited before the
        # crash: its transcript is X's, never the sessionless agent's.
        _write_transcript(self.HQ, "x-session", mtime=5_800.0)
        _write_transcript(self.HQ, "real", mtime=5_500.0)
        pm = _rec(session_id=None, cwd=self.HQ, pane="%101")
        x = _rec(session_id="x-session", cwd=self.HQ, pane="%7")
        records = [_record(pm, x, timestamp=5_000.0), _record(pm, timestamp=6_000.0)]
        [m] = resolve_from_transcripts(
            compute_missing(records[-1], []),
            claimed=set(),
            cutoff=10_000.0,
            records=records,
            baseline_index=1,
        )
        assert m.resolved_session_id == "real"

    def test_same_slot_earlier_id_stays_eligible(self):
        # The same pane recorded this session's id earlier, then lost it (the
        # suspected #410 cause): that transcript is the right answer.
        _write_transcript(self.HQ, "same-pane", mtime=5_900.0)
        earlier = _rec(session_id="same-pane", cwd=self.HQ, pane="%101")
        pm = _rec(session_id=None, cwd=self.HQ, pane="%101")
        records = [_record(earlier, timestamp=5_000.0), _record(pm, timestamp=6_000.0)]
        [m] = resolve_from_transcripts(
            compute_missing(records[-1], []),
            claimed=set(),
            cutoff=10_000.0,
            records=records,
            baseline_index=1,
        )
        assert m.resolved_session_id == "same-pane"

    def test_candidate_list_covers_every_sibling(self):
        for i in range(7):
            _write_transcript(self.HQ, f"t{i}", mtime=1_000.0 * (i + 1))
        recs = [_rec(session_id=None, cwd=self.HQ, pane=f"%{i}") for i in range(7)]
        resolved = resolve_from_transcripts(
            compute_missing(_record(*recs), []), claimed=set(), cutoff=10_000.0
        )
        assert all(len(m.candidates) == 7 for m in resolved)

    def test_sessionless_invoking_agent_does_not_hide_a_dead_sibling(self):
        # The invoking pane has no captured id, so its identity is the
        # tool:cwd fallback. It may only consume its OWN pane's record.
        rec = _record(_rec(session_id=None, cwd=self.HQ, pane="%101"))
        live = [_live(None, cwd=self.HQ, pane="%7")]
        record, kept, _ = exclude_self(rec, live, invoking_pane="%7")
        assert kept == []
        assert [r["pane"] for r in record["agents"]] == ["%101"]

    def test_sessionless_invoking_agent_consumes_its_own_pane_record(self):
        rec = _record(_rec(session_id=None, cwd=self.HQ, pane="%7"))
        live = [_live(None, cwd=self.HQ, pane="%7")]
        record, _, n = exclude_self(rec, live, invoking_pane="%7")
        assert record["agents"] == []
        assert n == 1


class TestReviewRound2:
    """Codex review of #410 (round 2)."""

    def test_sessionless_invoker_does_not_discount_sessionless_siblings(self, tmp_path):
        # The invoker's fallback identity (claude:/wt/p) is shared by every
        # sessionless Claude agent in /wt/p. Baseline counting may discount
        # only the invoker's own pane, or the dead siblings vanish.
        path = tmp_path / "snaps.jsonl"
        path.write_text(
            json.dumps(
                {
                    "timestamp": 100.0,
                    "agents": [
                        _rec(session_id=None, cwd="/wt/p", pane="%1"),
                        _rec(session_id=None, cwd="/wt/p", pane="%2"),
                    ],
                }
            )
            + "\n"
        )
        out = io.StringIO()
        rc = run_recover(
            store=SnapshotStore(path),
            live_agents=[_live(None, cwd="/wt/p", pane="%7")],
            out=out,
            now=200.0,
            as_json=True,
            self_root=None,
            invoking_pane="%7",
        )
        assert rc == 0
        data = json.loads(out.getvalue())
        assert sorted(m["pane"] for m in data["missing"]) == ["%1", "%2"]

    def test_sessionless_invoker_own_pane_record_is_discounted(self):
        me = _rec(session_id=None, cwd="/wt/p", pane="%7")
        workers = [_rec(session_id=f"w{i}", pane=f"%{i}") for i in range(4)]
        records = [
            _record(*workers, timestamp=100.0),
            _record(me, workers[0], timestamp=200.0),
        ]
        # 4 workers → 1 worker + the invoker: a collapse only if the invoker's
        # own pane record is discounted.
        bl = select_baseline(records, now=300.0, self_ids={"claude:/wt/p"}, self_pane="%7")
        assert bl.record["timestamp"] == 100.0


class TestReviewRound3:
    """Codex review of #410 (round 3)."""

    def test_colliding_cwds_share_one_sibling_count(self):
        # /wt/foo-bar and /wt/foo_bar encode to the same transcript dir: one
        # transcript there must not be "resolved" for both agents.
        _write_transcript("/wt/foo-bar", "only", mtime=9_000.0)
        missing = compute_missing(
            _record(
                _rec(session_id=None, cwd="/wt/foo-bar", pane="%1"),
                _rec(session_id=None, cwd="/wt/foo_bar", pane="%2"),
            ),
            [],
        )
        resolved = resolve_from_transcripts(missing, claimed=set(), cutoff=10_000.0)
        assert [m.resolved_session_id for m in resolved] == [None, None]
        assert all([c.session_id for c in m.candidates] == ["only"] for m in resolved)

    def test_unidentified_live_agent_in_colliding_cwd_counts(self):
        _write_transcript("/wt/foo-bar", "only", mtime=9_000.0)
        missing = compute_missing(_record(_rec(session_id=None, cwd="/wt/foo-bar")), [])
        [m] = resolve_from_transcripts(
            missing, claimed=set(), cutoff=10_000.0, unidentified_live={"/wt/foo_bar": 1}
        )
        assert m.resolved_session_id is None


class TestRegression20261029:
    """Replays the 2026-10-29 reboot that hid a real session. #410

    The agent-hq main checkout held two Claude sessions before the reboot:
    the one the owner later ran ``recover`` from, and a long-lived working session
    recorded with no session id. Only the invoking one is "self"; the other
    must come back with a resume command resolved from its transcript.
    """

    HQ_ROOT = "/Users/me/Programming/agent-hq"

    def test_real_session_in_self_checkout_is_listed_with_resolved_resume(self, tmp_path):
        pm = _rec(session_id=None, cwd=self.HQ_ROOT, branch="main", issue=None, pane="%101")
        me_old = _rec(session_id="me", cwd=self.HQ_ROOT, branch="main", issue=None, pane="%7")
        workers = [_rec(session_id=f"w{i}", cwd=f"/wt/w{i}", pane=f"%{i}") for i in range(4)]
        path = tmp_path / "snaps.jsonl"
        path.write_text(
            json.dumps({"timestamp": 1_000.0, "agents": [pm, me_old, *workers]})
            + "\n"
            + json.dumps({"timestamp": 2_000.0, "agents": [me_old]})
            + "\n"
        )
        _write_transcript(self.HQ_ROOT, "older-session", mtime=500.0)
        _write_transcript(self.HQ_ROOT, "d1ee6873", mtime=1_900.0)
        _write_transcript(self.HQ_ROOT, "me", mtime=2_500.0)
        live = [_live("me", cwd=self.HQ_ROOT, pane="%7")]
        out = io.StringIO()
        rc = run_recover(
            store=SnapshotStore(path),
            live_agents=live,
            out=out,
            now=3_000.0,
            as_json=True,
            self_root=self.HQ_ROOT,
            invoking_pane="%7",
        )
        assert rc == 0
        data = json.loads(out.getvalue())
        assert data["self_excluded"] == 1
        assert sorted(m["session_id"] for m in data["missing"]) == ["w0", "w1", "w2", "w3"]
        [pm_entry] = data["possibly_self"]
        assert pm_entry["pane"] == "%101"
        assert pm_entry["resolved_session_id"] == "d1ee6873"
        assert pm_entry["resume_command"] == f"(cd {self.HQ_ROOT} && claude --resume d1ee6873)"


class TestFormatReport:
    def test_no_snapshot(self):
        out = format_report(_baseline(None), [], now=0.0)
        assert "No snapshots" in out

    def test_all_live(self):
        rec = _record(_rec(session_id="s1"))
        out = format_report(_baseline(rec), [], now=160.0)
        assert "still live" in out
        assert "Nothing to recover" in out

    def test_empty_snapshot(self):
        out = format_report(_baseline(_record()), [], now=160.0)
        assert "no live agents" in out.lower()

    def test_lists_missing_with_resume_and_context(self):
        rec = _record(_rec(session_id="abc", cwd="/wt/a"))
        missing = compute_missing(rec, [])
        out = format_report(_baseline(rec), missing, now=160.0)
        assert "no longer live" in out
        assert "agent-hq/feature/125-x" in out
        assert "#125" in out
        assert "running/implement" in out
        assert "(cd /wt/a && claude --resume abc)" in out
        assert "1m ago" in out

    def test_non_claude_missing_shows_no_resume_note(self):
        rec = _record(_rec(session_id=None, tool="codex", cwd="/wt/c", branch="main", issue=None))
        missing = compute_missing(rec, [])
        out = format_report(_baseline(rec), missing, now=100.0)
        assert "no resume id" in out
        assert "codex" in out
        assert "/wt/c" in out

    def test_surfaces_baseline_reason_and_skipped_records(self):
        # A wrong heuristic pick must be visible, not silent. #135
        rec = _record(_rec(session_id="s1"), timestamp=100.0)
        out = format_report(
            _baseline(rec, reason="roster collapse 7→1 detected", newer=2), [], now=160.0
        )
        assert "roster collapse 7→1" in out
        assert "2 newer record(s)" in out

    def test_mentions_self_exclusions(self):
        rec = _record(_rec(session_id="s1"))
        out = format_report(_baseline(rec), [], now=160.0, self_excluded=1)
        assert "excluded" in out


class TestFormatJson:
    def test_shape(self):
        rec = _record(_rec(session_id="abc", cwd="/wt/a"), timestamp=100.0)
        missing = compute_missing(rec, [])
        data = json.loads(format_json(_baseline(rec, reason="latest snapshot"), missing))
        assert data["snapshot_timestamp"] == 100.0
        assert data["recorded"] == 1
        assert data["baseline_reason"] == "latest snapshot"
        assert len(data["missing"]) == 1
        assert data["missing"][0]["session_id"] == "abc"
        assert data["missing"][0]["resume_command"] == "(cd /wt/a && claude --resume abc)"
        assert data["missing"][0]["resume"] == {
            "cwd": "/wt/a",
            "argv": ["claude", "--resume", "abc"],
        }

    def test_carries_structured_resume_for_codex(self):
        rec = _record(_rec(tool="codex", session_id="u7", cwd="/w"), timestamp=100.0)
        missing = compute_missing(rec, [])
        data = json.loads(format_json(_baseline(rec), missing))
        assert data["missing"][0]["resume"] == {
            "cwd": "/w", "argv": ["agent-hq", "codex", "--", "resume", "u7"]
        }
        assert data["missing"][0]["resume_command"] == "(cd /w && agent-hq codex -- resume u7)"

    def test_resume_is_null_when_no_spec(self):
        rec = _record(_rec(session_id=None, tool="codex", cwd="/wt/c"), timestamp=100.0)
        missing = compute_missing(rec, [])
        data = json.loads(format_json(_baseline(rec), missing))
        assert data["missing"][0]["resume"] is None
        assert data["missing"][0]["resume_command"] is None

    def test_tolerates_record_without_timestamp(self):
        # A corrupt/hand-edited record (no timestamp) must not crash --json,
        # matching format_report's graceful handling.
        data = json.loads(format_json(_baseline({"agents": []}), []))
        assert data["snapshot_timestamp"] is None
        assert data["recorded"] == 0


class TestRunRecover:
    def test_prints_report_and_returns_zero(self, tmp_path):
        store = SnapshotStore(tmp_path / "snaps.jsonl")
        # Seed a snapshot with one agent, then run with an empty live set.
        store.append_if_changed([_live("abc")], timestamp=100.0)
        out = io.StringIO()
        rc = run_recover(store=store, live_agents=[], out=out, now=160.0)
        assert rc == 0
        assert "no longer live" in out.getvalue()

    def test_json_mode(self, tmp_path):
        store = SnapshotStore(tmp_path / "snaps.jsonl")
        store.append_if_changed([_live("abc")], timestamp=100.0)
        out = io.StringIO()
        rc = run_recover(store=store, live_agents=[], out=out, now=160.0, as_json=True)
        assert rc == 0
        data = json.loads(out.getvalue())
        assert data["recorded"] == 1
        assert len(data["missing"]) == 1

    def test_trailing_empty_record_does_not_defeat_recovery(self, tmp_path):
        # The reboot bug: a cold-start/transient empty record must NOT make
        # recover report "nothing to recover" — it uses the last NON-empty
        # snapshot as the baseline. #125
        path = tmp_path / "snaps.jsonl"
        path.write_text(
            json.dumps({"timestamp": 100.0, "agents": [_rec(session_id="abc", cwd="/wt/a")]})
            + "\n"
            + json.dumps({"timestamp": 200.0, "agents": []})
            + "\n"
        )
        out = io.StringIO()
        rc = run_recover(store=SnapshotStore(path), live_agents=[], out=out, now=260.0)
        assert rc == 0
        assert "no longer live" in out.getvalue()
        assert "claude --resume abc" in out.getvalue()

    def test_invalid_since_returns_error(self, tmp_path):
        store = SnapshotStore(tmp_path / "snaps.jsonl")
        out = io.StringIO()
        rc = run_recover(store=store, live_agents=[], out=out, now=100.0, since="soonish")
        assert rc == 2
        assert "since" in out.getvalue().lower()

    def test_baseline_back_flag_selects_older_record(self, tmp_path):
        path = tmp_path / "snaps.jsonl"
        path.write_text(
            json.dumps({"timestamp": 100.0, "agents": [_rec(session_id="old", cwd="/wt/a")]})
            + "\n"
            + json.dumps({"timestamp": 200.0, "agents": [_rec(session_id="new", cwd="/wt/b")]})
            + "\n"
        )
        out = io.StringIO()
        rc = run_recover(
            store=SnapshotStore(path), live_agents=[], out=out, now=300.0, baseline_back=1
        )
        assert rc == 0
        assert "claude --resume old" in out.getvalue()
        assert "claude --resume new" not in out.getvalue()


class TestRegression20260607:
    """Replays the 2026-06-07 crash that fooled recover. #135

    Snapshot history: 7 workers → collapse to 1 (the agent-hq session) →
    6 restored + the agent-hq session. Live set matches the last snapshot
    exactly, and the roster *count* is back to 7 — but worker w6 is dead.
    """

    HQ_ROOT = "/Users/me/Programming/agent-hq"

    def _seed(self, tmp_path):
        workers = [
            _rec(session_id=f"w{i}", cwd=f"/wt/w{i}", pane=f"%{i}", branch=f"feature/{i}-x")
            for i in range(7)
        ]
        hq = _rec(
            session_id="hq-session-xyz",
            cwd=self.HQ_ROOT,
            branch="main",
            issue=None,
            pane="%99",
        )
        path = tmp_path / "snaps.jsonl"
        path.write_text(
            json.dumps({"timestamp": 100.0, "agents": workers})
            + "\n"
            + json.dumps({"timestamp": 200.0, "agents": [hq]})
            + "\n"
            + json.dumps({"timestamp": 300.0, "agents": [hq] + workers[:6]})
            + "\n"
        )
        live = [_live("hq-session-xyz", cwd=self.HQ_ROOT, pane="%99")] + [
            _live(f"w{i}", cwd=f"/wt/w{i}", pane=f"%{i}") for i in range(6)
        ]
        return SnapshotStore(path), live

    def test_names_exactly_the_one_dead_worker(self, tmp_path):
        store, live = self._seed(tmp_path)
        out = io.StringIO()
        rc = run_recover(
            store=store,
            live_agents=live,
            out=out,
            now=360.0,
            as_json=True,
            self_root=self.HQ_ROOT,
            invoking_pane=None,
        )
        assert rc == 0
        data = json.loads(out.getvalue())
        assert [m["session_id"] for m in data["missing"]] == ["w6"]
        assert data["missing"][0]["resume_command"] == "(cd /wt/w6 && claude --resume w6)"

    def test_human_report_surfaces_baseline_and_omits_hq_session(self, tmp_path):
        store, live = self._seed(tmp_path)
        out = io.StringIO()
        rc = run_recover(
            store=store,
            live_agents=live,
            out=out,
            now=360.0,
            self_root=self.HQ_ROOT,
            invoking_pane=None,
        )
        assert rc == 0
        text = out.getvalue()
        assert "claude --resume w6" in text
        assert "hq-session-xyz" not in text
        assert "roster collapse" in text

    def test_dead_hq_session_flagged_not_listed_as_recoverable(self, tmp_path):
        # Pre-crash roster includes the agent-hq session; post-crash it was
        # replaced by a fresh session id. It must not be offered for resume.
        hq_old = _rec(session_id="hq-old", cwd=self.HQ_ROOT, branch="main", issue=None, pane="%9")
        worker = _rec(session_id="w0", cwd="/wt/w0", pane="%1")
        path = tmp_path / "snaps.jsonl"
        path.write_text(json.dumps({"timestamp": 100.0, "agents": [hq_old, worker]}) + "\n")
        out = io.StringIO()
        rc = run_recover(
            store=SnapshotStore(path),
            live_agents=[_live("hq-new", cwd=self.HQ_ROOT, pane="%2")],
            out=out,
            now=160.0,
            as_json=True,
            self_root=self.HQ_ROOT,
            invoking_pane=None,
        )
        assert rc == 0
        data = json.loads(out.getvalue())
        assert [m["session_id"] for m in data["missing"]] == ["w0"]
        # #410: still visible, but flagged for a human to verify, never mixed
        # in with the recovery targets.
        assert [m["session_id"] for m in data["possibly_self"]] == ["hq-old"]
