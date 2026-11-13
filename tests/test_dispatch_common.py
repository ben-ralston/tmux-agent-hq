"""Tests for the shared dispatch-screen helpers (#64)."""

from __future__ import annotations

from agent_hq.screens._dispatch_common import (
    DispatchGuard,
    GgChordTracker,
    SelectionState,
    failed_unit_keys,
)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class TestGgChordTracker:
    def test_single_g_does_not_fire(self):
        clock = FakeClock()
        tracker = GgChordTracker(clock=clock)
        assert tracker.saw_g() is False

    def test_second_g_within_timeout_fires(self):
        clock = FakeClock()
        tracker = GgChordTracker(clock=clock)
        tracker.saw_g()
        clock.advance(0.2)
        assert tracker.saw_g() is True

    def test_second_g_after_timeout_does_not_fire(self):
        clock = FakeClock()
        tracker = GgChordTracker(clock=clock)
        tracker.saw_g()
        clock.advance(0.5)
        assert tracker.saw_g() is False

    def test_chord_resets_after_firing(self):
        # g g g — the third press starts a new chord, it doesn't fire again.
        clock = FakeClock()
        tracker = GgChordTracker(clock=clock)
        tracker.saw_g()
        clock.advance(0.1)
        assert tracker.saw_g() is True
        clock.advance(0.1)
        assert tracker.saw_g() is False

    def test_custom_timeout(self):
        clock = FakeClock()
        tracker = GgChordTracker(timeout=1.0, clock=clock)
        tracker.saw_g()
        clock.advance(0.8)
        assert tracker.saw_g() is True

    def test_first_press_never_fires_even_at_clock_zero(self):
        # A monotonic clock that starts near 0 must not make the very first
        # g read as a completed chord.
        tracker = GgChordTracker(clock=FakeClock(0.0))
        assert tracker.saw_g() is False


class TestSelectionStateToggle:
    def test_toggle_selects(self):
        sel = SelectionState()
        sel.toggle("a")
        assert sel.selected == {"a"}

    def test_toggle_again_deselects(self):
        sel = SelectionState()
        sel.toggle("a")
        sel.toggle("a")
        assert sel.selected == set()

    def test_deselect_dissolves_batch_containing_key(self):
        sel = SelectionState()
        sel.toggle("a")
        sel.toggle("b")
        assert sel.group_ungrouped() == ""
        sel.toggle("a")
        assert sel.batches == []
        assert sel.selected == {"b"}


class TestSelectionStateGrouping:
    def test_group_two_ungrouped_selections(self):
        sel = SelectionState()
        sel.toggle("a")
        sel.toggle("b")
        assert sel.group_ungrouped() == ""
        assert sel.batches == [{"a", "b"}]

    def test_group_requires_two_ungrouped(self):
        sel = SelectionState()
        sel.toggle("a")
        error = sel.group_ungrouped()
        assert error == "Need at least 2 ungrouped selections to batch"
        assert sel.batches == []

    def test_group_skips_already_grouped_keys(self):
        sel = SelectionState()
        sel.toggle("a")
        sel.toggle("b")
        sel.group_ungrouped()
        sel.toggle("c")
        # Only "c" is ungrouped now — too few for a second batch.
        error = sel.group_ungrouped()
        assert error == "Need at least 2 ungrouped selections to batch"
        assert sel.batches == [{"a", "b"}]

    def test_validate_error_blocks_grouping(self):
        sel = SelectionState()
        sel.toggle("a")
        sel.toggle("b")
        error = sel.group_ungrouped(validate=lambda keys: "nope")
        assert error == "nope"
        assert sel.batches == []

    def test_validator_cannot_mutate_the_stored_batch(self):
        # The validator gets a copy — mutating or retaining it must not
        # affect the batch that gets stored.
        sel = SelectionState()
        sel.toggle("a")
        sel.toggle("b")
        retained: list[set[str]] = []

        def validate(keys: set[str]) -> str:
            retained.append(keys)
            keys.clear()
            return ""

        assert sel.group_ungrouped(validate=validate) == ""
        assert sel.batches == [{"a", "b"}]
        retained[0].add("z")
        assert sel.batches == [{"a", "b"}]

    def test_validate_receives_the_ungrouped_keys(self):
        sel = SelectionState()
        sel.toggle("a")
        sel.toggle("b")
        seen: list[set[str]] = []

        def validate(keys: set[str]) -> str:
            seen.append(set(keys))
            return ""

        assert sel.group_ungrouped(validate=validate) == ""
        assert seen == [{"a", "b"}]
        assert sel.batches == [{"a", "b"}]

    def test_ungroup_dissolves_batch_but_keeps_selection(self):
        sel = SelectionState()
        sel.toggle("a")
        sel.toggle("b")
        sel.group_ungrouped()
        sel.ungroup("a")
        assert sel.batches == []
        assert sel.selected == {"a", "b"}

    def test_ungrouped_selected(self):
        sel = SelectionState()
        sel.toggle("a")
        sel.toggle("b")
        sel.group_ungrouped()
        sel.toggle("c")
        assert sel.ungrouped_selected() == {"c"}


