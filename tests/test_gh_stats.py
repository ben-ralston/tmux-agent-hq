"""Tests for the gh-stats CLI (#255)."""

import json
import subprocess

import pytest

from agent_hq import gh_budget, gh_meter, gh_stats


@pytest.fixture(autouse=True)
def _no_live_budget(monkeypatch):
    """gh-stats renders headroom; never let the suite probe the real API."""
    gh_budget.clear_cache()
    monkeypatch.setattr(gh_budget, "snapshot", lambda: None)
    yield
    gh_budget.clear_cache()


def test_reports_counts_by_api_and_kind(capsys):
    for _ in range(3):
        gh_meter.record("project-item-list", "graphql")
    gh_meter.record("issue-index", "rest")
    assert gh_stats.run_gh_stats(hours=1) == 0
    out = capsys.readouterr().out
    assert "graphql" in out
    assert "project-item-list" in out
    assert "issue-index" in out


def test_json_output_is_machine_readable(capsys):
    gh_meter.record("issue-index", "rest")
    assert gh_stats.run_gh_stats(hours=1, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["by_api"]["rest"] == 1
    assert payload["hours"] == 1


def test_estimates_graphql_points_from_measured_costs(capsys):
    """Calls alone mislead: cost per call spans two orders of magnitude."""
    for _ in range(2):
        gh_meter.record("project-item-list", "graphql")
    assert gh_stats.run_gh_stats(hours=1, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["estimated_graphql_points"] == pytest.approx(406.0)


def test_the_board_query_is_scored_far_below_item_list(capsys):
    """The #255 saving has to show up in the report, not just in the code.

    Same call count, ~100x the points — if `project-board-query` were missing
    from the table it would silently score the 1.0 default and understate what
    the old call cost.
    """
    for _ in range(2):
        gh_meter.record("project-board-query", "graphql")
    gh_stats.run_gh_stats(hours=1, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["estimated_graphql_points"] == pytest.approx(4.0)


def test_rest_calls_cost_no_graphql_points(capsys):
    for _ in range(10):
        gh_meter.record("issue-index", "rest")
    gh_stats.run_gh_stats(hours=1, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["estimated_graphql_points"] == 0


def test_unknown_kind_falls_back_to_one_point(capsys):
    """An uncosted GraphQL call is worth at least a point — never zero."""
    gh_meter.record("some-new-call", "graphql")
    gh_stats.run_gh_stats(hours=1, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["estimated_graphql_points"] == 1


def test_empty_counter_reports_zero(capsys):
    assert gh_stats.run_gh_stats(hours=1) == 0
    out = capsys.readouterr().out
    assert "0" in out


def test_headroom_is_rendered_when_known(monkeypatch, capsys):
    monkeypatch.setattr(
        gh_budget,
        "snapshot",
        lambda: gh_budget.RateBudget(
            graphql_remaining=4150,
            graphql_limit=5000,
            graphql_reset=None,
            core_remaining=4999,
            core_limit=5000,
            core_reset=None,
            exhausted_reset=None,
        ),
    )
    gh_stats.run_gh_stats(hours=1)
    out = capsys.readouterr().out
    assert "4150" in out and "5000" in out


def test_cli_wires_the_subcommand(monkeypatch):
    from agent_hq import cli

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    parser = cli.create_parser()
    args = parser.parse_args(["gh-stats", "--hours", "3"])
    assert args.subcommand == "gh-stats"
    assert args.hours == 3


def test_a_busy_uncosted_rest_kind_cannot_hide_graphql_points(capsys):
    """The old estimator reconstructed the kind/bucket pairing from two
    independent histograms with a descending-count loop and a hand-kept
    "REST-only" set; a new REST kind not in that set consumed the GraphQL
    call count first and the board query never got scored (#255 review).
    The meter records the bucket on every row, so the sum is direct."""
    for _ in range(100):
        gh_meter.record("brand-new-rest-kind", "rest")
    for _ in range(6):
        gh_meter.record("project-board-query", "graphql")
    gh_stats.run_gh_stats(hours=1, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["estimated_graphql_points"] == pytest.approx(12.0)


def test_summary_exposes_graphql_calls_by_kind():
    gh_meter.record("issue-index", "rest")
    gh_meter.record("project-board-query", "graphql")
    summary = gh_meter.summarize(hours=1)
    assert summary.graphql_by_kind == {"project-board-query": 1}
