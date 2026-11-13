"""A once-observed window must not be forgotten until it expires (#255).

Selection by earliest reset picks the truthful replica whenever one appears in
the sample. It cannot help when *every* read is stale — measured live at a 70%
stale rate, that happens ~17% of the time with 5 samples, and it did: one run
in five reported 5000 remaining on a dead budget.

But a real window does not disappear. Once we have seen ``graphql`` at N
remaining for a window ending at R, no later read describing a *different*
(necessarily synthesized) window should be allowed to overwrite it before R
passes.
"""

import json
import subprocess

import pytest

from agent_hq import gh_budget


@pytest.fixture(autouse=True)
def _reset_state():
    gh_budget.clear_cache()
    yield
    gh_budget.clear_cache()


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


def _serve(body):
    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

    return _run


def test_a_seen_window_survives_an_all_stale_probe(monkeypatch, time_machine=None):
    """The live failure: first probe sees truth, a later one is all-stale."""
    import time

    now = time.time()
    truth = _read(0, now + 400, core_remaining=4999, core_reset=now + 300)
    stale = _read(5000, now + 3600, core_remaining=5000, core_reset=now + 3600)

    monkeypatch.setattr(subprocess, "run", _serve(truth))
    assert gh_budget.snapshot().graphql_remaining == 0

    # New probe, every read stale — must not resurrect a full budget.
    gh_budget._expire_snapshot_memo()
    monkeypatch.setattr(subprocess, "run", _serve(stale))
    assert gh_budget.snapshot().graphql_remaining == 0
    assert gh_budget.allow_background_refresh() is False


def test_the_remembered_window_is_released_once_it_expires(monkeypatch):
    import time

    now = time.time()
    truth = _read(0, now - 1, core_remaining=4999, core_reset=now - 100)
    fresh = _read(5000, now + 3600, core_remaining=5000, core_reset=now + 3500)

    monkeypatch.setattr(subprocess, "run", _serve(truth))
    gh_budget.snapshot()

    gh_budget._expire_snapshot_memo()
    monkeypatch.setattr(subprocess, "run", _serve(fresh))
    # The remembered window has passed, so the new one is believed.
    assert gh_budget.snapshot().graphql_remaining == 5000


def test_a_lower_reading_in_the_same_window_is_accepted(monkeypatch):
    """Spending inside a window must still be tracked downwards."""
    import time

    now = time.time()
    monkeypatch.setattr(subprocess, "run", _serve(_read(4000, now + 900)))
    assert gh_budget.snapshot().graphql_remaining == 4000

    gh_budget._expire_snapshot_memo()
    monkeypatch.setattr(subprocess, "run", _serve(_read(1500, now + 900)))
    assert gh_budget.snapshot().graphql_remaining == 1500


def test_clear_cache_forgets_the_remembered_window(monkeypatch):
    import time

    now = time.time()
    monkeypatch.setattr(subprocess, "run", _serve(_read(0, now + 900)))
    gh_budget.snapshot()
    gh_budget.clear_cache()
    monkeypatch.setattr(subprocess, "run", _serve(_read(5000, now + 3600, core_remaining=4999)))
    assert gh_budget.snapshot().graphql_remaining == 5000


def test_a_higher_reading_in_the_same_window_is_not_believed(monkeypatch):
    """Within one window ``remaining`` can only fall. A replica that lags on
    its counter under-reports *usage*, so it can only over-report headroom;
    the lower of two same-window readings is always the one nearer the
    truth. Believing the higher one reopened the gate on a budget we had
    already seen below the floor (#255 review)."""
    import time

    now = time.time()
    monkeypatch.setattr(subprocess, "run", _serve(_read(100, now + 900)))
    assert gh_budget.snapshot().graphql_remaining == 100
    gh_budget._expire_snapshot_memo()
    monkeypatch.setattr(subprocess, "run", _serve(_read(4000, now + 900)))
    assert gh_budget.snapshot().graphql_remaining == 100


def test_a_window_without_a_reset_is_not_remembered(monkeypatch):
    """A remembered window expires by its reset. One that carries none can
    never expire, and a below-floor reading of that shape would stand for
    the life of the process once later probes started failing (#255 review)."""
    import time

    monkeypatch.setattr(subprocess, "run", _serve(_read(100, None, core_reset=time.time() + 900)))
    assert gh_budget.snapshot().graphql_remaining == 100
    gh_budget._expire_snapshot_memo()
    def _gone(*a, **k):
        raise FileNotFoundError("gone")

    monkeypatch.setattr(subprocess, "run", _gone)
    assert gh_budget.snapshot() is None
