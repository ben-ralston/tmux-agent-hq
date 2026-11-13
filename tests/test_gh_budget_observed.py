"""The observed-failure signal, which outranks the rate_limit meter (#255).

`gh api rate_limit` cannot be trusted to report exhaustion: measured
2026-08-26, 19 of 30 reads reported a full budget while GraphQL was provably
dead. A gh call that actually failed with a rate-limit error is ground truth,
and that signal must win.
"""

import subprocess
import time

import pytest

from agent_hq import gh_budget

# A live window resets in the future; a hardcoded epoch silently ages into
# the past, where a below-floor reading correctly stops gating (#255 review).
_RESET = time.time() + 3600.0


@pytest.fixture(autouse=True)
def _reset_budget_state():
    gh_budget.clear_cache()
    gh_budget.set_floor(gh_budget.DEFAULT_GRAPHQL_FLOOR)
    yield
    gh_budget.clear_cache()
    gh_budget.set_floor(gh_budget.DEFAULT_GRAPHQL_FLOOR)


def _full_budget(*a, **k):
    """A rate_limit probe that (wrongly) reports a pristine budget."""
    import json

    body = {
        "resources": {
            "graphql": {"limit": 5000, "remaining": 5000, "used": 0, "reset": _RESET},
            "core": {"limit": 5000, "remaining": 5000, "used": 0, "reset": _RESET},
        }
    }
    return subprocess.CompletedProcess(a[0], 0, stdout=json.dumps(body), stderr="")


def test_observed_rate_limit_beats_a_lying_meter(monkeypatch):
    """The exact live failure: every replica lies, but a call really failed."""
    monkeypatch.setattr(subprocess, "run", _full_budget)
    assert gh_budget.allow_background_refresh() is True  # meter says fine
    gh_budget.note_rate_limited(reset_at=time.time() + 600)
    assert gh_budget.allow_background_refresh() is False


def test_observed_exhaustion_expires_at_reset(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _full_budget)
    gh_budget.note_rate_limited(reset_at=time.time() - 1)  # already past
    assert gh_budget.allow_background_refresh() is True


def test_observed_exhaustion_without_a_reset_uses_a_default_window(monkeypatch):
    """gh's stderr doesn't always carry a reset; refuse anyway, briefly."""
    monkeypatch.setattr(subprocess, "run", _full_budget)
    gh_budget.note_rate_limited(reset_at=None)
    assert gh_budget.allow_background_refresh() is False


def test_explicit_refresh_is_never_gated_by_observed_exhaustion(monkeypatch):
    """`allow_background_refresh` governs background work only — the caller
    decides that a forced refresh bypasses it entirely."""
    monkeypatch.setattr(subprocess, "run", _full_budget)
    gh_budget.note_rate_limited(reset_at=time.time() + 600)
    assert gh_budget.is_exhausted() is True
    assert gh_budget.exhausted_until() is not None


def test_clear_cache_forgets_observed_exhaustion(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _full_budget)
    gh_budget.note_rate_limited(reset_at=time.time() + 600)
    gh_budget.clear_cache()
    assert gh_budget.allow_background_refresh() is True


def test_synthetic_fresh_window_reads_are_discarded(monkeypatch):
    """A replica that hasn't seen the window mints one per bucket.

    The tell is that every bucket reports the *same* reset, ~1h out. A
    truthful read has per-bucket resets that differ, because each bucket's
    window began when its own first request landed.
    """
    import json

    now = time.time()
    truthful = {
        "resources": {
            "graphql": {"limit": 5000, "remaining": 0, "used": 5000, "reset": now + 900},
            "core": {"limit": 5000, "remaining": 4999, "used": 1, "reset": now + 880},
        }
    }
    synthetic = {
        "resources": {
            "graphql": {"limit": 5000, "remaining": 5000, "used": 0, "reset": now + 3600},
            "core": {"limit": 5000, "remaining": 5000, "used": 0, "reset": now + 3600},
        }
    }
    seq = [synthetic, synthetic, truthful, synthetic, synthetic]
    calls = []

    def _run(argv, **kwargs):
        calls.append(argv)
        body = seq[min(len(calls) - 1, len(seq) - 1)]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    snap = gh_budget.snapshot()
    assert snap.graphql_remaining == 0


def test_all_synthetic_reads_yield_unknown(monkeypatch):
    """If every read is synthetic we know nothing — and unknown allows."""
    monkeypatch.setattr(subprocess, "run", _full_budget)
    assert gh_budget.snapshot() is None
    assert gh_budget.allow_background_refresh() is True


def test_a_real_fresh_window_is_still_readable(monkeypatch):
    """Guard against the heuristic eating legitimately-fresh budgets.

    At the top of a real hour the buckets are full, but their resets still
    differ per bucket, so the read survives.
    """
    import json

    now = time.time()
    body = {
        "resources": {
            "graphql": {"limit": 5000, "remaining": 5000, "used": 0, "reset": now + 3599},
            "core": {"limit": 5000, "remaining": 4998, "used": 2, "reset": now + 3100},
        }
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr=""),
    )
    snap = gh_budget.snapshot()
    assert snap is not None
    assert snap.graphql_remaining == 5000
