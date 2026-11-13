"""Tests for GitHub rate-limit budget awareness (#255)."""

import json
import subprocess
import time

import pytest

from agent_hq import gh_budget

# A live ``rate_limit`` response always resets in the FUTURE. These were
# hardcoded epochs, which quietly aged into the past — and a past reset now
# correctly reads as "that window already rolled over", so a below-floor
# sample stops gating (#255 review). Relative values keep the fixtures honest
# as time passes; the offsets preserve the original ordering (core latest).
_GRAPHQL_RESET = time.time() + 3600.0
_LATE_RESET = _GRAPHQL_RESET + 1130.0
_CORE_RESET = _GRAPHQL_RESET + 81.0


@pytest.fixture(autouse=True)
def _reset_budget_state():
    """The snapshot memo, the floor and the warn edge-trigger are module
    globals — without this, whichever test ran first decides the rest."""
    gh_budget.clear_cache()
    gh_budget.set_floor(gh_budget.DEFAULT_GRAPHQL_FLOOR)
    yield
    gh_budget.clear_cache()
    gh_budget.set_floor(gh_budget.DEFAULT_GRAPHQL_FLOOR)


def _payload(graphql_remaining, core_remaining=4999, graphql_reset=_GRAPHQL_RESET):
    return {
        "resources": {
            "graphql": {
                "limit": 5000,
                "remaining": graphql_remaining,
                "used": 5000 - graphql_remaining,
                "reset": graphql_reset,
            },
            "core": {
                "limit": 5000,
                "remaining": core_remaining,
                "used": 5000 - core_remaining,
                "reset": _CORE_RESET,
            },
        }
    }


def _serving(*payloads):
    """A fake `gh` that returns each payload in turn, cycling the last."""
    seq = list(payloads)
    calls = []

    def _run(argv, **kwargs):
        calls.append(argv)
        body = seq[min(len(calls) - 1, len(seq) - 1)]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

    _run.calls = calls
    return _run


def test_snapshot_reads_both_buckets(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _serving(_payload(4150)))
    snap = gh_budget.snapshot()
    assert snap.graphql_remaining == 4150
    assert snap.graphql_limit == 5000
    assert snap.core_remaining == 4999


def test_snapshot_takes_the_minimum_remaining_across_replicas(monkeypatch):
    """The endpoint is served by replicas with inconsistent counters.

    Measured 2026-08-26: while GraphQL was fully exhausted, 7 of 12 reads
    reported ``used:0, remaining:5000`` — a full budget. A stale replica can
    only *over*-report remaining, so the minimum is the only safe aggregate.
    Trusting a single read would report full headroom on a dead budget.
    """
    fake = _serving(
        _payload(5000, graphql_reset=_LATE_RESET),  # lying replica
        _payload(0, graphql_reset=_GRAPHQL_RESET),  # truthful replica
        _payload(5000, graphql_reset=_LATE_RESET),
        _payload(5000, graphql_reset=_LATE_RESET),
        _payload(5000, graphql_reset=_LATE_RESET),
    )
    monkeypatch.setattr(subprocess, "run", fake)
    assert gh_budget.snapshot().graphql_remaining == 0


def _rest_calls(fake):
    """The REST ``rate_limit`` reads a fake saw (the GraphQL-side read is
    answered with a REST body by ``_serving`` fakes, so it fails and the
    sampled fallback runs — the path these tests pin)."""
    return [a for a in fake.calls if a[:3] != ["gh", "api", "graphql"]]


def test_snapshot_samples_several_times(monkeypatch):
    fake = _serving(_payload(4150))
    monkeypatch.setattr(subprocess, "run", fake)
    gh_budget.snapshot()
    assert len(_rest_calls(fake)) == gh_budget._SAMPLES


