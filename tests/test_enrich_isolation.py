"""One agent's enrichment failure never aborts the daemon tick (#358).

Since 2026-10-19 the daemon tick kept dying on
``TypeError('unexpected "<class '_pygit2.Commit'>"')`` from pygit2's
``Repository.diff``: ``git_diff_shortstat`` didn't catch it and
``enrich_all_agents`` had no per-agent isolation, so one bad repo froze
cache.json for every agent. These pin the containment and the diagnostics
(a stable-literal warn plus a local traceback) that let the inner cause be
found later.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agent_hq import enrichment
from agent_hq.models import Agent, AgentState, AgentType


def _agent(pane_id: str, *, branch: str = "main", pid: int | None = 100) -> Agent:
    return Agent(
        pane_id=pane_id,
        session_name="s",
        window_index=0,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd=f"/work/{pane_id[1:]}",
        repo_root=f"/work/{pane_id[1:]}",
        repo_name="repo",
        branch=branch,
        state=AgentState.IDLE,
        agent_pid=pid,
    )


def _raw(pane_id: str, pid: int | None = 100) -> dict:
    """A discovery row: no ``cwd`` (enrich_agent looks it up from tmux)."""
    return {
        "pane_id": pane_id,
        "session_name": "s",
        "window_index": 3,
        "pane_index": 1,
        "command": "claude",
        "agent_pid": pid,
    }


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch):
    enrichment._failure_reported.clear()
    monkeypatch.setattr(enrichment, "get_pane_cwd", lambda pane_id: f"/work/{pane_id[1:]}")
    yield
    enrichment._failure_reported.clear()


def _enrich_failing_on(pane_id: str, exc: Exception):
    def fake(raw):
        if raw["pane_id"] == pane_id:
            raise exc
        return _agent(raw["pane_id"])

    return fake


class TestEnrichAllAgentsIsolation:
    def test_failing_agent_keeps_previous_git_context_others_enrich(self):
        prior = _agent("%2", branch="feature/kept")
        prior.claude_transcript_session_id = "sess-2"
        with patch.object(
            enrichment, "enrich_agent", side_effect=_enrich_failing_on("%2", TypeError("boom"))
        ):
            agents = enrichment.enrich_all_agents([_raw("%1"), _raw("%2")], previous={"%2": prior})

        by_pane = {a.pane_id: a for a in agents}
        assert set(by_pane) == {"%1", "%2"}
        fallback = by_pane["%2"]
        assert fallback.branch == "feature/kept"
        assert fallback.repo_root == "/work/2"
        # Same identity, so the web card is updated, not removed and re-added.
        assert fallback.stable_key == prior.stable_key
        # Live pane fields come from discovery, not the old record.
        assert fallback.window_index == 3
        assert fallback.state is AgentState.UNKNOWN

    @pytest.mark.parametrize(
        "prior_pid, raw_pid", [(999, 100), (None, None)], ids=["other-process", "pid-unknown"]
    )
    def test_failing_agent_without_matching_prior_shows_without_git_context(
        self, prior_pid, raw_pid
    ):
        """A different (or unknown) process in the same pane can't lend its
        git context; the agent still shows, just without it."""
        previous = {"%2": _agent("%2", branch="feature/other", pid=prior_pid)}
        with patch.object(
            enrichment, "enrich_agent", side_effect=_enrich_failing_on("%2", RuntimeError("x"))
        ):
            agents = enrichment.enrich_all_agents(
                [_raw("%1"), _raw("%2", pid=raw_pid)], previous=previous
            )

        by_pane = {a.pane_id: a for a in agents}
        assert set(by_pane) == {"%1", "%2"}
        assert by_pane["%2"].branch is None
        assert by_pane["%2"].cwd == "/work/2"

    def test_malformed_row_costs_only_itself(self):
        """Review round 2: a row the fallback can't build from (no pane_id)
        must not raise after the loop either."""
        def fake(raw):
            if "pane_id" not in raw:
                raise KeyError("pane_id")
            return _agent(raw["pane_id"])

        with patch.object(enrichment, "enrich_agent", side_effect=fake), patch.object(
            enrichment.eventlog, "emit"
        ):
            agents = enrichment.enrich_all_agents([_raw("%1"), {"command": "claude"}])
        assert [a.pane_id for a in agents] == ["%1"]

    def test_failure_emits_stable_warn_with_context_and_writes_traceback(
        self, tmp_path, monkeypatch
    ):
        log_file = tmp_path / "enrich-errors.log"
        monkeypatch.setattr(enrichment, "ENRICH_ERRORS_FILE", log_file)
        previous = {"%2": _agent("%2", branch="feature/kept")}
        with patch.object(
            enrichment, "enrich_agent", side_effect=_enrich_failing_on("%2", TypeError("boom"))
        ), patch.object(enrichment.eventlog, "emit") as emit:
            enrichment.enrich_all_agents([_raw("%1"), _raw("%2")], previous=previous)

        emit.assert_called_once()
        level, message, extra = emit.call_args.args[:3]
        assert (level, message) == ("warn", "agent enrichment failed")
        assert extra["error_type"] == "TypeError"
        assert extra["repo"] == "/work/2"
        assert extra["branch"] == "feature/kept"
        assert "boom" not in json.dumps(extra)  # exception text stays local
        # Throttled here per repo, so eventlog's per-message dedupe is off.
        assert emit.call_args.kwargs["min_interval_s"] == 0
        text = log_file.read_text()
        assert "agent enrichment failed" in text
        assert "TypeError: boom" in text
        assert "Traceback" in text

    def test_no_prior_reports_the_pane_cwd_as_repo(self, tmp_path, monkeypatch):
        """Discovery rows carry no ``cwd``: the report looks it up."""
        monkeypatch.setattr(enrichment, "ENRICH_ERRORS_FILE", tmp_path / "e.log")
        with patch.object(
            enrichment, "enrich_agent", side_effect=_enrich_failing_on("%2", TypeError("boom"))
        ), patch.object(enrichment.eventlog, "emit") as emit:
            enrichment.enrich_all_agents([_raw("%2")])
        assert emit.call_args.args[2]["repo"] == "/work/2"


class TestGitDiffShortstatTypeError:
    def _repo_whose_diff_hides_a_peel_error(self):
        """pygit2's ``Repository.diff`` raises its TypeError from inside an
        ``except`` around ``peel(Tree)``, so the real error is ``__context__``."""
        repo = MagicMock()

        def diff(a, b):
            try:
                raise KeyError("tree 1234 not found")
            except Exception:
                raise TypeError('unexpected "<class \'_pygit2.Commit\'>"')

        repo.diff.side_effect = diff
        return repo

    def test_type_error_returns_none_drops_handle_and_reports(self, tmp_path, monkeypatch):
        log_file = tmp_path / "enrich-errors.log"
        monkeypatch.setattr(enrichment, "ENRICH_ERRORS_FILE", log_file)
        repo = self._repo_whose_diff_hides_a_peel_error()
        with patch.object(enrichment, "_open_repo", return_value=repo), patch.object(
            enrichment, "_drop_cached_repo"
        ) as drop, patch.object(enrichment.eventlog, "emit") as emit:
            assert enrichment.git_diff_shortstat("/repos/x", "feature/1") is None

        drop.assert_called_once_with("/repos/x")
        level, message, extra = emit.call_args.args[:3]
        assert (level, message) == ("warn", "git diff stats failed")
        assert extra["error_type"] == "TypeError"
        assert extra["cause_type"] == "KeyError"
        assert extra["repo"] == "/repos/x"
        assert extra["branch"] == "feature/1"
        text = log_file.read_text()
        assert "tree 1234 not found" in text  # the hidden inner error is kept locally

    def test_repeated_failures_are_throttled(self, tmp_path, monkeypatch):
        log_file = tmp_path / "enrich-errors.log"
        monkeypatch.setattr(enrichment, "ENRICH_ERRORS_FILE", log_file)
        repo = self._repo_whose_diff_hides_a_peel_error()
        with patch.object(enrichment, "_open_repo", return_value=repo), patch.object(
            enrichment, "_drop_cached_repo"
        ), patch.object(enrichment.eventlog, "emit") as emit:
            for _ in range(5):
                enrichment.git_diff_shortstat("/repos/x", "feature/1")

        assert emit.call_count == 1
        assert log_file.read_text().count("git diff stats failed") == 1

    def test_each_repo_and_each_new_cause_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(enrichment, "ENRICH_ERRORS_FILE", tmp_path / "e.log")
        with patch.object(enrichment.eventlog, "emit") as emit:
            for repo in ("/r/a", "/r/b"):
                try:
                    raise TypeError("x")
                except TypeError as exc:
                    enrichment._report_enrichment_failure("git diff stats failed", exc,
                                                          repo=repo, branch="main")
            try:
                try:
                    raise OSError("inner")
                except OSError:
                    raise TypeError("x")
            except TypeError as exc:
                enrichment._report_enrichment_failure("git diff stats failed", exc,
                                                      repo="/r/a", branch="main")
        assert emit.call_count == 3

    def test_full_log_rotates_to_dot_one(self, tmp_path, monkeypatch):
        log_file = tmp_path / "enrich-errors.log"
        monkeypatch.setattr(enrichment, "ENRICH_ERRORS_FILE", log_file)
        monkeypatch.setattr(enrichment, "_ENRICH_ERRORS_MAX_BYTES", 10)
        log_file.write_text("old window " * 5)
        with patch.object(enrichment.eventlog, "emit"):
            try:
                raise TypeError("x")
            except TypeError as exc:
                enrichment._report_enrichment_failure("git diff stats failed", exc,
                                                      repo="/r", branch="b")
        assert (tmp_path / "enrich-errors.log.1").read_text().startswith("old window")
        assert "git diff stats failed" in log_file.read_text()
