"""Tests for ``agent_hq.web.state`` — the signed ``dispatch_state``
cookie codec used by the web dispatch view to persist filters,
selection, and groups across reloads."""

from __future__ import annotations

from fastapi import Request, Response

from agent_hq.web.auth import TokenManager
from agent_hq.web.state import (
    COOKIE_NAME,
    DispatchState,
    read_dispatch_state,
    write_dispatch_state,
)


def _mk_request(cookie_value: str | None = None) -> Request:
    """Build a minimal Request that the codec can read cookies off of."""
    headers: list[tuple[bytes, bytes]] = []
    if cookie_value is not None:
        headers.append((b"cookie", f"{COOKIE_NAME}={cookie_value}".encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/dispatch",
        "headers": headers,
    }
    return Request(scope=scope)


def _round_trip(state: DispatchState, tm: TokenManager) -> DispatchState:
    """Write to a Response, scrape the Set-Cookie header, parse it back
    through ``read_dispatch_state`` to verify the codec is symmetric."""
    response = Response()
    write_dispatch_state(response, state, tm)
    raw = response.headers["set-cookie"]
    # ``COOKIE_NAME=<value>; Path=/; …`` — split on ``=`` once and on ``;``
    # to extract the bare value.
    assert raw.startswith(f"{COOKIE_NAME}=")
    value = raw.split("=", 1)[1].split(";", 1)[0]
    request = _mk_request(value)
    return read_dispatch_state(request, tm)


class TestDefaults:
    def test_missing_cookie_returns_empty_state(self, tmp_path):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        state = read_dispatch_state(_mk_request(None), tm)
        assert state == DispatchState()
        # Empty state means: no filters set, no selection, no groups.
        assert state.repos == []
        assert state.efforts == []
        assert state.fitnesses == []
        assert state.statuses == []
        assert state.query == ""
        assert state.selected == []
        assert state.groups == []

    def test_default_constructor_yields_empty_state(self):
        s = DispatchState()
        assert s.repos == []
        assert s.efforts == []
        assert s.fitnesses == []
        assert s.statuses == []
        assert s.query == ""
        assert s.selected == []
        assert s.groups == []
        assert s.sort == ""  # #101: empty == default order
        assert s.created_dir == ""  # #156: empty == desc (newest first)
        assert s.updated_dir == ""

    def test_old_cookie_without_sort_loads_as_empty(self):
        # Forward-compat: a cookie written before #101 has no ``sort`` key.
        s = DispatchState.from_dict({"repos": ["a/x"], "query": "hi"})
        assert s.sort == ""


class TestSortHelpers:
    """#156: per-field direction memory + activate/flip semantics."""

    def test_direction_for_defaults_to_desc(self):
        s = DispatchState()
        assert s.direction_for("created") == "desc"
        assert s.direction_for("updated") == "desc"

    def test_apply_sort_activates_inactive_button(self):
        s = DispatchState(sort="default")
        s.apply_sort("created")
        assert s.sort == "created"
        assert s.created_dir == ""  # untouched → still desc by default

    def test_apply_sort_reclick_flips_only_that_field(self):
        s = DispatchState(sort="created")
        s.apply_sort("created")  # re-click flips
        assert s.created_dir == "asc"
        assert s.updated_dir == ""  # other field untouched
        s.apply_sort("created")  # flip back
        assert s.created_dir == "desc"

    def test_apply_sort_switch_keeps_remembered_direction(self):
        s = DispatchState(sort="created", updated_dir="asc")
        s.apply_sort("updated")
        assert s.sort == "updated"
        assert s.updated_dir == "asc"  # remembered, not reset

    def test_sort_params_default(self):
        assert DispatchState().sort_params() == ("default", True)

    def test_sort_params_created_asc(self):
        assert DispatchState(sort="created", created_dir="asc").sort_params() == ("created", False)

    def test_legacy_newest_migrates_to_updated_desc(self):
        s = DispatchState.from_dict({"sort": "newest"})
        assert s.sort == "updated"
        assert s.updated_dir == "desc"

    def test_legacy_oldest_migrates_to_updated_asc(self):
        s = DispatchState.from_dict({"sort": "oldest"})
        assert s.sort == "updated"
        assert s.updated_dir == "asc"

    def test_dir_fields_round_trip(self, tmp_path):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        recovered = _round_trip(DispatchState(sort="created", created_dir="asc"), tm)
        assert recovered.sort == "created"
        assert recovered.created_dir == "asc"


class TestRoundTrip:
    def test_full_state_round_trips(self, tmp_path):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        original = DispatchState(
            repos=["a/x", "b/y"],
            efforts=["s", "m"],
            fitnesses=["solo"],
            statuses=["todo"],
            query="dispatch",
            selected=["a/x#1", "a/x#2"],
            groups=[["a/x#1", "a/x#2"]],
            sort="created",
            created_dir="asc",
            updated_dir="desc",
        )
        recovered = _round_trip(original, tm)
        assert recovered == original

    def test_sort_round_trips(self, tmp_path):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        recovered = _round_trip(DispatchState(sort="updated"), tm)
        assert recovered.sort == "updated"

    def test_empty_state_round_trips(self, tmp_path):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        recovered = _round_trip(DispatchState(), tm)
        assert recovered == DispatchState()


class TestTamperingRejected:
    def test_unsigned_cookie_falls_back_to_empty(self, tmp_path):
        """Raw JSON without an HMAC prefix must be rejected — otherwise
        anyone with the cookie store could inject arbitrary state."""
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        # JSON-only, no signature prefix.
        request = _mk_request("eyJyZXBvcyI6IFtdfQ==")
        state = read_dispatch_state(request, tm)
        assert state == DispatchState()

    def test_wrong_signature_falls_back_to_empty(self, tmp_path):
        """Valid payload + wrong HMAC must be rejected."""
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        # Get a legitimate cookie and corrupt the signature.
        response = Response()
        write_dispatch_state(response, DispatchState(query="hello"), tm)
        raw = response.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
        # Flip a hex char in the signature half (everything before the
        # first ``.`` separator).
        sig, payload = raw.split(".", 1)
        bad_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
        request = _mk_request(f"{bad_sig}.{payload}")
        state = read_dispatch_state(request, tm)
        assert state == DispatchState()

    def test_garbage_cookie_falls_back_to_empty(self, tmp_path):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        request = _mk_request("not-even-close-to-a-valid-cookie")
        state = read_dispatch_state(request, tm)
        assert state == DispatchState()

    def test_token_rotation_invalidates_cookie(self, tmp_path):
        """``TokenManager._key`` mixes in the file's mtime, so touching
        or rotating the token invalidates every outstanding cookie. The
        dispatch cookie inherits this — same security property as the
        auth cookie. Without it, an old laptop with a stale auth cookie
        could still send a self-consistent dispatch_state."""
        import os
        import time

        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        response = Response()
        write_dispatch_state(response, DispatchState(query="x"), tm)
        raw = response.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]

        # Force a different mtime_ns so the derived key changes.
        time.sleep(0.01)
        os.utime(tm.token_file, None)

        request = _mk_request(raw)
        state = read_dispatch_state(request, tm)
        assert state == DispatchState()

    def test_token_content_change_invalidates_cookie(self, tmp_path):
        """#82: the dispatch cookie is signed with ``signing_key()``
        (token bytes + mtime), not the mtime-only rotation key — so
        replacing the token *content* invalidates it even when the
        file's mtime is preserved."""
        import os

        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        response = Response()
        write_dispatch_state(response, DispatchState(query="x"), tm)
        raw = response.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]

        st = tm.token_file.stat()
        tm.token_file.write_text("f" * 64)
        os.utime(tm.token_file, ns=(st.st_atime_ns, st.st_mtime_ns))

        state = read_dispatch_state(_mk_request(raw), tm)
        assert state == DispatchState()


