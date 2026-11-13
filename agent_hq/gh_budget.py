"""GitHub rate-limit headroom and the degrade policy built on it (#255).

The 2026-08-24 incident exhausted the user-wide GraphQL budget while REST sat
untouched, and agent-hq had no idea — it kept spending the last of a budget
shared with every other tool the user runs. This module is the awareness half:
read the (free) ``rate_limit`` endpoint, expose what's left, and refuse to
spend the bottom of the budget on refreshes nobody asked for.

**The endpoint under-reports consumption, and that is the whole difficulty.**
Measured 2026-08-26 with GraphQL fully exhausted (every ``gh issue view``
returning "API rate limit already exceeded"), twelve consecutive
``gh api rate_limit`` reads returned:

    5 reads -> used 5000 / remaining 0     reset = the real window end
    7 reads -> used    0 / remaining 5000  reset = now + 1h

GitHub serves ``rate_limit`` from replicas with inconsistent counters; one that
has not seen the current window reports a pristine budget. The top-level
``.rate`` object agrees with whichever replica answered, so it is not a
cross-check. Taken naively, the majority answer is "you have 5,000 points" at
the exact moment you have none — it fails in the dangerous direction.

Three defences, none of which is sufficient alone — the order matters:

1. **Observed rejections outrank everything.** :func:`note_rate_limited`
   records that a real gh call came back rate-limited, and
   :func:`allow_background_refresh` then refuses until reset without
   consulting the meter at all. This is the only fully reliable signal.
2. **Selection by earliest ``reset``.** A replica that never saw the current
   window mints one *at request time*, so its reset is always later than the
   real window's. Preferring the earliest reset picks the truthful read
   whenever one appears in the sample, and unlike a "reset is ~1h out" test it
   is scale-free. Minimum ``remaining`` is the tie-break within a window.
3. **A seen window is sticky** (:func:`_reconcile`). Sampling cannot help when
   *every* read is stale: measured live at a 70% stale rate, that is ~17% of
   5-sample probes, and it was observed reporting 5,000 remaining on a dead
   budget. But a real window does not vanish, so once observed it stands until
   its reset passes, and a read describing a later window is discarded as
   synthesized.

Deliberately *not* done: discarding reads for ``used == 0`` (the legitimate
state at the top of the hour), validating ``used + remaining == 5000`` (the
lying read satisfies it), or raising the sample count to brute-force
reliability (12 sequential subprocesses on a page render, still not reliable).

Every failure path degrades to "unknown", and unknown always means *allow*: a
broken probe must never be able to lock the app out of refreshing. The meter
is advisory; defence 1 is what actually protects the budget.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from . import eventlog, gh_meter

# Below this many GraphQL points remaining, background refreshes stand down so
# the user's other tooling (and their own explicit refreshes) keep working.
DEFAULT_GRAPHQL_FLOOR = 500

_SNAPSHOT_TTL = 60.0

# Enough reads that an all-stale sample is unlikely: at the measured ~58% stale
# rate, 0.58^5 ~= 6.6%. The endpoint is free, so the only cost is latency, and
# the memo below means we pay it once a minute.
_SAMPLES = 5

# How long to treat the budget as exhausted when a rate-limit failure arrives
# without a usable reset time. GitHub's windows are hourly, but guessing an
# hour would strand the app long after an early-in-window reset; 10 minutes is
# short enough to self-correct and long enough to stop a hammering loop.
_BLIND_EXHAUSTION_WINDOW = 600.0

_snapshot_cache: tuple[float, "RateBudget | None"] | None = None
# Two locks, deliberately. `_probe_lock` is held across _SAMPLES subprocesses —
# up to ~50 s in the worst case — and is only ever taken non-blocking, so a
# project sweep arriving at once does not queue behind it. `_state_lock` guards
# the cheap in-memory state (the observed window, the memo, the warn edge) and
# must never be held across a subprocess: `exhausted_until()` is the ground-truth
# read on the hot path, and a single lock would make it block behind a probe.
# Ordering, where both are needed: _probe_lock then _state_lock, never reverse.
_probe_lock = threading.Lock()
_state_lock = threading.Lock()
_floor = DEFAULT_GRAPHQL_FLOOR
_below_floor = False  # edge-trigger for the warn
# Ground truth, set by whoever actually got rejected. Outranks the meter.
_exhausted_until: float | None = None
# The most authoritative window we have actually observed, kept until its reset
# passes. A real window does not vanish, so an all-stale probe must not be able
# to resurrect a full budget on top of it.
_seen_window: "RateBudget | None" = None


@dataclass(frozen=True)
class RateBudget:
    """Remaining headroom in GitHub's two independent 5,000/hr buckets."""

    graphql_remaining: int
    graphql_limit: int
    graphql_reset: float | None
    core_remaining: int
    core_limit: int
    core_reset: float | None
    # Latest reset among buckets that are actually at zero — what #254's
    # "rate limited, back at HH:MM" copy needs.
    exhausted_reset: float | None