def test_a_genuinely_fresh_window_is_not_discarded(monkeypatch):
    """``used == 0`` is the legitimate state at the top of the hour.

    An earlier draft filtered those reads out as "obviously stale", which
    would make a genuinely fresh budget unreadable.
    """
    monkeypatch.setattr(subprocess, "run", _serving(_payload(5000)))
    snap = gh_budget.snapshot()
    assert snap is not None
    assert snap.graphql_remaining == 5000


def test_snapshot_is_memoized(monkeypatch):
    fake = _serving(_payload(4150))
    monkeypatch.setattr(subprocess, "run", fake)
    gh_budget.snapshot()
    gh_budget.snapshot()
    assert len(_rest_calls(fake)) == gh_budget._SAMPLES  # not doubled


def test_snapshot_returns_none_when_gh_fails(monkeypatch):
    def _boom(*a, **k):
        raise subprocess.CalledProcessError(1, a[0], stderr="nope")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert gh_budget.snapshot() is None


def test_snapshot_survives_partial_read_failures(monkeypatch):
    """Some replicas answering is enough; we just want the pessimistic view."""
    calls = []

    def _flaky(argv, **kwargs):
        calls.append(argv)
        if len(calls) % 2:
            raise subprocess.CalledProcessError(1, argv, stderr="flaky")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(_payload(300)), stderr="")

    monkeypatch.setattr(subprocess, "run", _flaky)
    snap = gh_budget.snapshot()
    assert snap is not None
    assert snap.graphql_remaining == 300


def test_background_refresh_allowed_with_headroom(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _serving(_payload(4150)))
    assert gh_budget.allow_background_refresh() is True


def test_background_refresh_blocked_below_floor(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _serving(_payload(120)))
    assert gh_budget.allow_background_refresh() is False


def test_background_refresh_blocked_when_a_replica_reveals_exhaustion(monkeypatch):
    """The case that matters: majority of reads lie, one tells the truth."""
    monkeypatch.setattr(
        subprocess,
        "run",
        _serving(_payload(5000), _payload(5000), _payload(0), _payload(5000), _payload(5000)),
    )
    assert gh_budget.allow_background_refresh() is False