class TestPruneFilters:
    """#80: filter values whose chip vanished from the available option
    set (repo removed from config, last matching issue closed) must be
    pruned so a stale cookie can't silently filter the list to empty."""

    OPTS = {
        "repos": [{"id": "a/x", "label": "x", "count": 2}],
        "efforts": [{"id": "s", "label": "S", "count": 2}],
        "fitnesses": [{"id": "solo", "label": "solo", "count": 2}],
        "statuses": [{"id": "todo", "label": "Todo", "count": 2}],
        "autopilots": [{"id": "on", "label": "⏵ Autopilot", "count": 1}],
    }

    def test_autopilot_chip_toggles_and_prunes(self):
        s = DispatchState()
        s.toggle_filter("autopilot", "on")
        assert s.autopilots == ["on"]
        assert s.prune_filters(self.OPTS) is False
        assert s.prune_filters({**self.OPTS, "autopilots": []}) is True
        assert s.autopilots == []

    def test_autopilot_chip_survives_the_cookie(self):
        s = DispatchState(autopilots=["on"])
        assert DispatchState.from_dict(s.to_dict()).autopilots == ["on"]

    def test_removes_stale_values_across_all_dimensions(self):
        s = DispatchState(
            repos=["a/x", "gone/y"],
            efforts=["s", "l"],
            statuses=["todo", "in progress"],
        )
        assert s.prune_filters(self.OPTS) is True
        assert s.repos == ["a/x"]
        assert s.efforts == ["s"]
        assert s.statuses == ["todo"]

    def test_returns_false_when_all_values_still_valid(self):
        s = DispatchState(repos=["a/x"], efforts=["s"], statuses=["todo"])
        assert s.prune_filters(self.OPTS) is False
        assert s.repos == ["a/x"]
        assert s.efforts == ["s"]
        assert s.statuses == ["todo"]

    def test_empty_state_is_untouched(self):
        s = DispatchState()
        assert s.prune_filters(self.OPTS) is False
        assert s == DispatchState()

    def test_query_and_selection_are_not_pruned(self):
        """Pruning is scoped to the chip dimensions — free-text query and
        selection keys have no chip whose disappearance could strand them."""
        s = DispatchState(repos=["gone/y"], query="hello", selected=["gone/y#1"])
        s.prune_filters(self.OPTS)
        assert s.query == "hello"
        assert s.selected == ["gone/y#1"]