def set_floor(floor: int) -> None:
    """Override the GraphQL floor (from ``config.github.graphql_floor``)."""
    global _floor
    _floor = floor


def clear_cache() -> None:
    """Drop the memoized snapshot and the observed-exhaustion state (tests,
    and after a manual ``gh auth`` fix)."""
    global _snapshot_cache, _below_floor, _exhausted_until, _seen_window
    with _state_lock:
        _snapshot_cache = None
        _below_floor = False
        _exhausted_until = None
        _seen_window = None


def _expire_snapshot_memo() -> None:
    """Force the next :func:`snapshot` to re-probe, keeping what we've learned.

    Test seam. ``clear_cache`` deliberately forgets everything; this forgets
    only the 60 s memo.
    """
    global _snapshot_cache
    with _state_lock:
        _snapshot_cache = None


def note_rate_limited(reset_at: float | None) -> None:
    """Record that a real gh call was rejected for rate limiting.

    This is the authoritative signal. ``gh api rate_limit`` cannot be trusted
    to report exhaustion — measured 2026-08-26, 19 of 30 reads reported a full
    budget while GraphQL was provably dead — but a call that came back with
    "API rate limit exceeded" is ground truth. Callers pass the reset from
    ``GitHubFetchError.reset_at`` when they have one.
    """
    global _exhausted_until
    until = reset_at if reset_at else time.time() + _BLIND_EXHAUSTION_WINDOW
    with _state_lock:
        # Never shorten a known-longer window with a blind guess.
        if _exhausted_until is None or until > _exhausted_until:
            _exhausted_until = until


def exhausted_until() -> float | None:
    """When observed exhaustion lifts, or ``None`` if not currently exhausted."""
    with _state_lock:
        until = _exhausted_until
    if until is None or time.time() >= until:
        return None
    return until


def is_exhausted() -> bool:
    """True while a real rejection says the budget is spent."""
    return exhausted_until() is not None


def _read_once() -> dict | None:
    """One ``rate_limit`` read. ``None`` on any failure."""
    try:
        result = gh_meter.run_gh(["gh", "api", "rate_limit"], kind="rate-limit")
        payload = json.loads(result.stdout)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
    ):
        return None
    resources = payload.get("resources") if isinstance(payload, dict) else None
    return resources if isinstance(resources, dict) else None


_RATE_LIMIT_QUERY = "query { rateLimit { limit used remaining resetAt } }"


