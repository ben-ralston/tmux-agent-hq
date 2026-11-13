"""Picking the authoritative replica by reset time (#255).

MIN(remaining) alone is not enough. Measured 2026-08-26 at 07:56, 7 of 10
`rate_limit` reads were stale; at that rate five samples miss the truthful
replica ~17% of the time, and a live Task-13 run hit exactly that — the module
reported 5000 remaining while every call was rejected.

The scale-free discriminator is the reset timestamp. A replica that has not
seen the current window mints a fresh one *at request time*, so its reset is
always LATER than the real window's. Preferring the earliest reset picks the
authoritative read regardless of how many replicas lie.
"""

import json
import subprocess
import time

import pytest

from agent_hq import gh_budget

# Future-relative so the fixtures stay in a live window as time passes; the
# offsets preserve the original ordering between the truthful and stale
# replicas (#255 review).
_BASE = time.time() + 3600.0


@pytest.fixture(autouse=True)
def _reset_state():
    gh_budget.clear_cache()
    gh_budget.set_floor(gh_budget.DEFAULT_GRAPHQL_FLOOR)
    yield
    gh_budget.clear_cache()
    gh_budget.set_floor(gh_budget.DEFAULT_GRAPHQL_FLOOR)


def _read(gql_remaining, gql_reset, core_remaining=4999, core_reset=None):
    return {
        "resources": {
            "graphql": {
                "limit": 5000,
                "remaining": gql_remaining,
                "used": 5000 - gql_remaining,
                "reset": gql_reset,
            },
            "core": {
                "limit": 5000,
                "remaining": core_remaining,
                "used": 5000 - core_remaining,
                "reset": core_reset if core_reset is not None else gql_reset - 108,
            },
        }
    }


def _serve(sequence):
    calls = []

    def _run(argv, **kwargs):
        if argv[:3] == ["gh", "api", "graphql"]:
            # These tests pin the REST sampling fallback: the GraphQL-side
            # read fails here without consuming a body from the sequence.
            raise subprocess.CalledProcessError(1, argv, output="", stderr="gh: down")
        calls.append(argv)
        body = sequence[min(len(calls) - 1, len(sequence) - 1)]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

    _run.calls = calls
    return _run


# The real observed values, 2026-08-26.
_TRUTH = _read(0, _BASE, core_remaining=4999, core_reset=(_BASE - 108))
_STALE = _read(5000, (_BASE + 1130), core_remaining=5000, core_reset=(_BASE + 1130))


def test_one_truthful_read_among_many_stale_wins(monkeypatch):
    """The exact live failure: 4 stale, 1 truthful, truthful must win."""
    monkeypatch.setattr(subprocess, "run", _serve([_STALE, _STALE, _STALE, _STALE, _TRUTH]))
    assert gh_budget.snapshot().graphql_remaining == 0


def test_truthful_read_wins_from_any_position(monkeypatch):
    for position in range(5):
        gh_budget.clear_cache()
        seq = [_STALE] * 5
        seq[position] = _TRUTH
        monkeypatch.setattr(subprocess, "run", _serve(seq))
        assert gh_budget.snapshot().graphql_remaining == 0, f"missed at position {position}"


def test_earliest_reset_wins_even_when_it_reports_more_remaining(monkeypatch):
    """Reset time, not remaining, identifies the authoritative window.

    A stale replica could in principle report a *lower* remaining for a
    window that isn't ours; the real window is the one that started first.
    """
    real = _read(1200, _BASE)
    stale_low = _read(50, (_BASE + 1130), core_remaining=5000, core_reset=(_BASE + 1130))
    monkeypatch.setattr(subprocess, "run", _serve([stale_low, real, stale_low]))
    assert gh_budget.snapshot().graphql_remaining == 1200


def test_all_stale_still_yields_a_usable_answer(monkeypatch):
    """With nothing but stale reads we report what we saw, not a crash."""
    monkeypatch.setattr(subprocess, "run", _serve([_STALE]))
    snap = gh_budget.snapshot()
    assert snap is None or snap.graphql_remaining == 5000


def test_a_consistent_fresh_window_is_read_normally(monkeypatch):
    """All replicas agreeing on a real window must not trip the heuristic."""
    fresh = _read(4800, (_BASE + 10457), core_remaining=4700, core_reset=(_BASE + 9457))
    monkeypatch.setattr(subprocess, "run", _serve([fresh]))
    snap = gh_budget.snapshot()
    assert snap is not None
    assert snap.graphql_remaining == 4800


def test_reads_without_a_reset_do_not_break_selection(monkeypatch):
    no_reset = {
        "resources": {
            "graphql": {"limit": 5000, "remaining": 4000, "used": 1000},
            "core": {"limit": 5000, "remaining": 4999, "used": 1},
        }
    }
    monkeypatch.setattr(subprocess, "run", _serve([no_reset]))
    snap = gh_budget.snapshot()
    assert snap is not None
    assert snap.graphql_remaining == 4000