class TestDimensionFilterState:
    """#247: effort/fitness chips replace the old priority dimension."""

    def test_defaults_empty(self):
        s = DispatchState()
        assert s.efforts == []
        assert s.fitnesses == []

    def test_toggle_effort_and_fitness(self):
        s = DispatchState()
        s.toggle_filter("effort", "s")
        s.toggle_filter("fitness", "solo")
        assert s.efforts == ["s"]
        assert s.fitnesses == ["solo"]
        s.toggle_filter("effort", "s")
        assert s.efforts == []

    def test_priority_dimension_is_gone(self):
        s = DispatchState()
        s.toggle_filter("priority", "need")  # unknown dimension → no-op
        assert not hasattr(s, "priorities")

    def test_legacy_cookie_with_priorities_decodes_silently(self):
        s = DispatchState.from_dict({"priorities": ["need"], "repos": ["a/x"]})
        assert s.repos == ["a/x"]
        assert not hasattr(s, "priorities")

    def test_round_trip_new_dims(self):
        s = DispatchState(efforts=["s", "none"], fitnesses=["solo"])
        assert DispatchState.from_dict(s.to_dict()) == s

    def test_prune_covers_new_dims(self):
        opts = {
            "repos": [{"id": "a/x", "label": "x", "count": 1}],
            "efforts": [{"id": "s", "label": "S", "count": 1}],
            "fitnesses": [{"id": "solo", "label": "solo", "count": 1}],
            "statuses": [{"id": "todo", "label": "Todo", "count": 1}],
        }
        s = DispatchState(efforts=["s", "l"], fitnesses=["solo", "pair"])
        assert s.prune_filters(opts) is True
        assert s.efforts == ["s"]
        assert s.fitnesses == ["solo"]


class TestSchemaValidation:
    """The cookie value is user-controllable — every shape mismatch must
    fall back to the empty state rather than blow up."""

    def test_payload_with_extra_fields_is_tolerated(self, tmp_path):
        """Schema migrations: an old client should still produce a usable
        state when the schema gains a new field. Forward-compat cuts the
        other way too — keep extra fields from breaking decode."""
        import base64
        import hashlib
        import hmac
        import json

        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        # Manually construct a payload with an unknown ``foo`` key.
        body = {
            "repos": ["a/x"],
            "priorities": [],
            "statuses": [],
            "query": "",
            "selected": [],
            "groups": [],
            "foo": "future",
        }
        payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode()
        sig = hmac.new(tm.signing_key(), payload.encode(), hashlib.sha256).hexdigest()
        request = _mk_request(f"{sig}.{payload}")
        state = read_dispatch_state(request, tm)
        assert state.repos == ["a/x"]

    def test_payload_with_wrong_field_types_falls_back(self, tmp_path):
        import base64
        import hashlib
        import hmac
        import json

        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        # ``repos`` should be ``list[str]``; pass an int instead.
        body = {"repos": 42}
        payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode()
        sig = hmac.new(tm.signing_key(), payload.encode(), hashlib.sha256).hexdigest()
        request = _mk_request(f"{sig}.{payload}")
        state = read_dispatch_state(request, tm)
        assert state == DispatchState()