def _read_graphql_once() -> tuple[int, int, float | None] | None:
    """The GraphQL bucket from GraphQL's own ``rateLimit`` object.

    ``(remaining, limit, reset)``, or ``None`` on any failure — including the
    rejection an exhausted budget returns, which is exactly when the REST
    fallback below still has to carry the answer.

    This is the counter that actually rejects board queries, and querying it
    is free (``used`` did not move across three consecutive reads). Measured
    2026-09-11: six REST ``rate_limit`` reads reported ``used: 0`` with a
    reset minted per call, while this object reported ``used: 1798``, three
    times identically. The REST endpoint's GraphQL bucket comes from a replica
    that may never have seen the window; this one does not.
    """
    try:
        result = gh_meter.run_gh(
            ["gh", "api", "graphql", "-f", f"query={_RATE_LIMIT_QUERY}"],
            kind="rate-limit-graphql",
        )
        payload = json.loads(result.stdout)
        bucket = payload["data"]["rateLimit"]
        remaining, limit = bucket["remaining"], bucket["limit"]
        if not isinstance(remaining, int) or not isinstance(limit, int):
            return None
        reset_at = bucket.get("resetAt")
        reset = None
        if isinstance(reset_at, str):
            reset = datetime.fromisoformat(reset_at.replace("Z", "+00:00")).timestamp()
        return remaining, limit, reset
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        KeyError,
    ):
        return None


def _bucket(resources: dict, name: str) -> dict:
    value = resources.get(name)
    return value if isinstance(value, dict) else {}


def _is_synthetic(resources: dict) -> bool:
    """Whether this read came from a replica that never saw the window.

    Such a replica mints a fresh window *at request time*, so every bucket
    reports an identical ``reset`` roughly an hour out, and every bucket reads
    full. A truthful response has per-bucket resets that differ, because each
    bucket's window started when its own first request landed. Measured
    2026-08-26 (truthful: graphql reset 1787749543 vs core 1787749435;
    synthetic: both 1787751108, == now + 3600).

    Heuristic, on undocumented behaviour — so it is deliberately narrow, and a
    false positive is harmless: if every bucket really is untouched then the
    budget really is full, and discarding the read yields "unknown", which
    allows exactly as a 5000 reading would have. Being wrong costs nothing;
    being right avoids reporting a full budget on a dead one.

    Both conditions must hold: identical resets across buckets, and no usage
    anywhere. Deliberately *no* "reset is ~1h out" check — that would hang the
    filter on a constant that decays as the window advances, and it caught a
    fixture out within the hour it was written.
    """
    buckets = [b for b in resources.values() if isinstance(b, dict)]
    resets = [b["reset"] for b in buckets if isinstance(b.get("reset"), (int, float))]
    if len(resets) < 2 or len(set(resets)) != 1:
        return False
    return all(b.get("used") in (0, None) for b in buckets)


def _more_authoritative(
    candidate: tuple[int, int, float | None],
    incumbent: tuple[int, int, float | None],
) -> bool:
    """Whether ``candidate`` describes the real window better than ``incumbent``.

    Earliest reset wins — a synthesized window is always later than the real
    one. A read with no reset at all can only win on the ``remaining``
    tie-break, since it carries no evidence about which window it describes.
    """
    cand_reset, inc_reset = candidate[2], incumbent[2]
    if cand_reset is not None and inc_reset is not None and cand_reset != inc_reset:
        return cand_reset < inc_reset
    return candidate[0] < incumbent[0]


