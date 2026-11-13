"""In-process LRU for Idea Inbox idempotency keys.

Phone clients (iOS Shortcut) and the CLI generate a UUID per submit and pass
it as `idempotency_key`. Repeated POSTs with the same key return the cached
result instead of filing a duplicate issue. Cache is in-process — single-
process web app, so no need for a shared store.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Iterator


class IdempotencyCache:
    def __init__(self, max_size: int = 256, ttl_seconds: float = 600.0):
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._items: OrderedDict[tuple[str, str], tuple[float, Any]] = OrderedDict()
        # Guards both `_items` and the `_key_locks` registry. Held only for the
        # fast bookkeeping below — never across the classify+file pipeline, so
        # it can't stall a request. The per-key content lock (see `reserve`) is
        # what serializes the pipeline; this one just protects the maps.
        self._meta = threading.Lock()
        # (token_hash, key) -> (content lock, refcount). Refcounted so a lock is
        # dropped only once no in-flight or waiting caller references it.
        self._key_locks: dict[tuple[str, str], tuple[threading.Lock, int]] = {}

    def get(self, token_hash: str, key: str | None) -> Any | None:
        if key is None:
            return None
        compound = (token_hash, key)
        with self._meta:
            entry = self._items.get(compound)
            if entry is None:
                return None
            timestamp, value = entry
            if time.monotonic() - timestamp > self._ttl:
                self._items.pop(compound, None)
                return None
            # Promote to most-recently-used
            self._items.move_to_end(compound)
            return value

    def set(self, token_hash: str, key: str | None, value: Any) -> None:
        if key is None:
            return
        compound = (token_hash, key)
        with self._meta:
            self._items[compound] = (time.monotonic(), value)
            self._items.move_to_end(compound)
            while len(self._items) > self._max_size:
                self._items.popitem(last=False)

    @contextmanager
    def reserve(self, token_hash: str, key: str | None) -> Iterator[None]:
        """Serialize concurrent requests sharing an idempotency key (#181).

        The handler runs in a threadpool, so a same-key retry can arrive while
        the first request is still inside the (60s+) classify+file pipeline.
        Holding this per-key lock across that section forces the retry to wait,
        then find the first request's result in the cache instead of re-filing.

        A no-op when ``key is None`` (anonymous submits don't dedupe, matching
        ``get``/``set``). The blocking acquire happens outside ``_meta`` so a
        waiter never holds the bookkeeping lock; ``get``/``set`` only grab
        ``_meta`` briefly inside the held content lock, so there's no cycle.
        """
        if key is None:
            yield
            return
        compound = (token_hash, key)
        lock = self._acquire_key_lock(compound)  # bumps the refcount
        try:
            lock.acquire()
            # Release the lock only inside this inner block, which is entered
            # solely after acquire() succeeds — so a raising acquire() never
            # triggers a spurious release. The outer finally still runs, so the
            # refcount bumped above is always balanced (no registry leak).
            try:
                yield
            finally:
                lock.release()
        finally:
            self._release_key_lock(compound)

    def _acquire_key_lock(self, compound: tuple[str, str]) -> threading.Lock:
        with self._meta:
            entry = self._key_locks.get(compound)
            if entry is None:
                lock = threading.Lock()
                self._key_locks[compound] = (lock, 1)
                return lock
            lock, refs = entry
            self._key_locks[compound] = (lock, refs + 1)
            return lock

    def _release_key_lock(self, compound: tuple[str, str]) -> None:
        with self._meta:
            entry = self._key_locks.get(compound)
            if entry is None:
                return
            lock, refs = entry
            if refs <= 1:
                del self._key_locks[compound]
            else:
                self._key_locks[compound] = (lock, refs - 1)