def test_background_refresh_allowed_when_budget_unknown(monkeypatch):
    """An unreadable rate_limit must not lock the app out of refreshing."""

    def _boom(*a, **k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert gh_budget.allow_background_refresh() is True


def test_crossing_the_floor_emits_one_warn(monkeypatch):
    """One warn per descent, not one per blocked refresh.

    The TTL is zeroed rather than calling ``clear_cache()`` between the two
    checks: clear_cache deliberately re-arms the edge trigger, so using it
    here would test the reset rather than the edge.
    """
    emitted = []
    monkeypatch.setattr(
        "agent_hq.gh_budget.eventlog.emit",
        lambda level, msg, extra=None, **kw: emitted.append((level, msg)),
    )
    monkeypatch.setattr(gh_budget, "_SNAPSHOT_TTL", 0.0)
    monkeypatch.setattr(subprocess, "run", _serving(_payload(120)))
    assert gh_budget.allow_background_refresh() is False
    assert gh_budget.allow_background_refresh() is False
    assert [lvl for lvl, _ in emitted] == ["warn"]


def test_recovering_above_the_floor_rearms_the_warn(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        "agent_hq.gh_budget.eventlog.emit",
        lambda level, msg, extra=None, **kw: emitted.append(msg),
    )
    monkeypatch.setattr(subprocess, "run", _serving(_payload(120)))
    gh_budget.allow_background_refresh()
    gh_budget.clear_cache()
    monkeypatch.setattr(subprocess, "run", _serving(_payload(4000)))
    gh_budget.allow_background_refresh()
    gh_budget.clear_cache()
    monkeypatch.setattr(subprocess, "run", _serving(_payload(120)))
    gh_budget.allow_background_refresh()
    assert len(emitted) == 2


def test_floor_is_configurable(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _serving(_payload(800)))
    assert gh_budget.allow_background_refresh() is True
    gh_budget.clear_cache()
    gh_budget.set_floor(1000)
    assert gh_budget.allow_background_refresh() is False


def test_exhausted_reset_prefers_the_graphql_bucket(monkeypatch):
    """This value is what an observed GraphQL rejection pins its stand-down
    to; a later core reset must not stretch it (#255 review)."""
    monkeypatch.setattr(subprocess, "run", _serving(_payload(0, core_remaining=0)))
    assert gh_budget.snapshot().exhausted_reset == _GRAPHQL_RESET


def test_exhausted_reset_falls_back_to_other_zeroed_buckets(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _serving(_payload(4150, core_remaining=0)))
    assert gh_budget.snapshot().exhausted_reset == _CORE_RESET


def test_exhausted_reset_is_none_with_headroom(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _serving(_payload(4150)))
    assert gh_budget.snapshot().exhausted_reset is None


def test_snapshot_is_metered(monkeypatch):
    """rate_limit is free against GitHub's buckets but still a subprocess."""
    from agent_hq import gh_meter

    monkeypatch.setattr(subprocess, "run", _serving(_payload(4150)))
    gh_budget.snapshot()
    assert gh_meter.summarize(hours=1).by_kind.get("rate-limit") == gh_budget._SAMPLES


class TestSnapshotCachedIsNonBlocking:
    """``snapshot_cached`` answers from the memo and probes off-thread (#255)."""

    def test_returns_immediately_then_populates(self, monkeypatch):
        import threading
        import time as _time

        gh_budget.clear_cache()
        probed = threading.Event()

        def _slow_probe():
            probed.set()
            _time.sleep(1.0)
            return gh_budget.RateBudget(
                graphql_remaining=4321,
                graphql_limit=5000,
                graphql_reset=None,
                core_remaining=5000,
                core_limit=5000,
                core_reset=None,
                exhausted_reset=None,
            )

        monkeypatch.setattr(gh_budget, "_probe", _slow_probe)
        monkeypatch.setattr(gh_budget, "_reconcile", lambda b: b)

        started = _time.monotonic()
        first = gh_budget.snapshot_cached()
        elapsed = _time.monotonic() - started

        assert elapsed < 0.3, f"snapshot_cached blocked for {elapsed:.2f}s"
        assert first is None  # cold memo: unknown, which always ALLOWS

        assert probed.wait(timeout=3.0), "no background probe was started"
        deadline = _time.monotonic() + 3.0
        while _time.monotonic() < deadline:
            later = gh_budget.snapshot_cached()
            if later is not None:
                assert later.graphql_remaining == 4321
                return
            _time.sleep(0.05)
        raise AssertionError("memo never populated from the background probe")


class TestExpiredWindowStopsBlocking:
    """A below-floor reading whose reset has passed must not still gate (#255).

    The memo keeps a snapshot for ``_SNAPSHOT_TTL``, and while a probe is in
    flight callers are handed the *previous* one for longer still. A sample
    taken seconds before the hour therefore kept background refreshes blocked
    well past the reset that had already refilled the budget — exactly when
    recovery should begin.
    """

    def _expired(self, remaining):
        import time as _time

        return gh_budget.RateBudget(
            graphql_remaining=remaining,
            graphql_limit=5000,
            graphql_reset=_time.time() - 30.0,  # window already rolled over
            core_remaining=5000,
            core_limit=5000,
            core_reset=None,
            exhausted_reset=None,
        )

    def test_expired_below_floor_reading_allows_refresh(self, monkeypatch):
        gh_budget.clear_cache()
        monkeypatch.setattr(gh_budget, "snapshot", lambda: self._expired(3))
        assert gh_budget.allow_background_refresh(500) is True

    def test_live_below_floor_reading_still_blocks(self, monkeypatch):
        import time as _time

        gh_budget.clear_cache()
        live = gh_budget.RateBudget(
            graphql_remaining=3,
            graphql_limit=5000,
            graphql_reset=_time.time() + 600.0,
            core_remaining=5000,
            core_limit=5000,
            core_reset=None,
            exhausted_reset=None,
        )
        monkeypatch.setattr(gh_budget, "snapshot", lambda: live)
        assert gh_budget.allow_background_refresh(500) is False


class TestGraphQLBucketComesFromGraphQL:
    """The GraphQL bucket is read from GraphQL's own ``rateLimit`` object.

    Measured 2026-09-11: six consecutive REST ``rate_limit`` reads reported
    GraphQL ``used: 0 / remaining: 5000`` with a reset minted per call, while
    the ``rateLimit`` object — free to query, and the counter that actually
    rejects board queries — reported ``used: 1798`` three times identically.
    ``_is_synthetic`` could not catch it: the REST index keeps core busy, so
    the "every bucket untouched" signature never appears. The ⚙ row sat on
    5000/5000 through an evening of spending.
    """

    @staticmethod
    def _graphql_body(remaining, reset_at="2026-09-12T03:35:09Z"):
        return {
            "data": {
                "rateLimit": {
                    "limit": 5000,
                    "cost": 1,
                    "used": 5000 - remaining,
                    "remaining": remaining,
                    "resetAt": reset_at,
                }
            }
        }

    @staticmethod
    def _both(rest_body, graphql_body):
        """A fake gh that answers ``gh api graphql`` and ``gh api rate_limit``."""
        calls = []

        def _run(argv, **kwargs):
            calls.append(argv)
            if argv[:3] == ["gh", "api", "graphql"]:
                if graphql_body is None:
                    raise subprocess.CalledProcessError(1, argv, output="", stderr="gh: boom")
                body = graphql_body
            else:
                body = rest_body
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

        _run.calls = calls
        return _run

    def test_the_graphql_object_outranks_a_lying_rest_read(self, monkeypatch):
        fake = self._both(_payload(5000, graphql_reset=_LATE_RESET), self._graphql_body(3202))
        monkeypatch.setattr(subprocess, "run", fake)
        snap = gh_budget.snapshot()
        assert snap.graphql_remaining == 3202
        assert snap.graphql_limit == 5000
        assert snap.graphql_reset == 1789184109.0  # 2026-09-12T03:35:09Z
        assert snap.core_remaining == 4999  # core still comes from REST

    def test_one_rest_sample_suffices_once_graphql_answered(self, monkeypatch):
        """Sampling existed to out-vote lying replicas on the GraphQL bucket;
        with that bucket read from the source, core is display copy."""
        fake = self._both(_payload(5000), self._graphql_body(3202))
        monkeypatch.setattr(subprocess, "run", fake)
        gh_budget.snapshot()
        rest = [a for a in fake.calls if a[:3] != ["gh", "api", "graphql"]]
        assert len(rest) == 1

    def test_a_dead_graphql_bucket_pins_exhausted_reset(self, monkeypatch):
        fake = self._both(_payload(5000, graphql_reset=_LATE_RESET), self._graphql_body(0))
        monkeypatch.setattr(subprocess, "run", fake)
        snap = gh_budget.snapshot()
        assert snap.graphql_remaining == 0
        assert snap.exhausted_reset == 1789184109.0

    def test_falls_back_to_rest_sampling_when_graphql_fails(self, monkeypatch):
        fake = self._both(_payload(4150), None)
        monkeypatch.setattr(subprocess, "run", fake)
        snap = gh_budget.snapshot()
        assert snap.graphql_remaining == 4150
        rest = [a for a in fake.calls if a[:3] != ["gh", "api", "graphql"]]
        assert len(rest) == gh_budget._SAMPLES

    def test_the_graphql_probe_is_metered_as_free(self, monkeypatch):
        from agent_hq import gh_meter, gh_stats

        fake = self._both(_payload(5000), self._graphql_body(3202))
        monkeypatch.setattr(subprocess, "run", fake)
        gh_budget.snapshot()
        summary = gh_meter.summarize(hours=1)
        assert summary.graphql_by_kind.get("rate-limit-graphql") == 1
        assert gh_stats.estimate_graphql_points(summary.graphql_by_kind) == 0.0