class TestStateMutations:
    """``DispatchState`` exposes a few small helpers so route handlers
    don't have to hand-roll list mutation. Keeping these on the
    dataclass means the tests + routes share one definition of
    'toggle'/'group' semantics."""

    def test_toggle_filter_adds_then_removes(self):
        s = DispatchState()
        s.toggle_filter("effort", "s")
        assert s.efforts == ["s"]
        s.toggle_filter("effort", "s")
        assert s.efforts == []

    def test_toggle_filter_with_multiple_values(self):
        s = DispatchState()
        s.toggle_filter("effort", "s")
        s.toggle_filter("effort", "m")
        assert sorted(s.efforts) == ["m", "s"]

    def test_toggle_filter_for_repos_and_statuses(self):
        s = DispatchState()
        s.toggle_filter("repo", "a/x")
        s.toggle_filter("status", "todo")
        assert s.repos == ["a/x"]
        assert s.statuses == ["todo"]

    def test_toggle_filter_unknown_dimension_is_noop(self):
        s = DispatchState()
        s.toggle_filter("garbage", "x")
        assert s == DispatchState()

    def test_toggle_filter_sentinel_dimension(self):
        # #235 follow-up: include/exclude machine-filed issues.
        s = DispatchState()
        s.toggle_filter("sentinel", "sentinel")
        assert s.sentinels == ["sentinel"]
        s.toggle_filter("sentinel", "other")
        assert sorted(s.sentinels) == ["other", "sentinel"]
        s.toggle_filter("sentinel", "sentinel")
        assert s.sentinels == ["other"]

    def test_sentinels_round_trip_and_prune(self):
        s = DispatchState(sentinels=["sentinel", "stale"])
        restored = DispatchState.from_dict(s.to_dict())
        assert restored.sentinels == ["sentinel", "stale"]
        changed = restored.prune_filters(
            {
                "repos": [],
                "priorities": [],
                "statuses": [],
                "sentinels": [{"id": "sentinel", "label": "⚑ Sentinel", "count": 1}],
            }
        )
        assert changed is True
        assert restored.sentinels == ["sentinel"]

    def test_toggle_selection(self):
        s = DispatchState()
        s.toggle_selection("a/x#1")
        assert s.selected == ["a/x#1"]
        s.toggle_selection("a/x#1")
        assert s.selected == []

    def test_deselect_also_removes_from_groups(self):
        """The TUI's ``toggle_selection`` (dispatch_screen.py:455) drops
        a key from any group it lives in when the selection is cleared.
        Mirror that here so the cookie can't end up with a 'phantom'
        grouped key the user never selected."""
        s = DispatchState(
            selected=["a/x#1", "a/x#2"],
            groups=[["a/x#1", "a/x#2"]],
        )
        s.toggle_selection("a/x#1")
        assert s.selected == ["a/x#2"]
        # ``a/x#1`` is gone from the group; the group survives but is
        # now down to a single key.
        assert s.groups == [["a/x#2"]]

    def test_group_selected_keys(self):
        s = DispatchState(selected=["a/x#1", "a/x#2"])
        s.group_selected(["a/x#1", "a/x#2"])
        assert s.groups == [["a/x#1", "a/x#2"]]

    def test_ungroup_removes_indexed_group(self):
        s = DispatchState(
            groups=[["a/x#1", "a/x#2"], ["b/y#3", "b/y#4"]],
        )
        s.ungroup(0)
        assert s.groups == [["b/y#3", "b/y#4"]]

    def test_ungroup_invalid_index_is_noop(self):
        s = DispatchState(groups=[["a/x#1"]])
        s.ungroup(99)
        assert s.groups == [["a/x#1"]]

    def test_clear_selection_drops_selected_and_groups(self):
        s = DispatchState(
            selected=["a/x#1", "a/x#2"],
            groups=[["a/x#1", "a/x#2"]],
        )
        s.clear_selection()
        assert s.selected == []
        assert s.groups == []
        # Filters are intentionally untouched: clear_selection is called
        # post-dispatch and the user probably wants to keep the same
        # filters in place to grab the next batch.

    def test_ungrouped_keys_helper(self):
        """``ungrouped_keys`` returns the selections not yet in any
        batch — used to enable / disable the Group button and to build
        the implicit "one worktree per ungrouped issue" batch list at
        dispatch time."""
        s = DispatchState(
            selected=["a/x#1", "a/x#2", "a/x#3"],
            groups=[["a/x#1", "a/x#2"]],
        )
        assert s.ungrouped_keys() == ["a/x#3"]


class TestNonAsciiCookie:
    """#170 audit AHQ-013: a non-ASCII sig reaching hmac.compare_digest raised
    TypeError, escaping the (ValueError, JSONDecodeError) fallback — /dispatch
    500'd on every load until the cookie was cleared."""

    def test_non_ascii_cookie_returns_empty_state(self, tmp_path):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        state = read_dispatch_state(_mk_request("\xe9abc.payload"), tm)
        assert state.to_dict() == DispatchState().to_dict()