class TestSelectionStateRemoveRetainClear:
    def test_remove_drops_selection_and_dissolves_batch(self):
        sel = SelectionState()
        sel.toggle("a")
        sel.toggle("b")
        sel.group_ungrouped()
        sel.remove("a")
        assert sel.selected == {"b"}
        assert sel.batches == []

    def test_remove_unknown_key_is_noop(self):
        sel = SelectionState()
        sel.toggle("a")
        sel.remove("z")
        assert sel.selected == {"a"}

    def test_retain_prunes_missing_keys_from_selection_and_batches(self):
        # A cross-surface change (e.g. the issue closing elsewhere)
        # prunes the key from selection AND any batch, dropping emptied
        # batches (but keeping a pruned, still-populated batch).
        sel = SelectionState()
        for key in ("a", "b", "c"):
            sel.toggle(key)
        sel.group_ungrouped()
        sel.retain({"a", "b"})
        assert sel.selected == {"a", "b"}
        assert sel.batches == [{"a", "b"}]

    def test_retain_drops_emptied_batches(self):
        sel = SelectionState()
        sel.toggle("a")
        sel.toggle("b")
        sel.group_ungrouped()
        sel.retain(set())
        assert sel.selected == set()
        assert sel.batches == []

    def test_clear_empties_everything(self):
        sel = SelectionState()
        sel.toggle("a")
        sel.toggle("b")
        sel.group_ungrouped()
        sel.clear()
        assert sel.selected == set()
        assert sel.batches == []


def _selection(*keys: str, group: bool = False) -> SelectionState:
    sel = SelectionState()
    for key in keys:
        sel.toggle(key)
    if group:
        assert sel.group_ungrouped() == ""
    return sel


class TestDispatchGuardBegin:
    def test_begin_clears_selection_and_marks_active(self):
        sel = _selection("a", "b", group=True)
        guard = DispatchGuard()

        assert guard.begin(sel) is True

        assert guard.active is True
        assert sel.selected == set()
        assert sel.batches == []

    def test_second_begin_is_refused_and_changes_nothing(self):
        # The duplicate-agents footgun (#59): a second Enter must not
        # re-snapshot (which would clobber the in-flight snapshot) or
        # report success.
        sel = _selection("a", "b", group=True)
        guard = DispatchGuard()
        guard.begin(sel)
        sel.toggle("c")

        assert guard.begin(sel) is False

        assert guard.active is True
        assert sel.selected == {"c"}
        guard.finish(sel, restore_keys={"a", "b"})
        assert sel.selected == {"a", "b", "c"}

    def test_begin_snapshot_is_a_copy(self):
        # Mutating the selection after begin() must not reach the snapshot.
        sel = _selection("a", "b", group=True)
        guard = DispatchGuard()
        guard.begin(sel)
        sel.toggle("a")
        sel.toggle("z")

        guard.finish(sel, restore_keys={"a", "b"})
        assert sel.selected == {"a", "b", "z"}
        assert sel.batches == [{"a", "b"}]

    def test_begin_on_empty_selection_still_engages(self):
        sel = SelectionState()
        guard = DispatchGuard()
        assert guard.begin(sel) is True
        assert guard.active is True


