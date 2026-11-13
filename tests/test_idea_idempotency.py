"""Tests for the in-process idempotency LRU."""

import threading
import time

import pytest

from agent_hq.idea.idempotency import IdempotencyCache


def test_returns_none_when_unseen():
    cache = IdempotencyCache(max_size=10, ttl_seconds=60)
    assert cache.get("token-hash", "key-1") is None


def test_set_then_get_returns_value():
    cache = IdempotencyCache(max_size=10, ttl_seconds=60)
    cache.set("th", "k", {"url": "x"})
    assert cache.get("th", "k") == {"url": "x"}


def test_different_token_isolates_keys():
    cache = IdempotencyCache(max_size=10, ttl_seconds=60)
    cache.set("th-a", "k", {"url": "x"})
    assert cache.get("th-b", "k") is None


def test_different_key_isolates_values():
    cache = IdempotencyCache(max_size=10, ttl_seconds=60)
    cache.set("th", "k1", {"url": "x"})
    cache.set("th", "k2", {"url": "y"})
    assert cache.get("th", "k1") == {"url": "x"}
    assert cache.get("th", "k2") == {"url": "y"}


def test_lru_eviction_when_full():
    cache = IdempotencyCache(max_size=2, ttl_seconds=60)
    cache.set("th", "k1", {"v": 1})
    cache.set("th", "k2", {"v": 2})
    cache.set("th", "k3", {"v": 3})
    assert cache.get("th", "k1") is None  # evicted
    assert cache.get("th", "k2") == {"v": 2}
    assert cache.get("th", "k3") == {"v": 3}


def test_get_promotes_to_most_recent():
    cache = IdempotencyCache(max_size=2, ttl_seconds=60)
    cache.set("th", "k1", {"v": 1})
    cache.set("th", "k2", {"v": 2})
    cache.get("th", "k1")  # promote
    cache.set("th", "k3", {"v": 3})
    assert cache.get("th", "k1") == {"v": 1}
    assert cache.get("th", "k2") is None  # evicted (LRU)


def test_ttl_expiry():
    cache = IdempotencyCache(max_size=10, ttl_seconds=0.05)
    cache.set("th", "k", {"v": 1})
    time.sleep(0.06)
    assert cache.get("th", "k") is None


def test_none_key_skips_cache():
    """When idempotency_key is None, set/get must be no-ops."""
    cache = IdempotencyCache(max_size=10, ttl_seconds=60)
    cache.set("th", None, {"v": 1})
    assert cache.get("th", None) is None


# --- reserve(): per-key serialization (#181 AHQ-005) -------------------------


def test_reserve_none_key_is_noop_and_leaves_no_lock():
    """reserve(None) must not block and must not register a per-key lock."""
    cache = IdempotencyCache(max_size=10, ttl_seconds=60)
    with cache.reserve("th", None):
        pass
    assert cache._key_locks == {}


def test_reserve_serializes_same_key():
    """A second reserve() for the same key blocks until the first exits."""
    cache = IdempotencyCache(max_size=10, ttl_seconds=60)
    order: list[str] = []
    a_started = threading.Event()
    a_may_exit = threading.Event()

    def worker_a():
        with cache.reserve("th", "k"):
            order.append("A-enter")
            a_started.set()
            a_may_exit.wait(timeout=2)
            order.append("A-exit")

    def worker_b():
        with cache.reserve("th", "k"):
            order.append("B-enter")

    ta = threading.Thread(target=worker_a)
    ta.start()
    assert a_started.wait(timeout=2)

    tb = threading.Thread(target=worker_b)
    tb.start()
    # B must be blocked while A holds the reservation.
    time.sleep(0.05)
    assert "B-enter" not in order

    a_may_exit.set()
    ta.join(timeout=2)
    tb.join(timeout=2)
    assert order == ["A-enter", "A-exit", "B-enter"]


def test_reserve_different_keys_do_not_serialize():
    """Distinct keys hold independent locks and run concurrently."""
    cache = IdempotencyCache(max_size=10, ttl_seconds=60)
    both_in = threading.Barrier(2, timeout=2)

    def worker(key: str):
        with cache.reserve("th", key):
            both_in.wait()  # raises BrokenBarrierError if the other is blocked

    t1 = threading.Thread(target=worker, args=("k1",))
    t2 = threading.Thread(target=worker, args=("k2",))
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert not t1.is_alive() and not t2.is_alive()


def test_reserve_cleans_up_lock_registry():
    """Per-key locks are removed once no caller references them (no leak)."""
    cache = IdempotencyCache(max_size=10, ttl_seconds=60)
    for _ in range(3):
        with cache.reserve("th", "k"):
            pass
    assert cache._key_locks == {}


def test_reserve_releases_refcount_if_acquire_raises(monkeypatch):
    """If lock.acquire() raises, the bumped refcount must still be released so
    the registry can't leak an entry (near-unreachable, but the registry is
    meant to be self-cleaning). #181 review."""
    cache = IdempotencyCache(max_size=10, ttl_seconds=60)

    class BadLock:
        def acquire(self):
            raise RuntimeError("boom")

        def release(self):  # pragma: no cover - must never run when acquire failed
            raise AssertionError("release must not run after a failed acquire")

    def fake_acquire(compound):
        # Mimic the real _acquire_key_lock: register a refcount, return a lock.
        cache._key_locks[compound] = (BadLock(), 1)
        return cache._key_locks[compound][0]

    monkeypatch.setattr(cache, "_acquire_key_lock", fake_acquire)

    with pytest.raises(RuntimeError, match="boom"):
        with cache.reserve("th", "k"):
            pass  # pragma: no cover - reserve body never runs

    assert cache._key_locks == {}  # refcount released despite the failed acquire