def _probe() -> RateBudget | None:
    """Sample the endpoint ``_SAMPLES`` times and pick the authoritative read.

    **Selection is by earliest ``reset``, not lowest ``remaining``.** A replica
    that has not seen the current window mints a fresh one at request time, so
    its reset is always *later* than the real window's. Preferring the earliest
    reset therefore identifies the authoritative read no matter how many
    replicas lie, and — unlike a "reset is ~1 h out" test — it is scale-free,
    with nothing that decays as the window advances.

    Minimum ``remaining`` remains the tie-break, for replicas that genuinely
    share the current window but lag on the counter.

    Sampling alone was demonstrably not enough: at the measured 70% stale rate,
    five samples miss the truthful replica ~17% of the time, and a live run hit
    exactly that — reporting 5,000 remaining while every call was rejected.
    That is why :func:`note_rate_limited` outranks this function entirely.
    """
    best: dict[str, tuple[int, int, float | None]] = {}
    exhausted: list[tuple[bool, float]] = []
    saw_any = False

    # The GraphQL bucket is read from GraphQL itself when it will answer
    # (:func:`_read_graphql_once`). Sampling the REST endpoint existed to
    # out-vote lying replicas on *that* bucket; once it is known, core is
    # display copy and a single REST read is enough.
    graphql = _read_graphql_once()
    if graphql is not None:
        best["graphql"] = graphql
        saw_any = True
        if graphql[0] == 0 and graphql[2] is not None:
            exhausted.append((True, graphql[2]))
    samples = 1 if graphql is not None else _SAMPLES
    rest_buckets = ("core",) if graphql is not None else ("graphql", "core")

    for _ in range(samples):
        resources = _read_once()
        if resources is None or _is_synthetic(resources):
            continue
        saw_any = True
        for name in rest_buckets:
            bucket = _bucket(resources, name)
            remaining = bucket.get("remaining")
            limit = bucket.get("limit")
            if not isinstance(remaining, int) or not isinstance(limit, int):
                continue
            reset = bucket.get("reset")
            reset_f = float(reset) if isinstance(reset, (int, float)) else None
            current = best.get(name)
            if current is None or _more_authoritative((remaining, limit, reset_f), current):
                best[name] = (remaining, limit, reset_f)
        for name, bucket in resources.items():
            if name == "graphql" and graphql is not None:
                continue  # already answered by the source
            if isinstance(bucket, dict) and bucket.get("remaining") == 0 and bucket.get("reset"):
                # GraphQL first: this value is what note_rate_limited pins the
                # stand-down to, and "never shorten" would otherwise extend a
                # GraphQL stand-down to some unrelated bucket's later reset
                # (#255 review). Other zeroed buckets remain display copy.
                exhausted.append((name == "graphql", float(bucket["reset"])))

    if not saw_any or "graphql" not in best:
        return None

    gql = best["graphql"]
    core = best.get("core", (0, 0, None))
    return RateBudget(
        graphql_remaining=gql[0],
        graphql_limit=gql[1],
        graphql_reset=gql[2],
        core_remaining=core[0],
        core_limit=core[1],
        core_reset=core[2],
        exhausted_reset=max(exhausted)[1] if exhausted else None,
    )


def _reconcile(fresh: "RateBudget | None") -> "RateBudget | None":
    """Blend a fresh probe with the last window we actually observed.

    Selection by earliest reset picks the truthful replica whenever one shows
    up in the sample, but cannot help when every read is stale — measured live
    at a 70% stale rate, ~17% of 5-sample probes. A real window does not
    disappear, though: if we have already seen a window whose reset has not
    passed, a fresh read describing a *later* (therefore synthesized) window is
    not new information, and the remembered one stands.

    Readings inside the same window are always believed, in both directions —
    same window means same truth, and spending must still track downwards.
    """
    global _seen_window
    remembered = _seen_window
    if remembered is not None and remembered.graphql_reset is not None:
        if time.time() >= remembered.graphql_reset:
            remembered = None  # that window is over
    if fresh is None:
        _seen_window = remembered
        return remembered
    if (
        remembered is not None
        and remembered.graphql_reset is not None
        and fresh.graphql_reset is not None
        and fresh.graphql_reset > remembered.graphql_reset
    ):
        # A window later than one still running: a synthesized read.
        _seen_window = remembered
        return remembered
    if (
        remembered is not None
        and fresh.graphql_reset == remembered.graphql_reset
        and fresh.graphql_remaining > remembered.graphql_remaining
    ):
        # Same window, MORE headroom. Inside a window `remaining` can only
        # fall, and a replica that lags on its counter under-reports usage,
        # so it can only over-report headroom: the lower reading is always
        # the one nearer the truth. Believing the higher one reopened the
        # gate on a budget already seen below the floor (#255 review).
        _seen_window = remembered
        return remembered
    # A window is remembered only if it can expire. One without a reset
    # would otherwise stand for the life of the process the moment later
    # probes started failing (#255 review).
    _seen_window = fresh if fresh.graphql_reset is not None else None
    return fresh