class TestDispatchGuardFinish:
    def test_finish_without_restore_keys_clears_active_and_restores_nothing(self):
        sel = _selection("a", "b", group=True)
        guard = DispatchGuard()
        guard.begin(sel)

        guard.finish(sel)

        assert guard.active is False
        assert sel.selected == set()
        assert sel.batches == []

    def test_finish_with_empty_restore_keys_restores_nothing(self):
        sel = _selection("a", "b", group=True)
        guard = DispatchGuard()
        guard.begin(sel)

        guard.finish(sel, restore_keys=set())

        assert guard.active is False
        assert sel.selected == set()
        assert sel.batches == []

    def test_finish_restores_only_the_requested_keys(self):
        # Partial failure: only the failed batch comes back, so the
        # successfully-dispatched items can't be re-fired by a stray Enter.
        sel = _selection("a", "b", "c", "d")
        guard = DispatchGuard()
        guard.begin(sel)

        guard.finish(sel, restore_keys={"c", "d"})

        assert sel.selected == {"c", "d"}

    def test_finish_restores_a_batch_when_two_or_more_keys_survive(self):
        sel = _selection("a", "b", group=True)
        guard = DispatchGuard()
        guard.begin(sel)

        guard.finish(sel, restore_keys={"a", "b"})

        assert sel.selected == {"a", "b"}
        assert sel.batches == [{"a", "b"}]

    def test_finish_does_not_restore_a_batch_with_one_surviving_key(self):
        sel = _selection("a", "b", group=True)
        guard = DispatchGuard()
        guard.begin(sel)

        guard.finish(sel, restore_keys={"a"})

        assert sel.selected == {"a"}
        assert sel.batches == []

    def test_finish_does_not_reappend_a_batch_overlapping_a_current_batch(self):
        sel = _selection("a", "b", group=True)
        guard = DispatchGuard()
        guard.begin(sel)
        # User re-selected and re-grouped during the in-flight window.
        sel.toggle("a")
        sel.toggle("z")
        assert sel.group_ungrouped() == ""

        guard.finish(sel, restore_keys={"a", "b"})

        assert sel.batches == [{"a", "z"}]
        assert sel.selected == {"a", "b", "z"}

    def test_finish_unions_with_keys_selected_during_the_flight(self):
        sel = _selection("a", "b")
        guard = DispatchGuard()
        guard.begin(sel)
        sel.toggle("z")

        guard.finish(sel, restore_keys={"a", "b"})

        assert sel.selected == {"a", "b", "z"}

    def test_finish_ignores_restore_keys_that_were_never_snapshotted(self):
        sel = _selection("a")
        guard = DispatchGuard()
        guard.begin(sel)

        guard.finish(sel, restore_keys={"a", "ghost"})

        assert sel.selected == {"a"}

    def test_guard_can_be_reused_after_finish(self):
        sel = _selection("a")
        guard = DispatchGuard()
        guard.begin(sel)
        guard.finish(sel, restore_keys={"a"})

        assert guard.begin(sel) is True
        assert sel.selected == set()

    def test_finish_drops_the_snapshot_so_a_later_finish_restores_nothing(self):
        sel = _selection("a", "b", group=True)
        guard = DispatchGuard()
        guard.begin(sel)
        guard.finish(sel, restore_keys={"a", "b"})
        sel.clear()

        guard.finish(sel, restore_keys={"a", "b"})

        assert sel.selected == set()
        assert sel.batches == []

    def test_finish_without_begin_is_a_noop(self):
        sel = _selection("a")
        guard = DispatchGuard()

        guard.finish(sel, restore_keys={"a"})

        assert guard.active is False
        assert sel.selected == {"a"}


class TestDispatchGuardSnapshotKeys:
    def test_snapshot_keys_are_the_keys_begin_captured(self):
        sel = _selection("a", "b", group=True)
        guard = DispatchGuard()
        guard.begin(sel)

        assert guard.snapshot_keys() == {"a", "b"}

    def test_snapshot_keys_survive_a_key_vanishing_from_the_live_set(self):
        # The cancel path restores from this, NOT from batches rebuilt off
        # live data — otherwise a batch that lost a member gets dissolved.
        sel = _selection("a", "b", group=True)
        guard = DispatchGuard()
        guard.begin(sel)

        guard.finish(sel, restore_keys=guard.snapshot_keys())

        assert sel.selected == {"a", "b"}
        assert sel.batches == [{"a", "b"}]

    def test_snapshot_keys_is_empty_before_begin_and_after_finish(self):
        sel = _selection("a")
        guard = DispatchGuard()
        assert guard.snapshot_keys() == set()
        guard.begin(sel)
        guard.finish(sel)
        assert guard.snapshot_keys() == set()


class _Res:
    def __init__(self, success: bool, launched: bool = False) -> None:
        self.success = success
        self.launched = launched


class TestFailedUnitKeys:
    def test_collects_keys_of_failed_units_only(self):
        keys = failed_unit_keys(
            [["a"], ["b"]], [_Res(True), _Res(False)], keys_of=lambda unit: set(unit)
        )
        assert keys == {"b"}

    def test_collects_every_key_of_a_failed_unit(self):
        keys = failed_unit_keys([["a", "b"]], [_Res(False)], keys_of=lambda unit: set(unit))
        assert keys == {"a", "b"}

    def test_empty_units_are_skipped_to_stay_aligned_with_results(self):
        keys = failed_unit_keys(
            [[], ["a"], ["b"]],
            [_Res(True), _Res(False)],
            keys_of=lambda unit: set(unit),
        )
        assert keys == {"b"}

    def test_length_mismatch_restores_nothing(self):
        keys = failed_unit_keys([["a"], ["b"]], [_Res(False)], keys_of=lambda unit: set(unit))
        assert keys == set()

    def test_a_launched_failure_is_not_restored(self):
        # `success=False` does not prove nothing started: dispatch puts the
        # harness command on argv BEFORE waiting for readiness, so a
        # readiness timeout can still boot an agent. Re-selecting those
        # invites a duplicate worktree/pane on the next Enter.
        keys = failed_unit_keys(
            [["a"], ["b"]],
            [_Res(False, launched=True), _Res(False)],
            keys_of=lambda unit: set(unit),
        )
        assert keys == {"b"}

    def test_results_without_a_launched_attribute_are_treated_as_not_launched(self):
        class Bare:
            success = False

        keys = failed_unit_keys([["a"]], [Bare()], keys_of=lambda unit: set(unit))
        assert keys == {"a"}
