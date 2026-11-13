"""Which refreshes are allowed to spend the GraphQL budget (#255 review).

The degrade rule has exactly two halves and both surfaces got one of them
wrong, in opposite directions:

* the web's stale-while-revalidate **background** refresh passed
  ``force=True``, which skips ``allow_background_refresh`` *and* the
  post-lock freshness recheck — so the budget gate this branch exists to
  add was inert on the normal web path, and N processes each fetched;
* the TUI's **explicit** ``r`` refresh passed no ``force`` at all, so a
  user standing at the keyboard was refused when headroom was low.

These tests pin the rule itself: background never forces, explicit always
does.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import patch

from agent_hq.screens.dispatch_screen import DispatchScreen
from agent_hq.web.routes import issues as issues_route


@dataclass
class _Proj:
    number: int
    owner: str


class _RecordingCache:
    """Records the ``force`` argument of every ``get_issues`` call."""

    def __init__(self):
        self.forces: list[bool] = []
        self.closed_forces: list[bool] = []

    def get_issues_cached(self, number, owner):
        return None, False  # always stale, so the refresh has work to do

    def get_issues(self, number, owner, force=False):
        self.forces.append(force)
        return []

    def get_closed_keys(self, repos, force=False):
        self.closed_forces.append(force)
        return set()


class _Broker:
    async def publish(self, *a, **kw):
        return None


def test_background_refresh_does_not_force(monkeypatch):
    """The SWR background sweep must stay subject to the budget gate.

    ``force=True`` here bypassed ``allow_background_refresh`` entirely and
    also disabled the "a peer just fetched this" recheck inside the fetch
    lock, so concurrent processes serialized *and then each fetched*.
    Filtering to stale projects already guarantees a fetch, so nothing is
    lost by dropping it.
    """
    cache = _RecordingCache()

    class _App:
        class state:
            config = type("C", (), {"projects": [_Proj(12, "@me")]})()
            github_cache = cache
            broker = _Broker()

    monkeypatch.setattr(issues_route, "_load_issues_swr", lambda app: _async_value(([], False)))
    asyncio.run(issues_route._refresh_issues_background(_App))

    assert cache.forces, "background refresh never fetched the stale project"
    assert all(f is False for f in cache.forces), (
        f"background refresh forced past the budget gate: {cache.forces}"
    )


def test_explicit_tui_refresh_forces():
    """``r`` in the TUI is a present user deliberately spending budget."""
    screen = DispatchScreen.__new__(DispatchScreen)
    screen._config = object()
    calls: list[bool] = []
    screen._refresh_data = lambda force=False: calls.append(force)

    screen.action_refresh()

    assert calls == [True], f"explicit refresh did not force: {calls}"


def _async_value(value):
    async def _coro():
        return value

    return _coro()


def _run_tui_refresh(force):
    """Drive the real worker body (unwrapped from ``@work``) with stubs."""
    from agent_hq.screens import dispatch_screen as ds

    cache = _RecordingCache()
    screen = DispatchScreen.__new__(DispatchScreen)
    screen._config = type("C", (), {"projects": [_Proj(12, "@me")], "repos": {}})()
    screen._github_cache = cache
    screen._triage_store = None
    screen._apply_refresh = lambda *a, **k: None
    fake_app = type("A", (), {"call_from_thread": staticmethod(lambda fn, *a: fn(*a))})()
    original = ds.detect_active_worktrees
    ds.detect_active_worktrees = lambda repos: set()
    try:
        # `app` is a read-only property on Screen; patch it on the class.
        with patch.object(DispatchScreen, "app", new=fake_app):
            DispatchScreen._refresh_data.__wrapped__(screen, force)
    finally:
        ds.detect_active_worktrees = original
    return cache


def test_explicit_tui_refresh_forces_closed_keys_too():
    """The board and the closed-keys lookup are one refresh to the user; a
    closed issue must not outlive a deliberate ``r`` (#255 review)."""
    cache = _run_tui_refresh(force=True)
    assert cache.forces == [True]
    assert cache.closed_forces == [True], cache.closed_forces


def test_background_tui_refresh_does_not_force_closed_keys():
    cache = _run_tui_refresh(force=False)
    assert cache.closed_forces == [False]