def snapshot() -> RateBudget | None:
    """Current headroom, memoized for ``_SNAPSHOT_TTL``.

    ``None`` when the probe could not answer. Callers must treat that as
    "unknown", never as "empty".
    """
    global _snapshot_cache
    cached = _snapshot_cache
    if cached is not None and time.time() - cached[0] < _SNAPSHOT_TTL:
        return cached[1]
    # A whole project sweep can arrive here at once. Exactly one probes; the
    # rest take whatever is cached rather than queueing behind _SAMPLES
    # subprocesses.
    if not _probe_lock.acquire(blocking=False):
        return cached[1] if cached else None
    try:
        cached = _snapshot_cache
        if cached is not None and time.time() - cached[0] < _SNAPSHOT_TTL:
            return cached[1]
        budget = _reconcile(_probe())
        _snapshot_cache = (time.time(), budget)
        return budget
    finally:
        _probe_lock.release()


def _refresh_memo() -> None:
    """Populate the memo on a background thread; never raises."""
    try:
        snapshot()
    except Exception:  # pragma: no cover - snapshot already swallows its own
        pass


def snapshot_cached() -> RateBudget | None:
    """Memoized headroom that NEVER probes inline — safe on an event loop.

    :func:`snapshot` can sit in ``_SAMPLES`` sequential subprocesses for up
    to ``_SAMPLES`` x ``GH_TIMEOUT`` seconds. The settings menu asks for the
    number from inside Jinja rendering, and the pages carrying it are
    ``async def`` routes that render on the event loop — so calling the
    blocking form there stalled every other request and all background async
    work for the probe's duration, once per memo expiry (#255 review).

    Returns whatever the memo holds, which is ``None`` on a cold process:
    that reads as "unknown", the row simply hides, and unknown always
    ALLOWS. A refresh is kicked onto a daemon thread so the number appears
    a beat later instead of blocking the request that noticed it was stale.
    """
    cached = _snapshot_cache
    if cached is not None and time.time() - cached[0] < _SNAPSHOT_TTL:
        return cached[1]
    # `snapshot` self-guards with a non-blocking `_probe_lock`, so a
    # redundant thread would exit immediately; this check just avoids
    # creating one per render while a probe is already running.
    if not _probe_lock.locked():
        threading.Thread(target=_refresh_memo, name="gh-budget-probe", daemon=True).start()
    return cached[1] if cached else None


def allow_background_refresh(floor: int | None = None) -> bool:
    """Whether a TTL-driven / stale-while-revalidate refresh may spend budget.

    Explicit user refreshes bypass this entirely — the user is present and
    deliberately spending. Unknown headroom allows the refresh.

    Checked in order of trustworthiness: an observed rejection is ground truth
    and short-circuits everything; only then do we consult the meter, which
    can and does report a full budget on a dead one.
    """
    global _below_floor
    if is_exhausted():
        return False
    budget = snapshot()
    if budget is None:
        return True
    # A reading whose window has already reset describes a budget that no
    # longer exists. The memo holds one for _SNAPSHOT_TTL, and an in-flight
    # probe hands out the previous one for longer still, so a below-floor
    # sample taken seconds before the hour would keep refreshes blocked well
    # past the reset that already refilled them (#255 review). Unknown allows.
    if budget.graphql_reset is not None and budget.graphql_reset <= time.time():
        _below_floor = False
        return True
    limit = _floor if floor is None else floor
    below = budget.graphql_remaining < limit
    if below and not _below_floor:
        # Edge-triggered: one warn per descent, not one per blocked refresh.
        eventlog.emit(
            "warn",
            "github graphql headroom low",
            {
                "remaining": budget.graphql_remaining,
                "limit": budget.graphql_limit,
                "floor": limit,
                "reset_at": budget.graphql_reset,
            },
        )
    _below_floor = below
    return not below
