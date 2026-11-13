"""Small helpers for the Dispatch screen (and the Agents tab's table).

Deliberately tiny: only data-type-free UI/timing patterns live here (#64):
the ``gg`` chord, selection/batch state, and the separator-skipping
``SkipRowTable`` (#261, also used by the Agents tab).
Anything non-trivial stays inside each screen's module.
"""

from __future__ import annotations

import time
from typing import Callable

from textual.widgets import DataTable


class GgChordTracker:
    """Detect the vim ``gg`` chord: two ``g`` presses within ``timeout`` seconds.

    ``clock`` is injectable so tests don't sleep.
    """

    def __init__(self, timeout: float = 0.4, clock: Callable[[], float] = time.monotonic) -> None:
        self._timeout = timeout
        self._clock = clock
        self._last_g_time: float | None = None

    def saw_g(self) -> bool:
        """Record a ``g`` press; True when it completes the chord."""
        now = self._clock()
        if self._last_g_time is not None and now - self._last_g_time < self._timeout:
            self._last_g_time = None
            return True
        self._last_g_time = now
        return False


class SelectionState:
    """Selected keys + grouped batches, with the invariants both screens
    used to maintain by hand:

    - a key must be selected to be in any batch;
    - deselecting (or removing) a grouped key dissolves its whole batch;
    - only ungrouped selections can form a new batch, and it takes two.

    Pure Python on purpose — the web ``DispatchState`` keeps the same
    contract a third time (list-based for its JSON cookie), so this stays
    importable without Textual if that ever converges.
    """

    def __init__(self) -> None:
        self.selected: set[str] = set()
        self.batches: list[set[str]] = []

    def toggle(self, key: str) -> None:
        """Toggle selection; deselecting also dissolves the key's batch."""
        if key in self.selected:
            self.remove(key)
        else:
            self.selected.add(key)

    def remove(self, key: str) -> None:
        """Drop a key from the selection and dissolve any batch holding it."""
        self.selected.discard(key)
        self.ungroup(key)

    def ungroup(self, key: str) -> None:
        """Dissolve the batch containing ``key``. Selections remain."""
        self.batches = [batch for batch in self.batches if key not in batch]

    def ungrouped_selected(self) -> set[str]:
        """Selected keys not yet part of any batch."""
        grouped: set[str] = set()
        for batch in self.batches:
            grouped |= batch
        return self.selected - grouped

    def group_ungrouped(self, validate: Callable[[set[str]], str] | None = None) -> str:
        """Batch the currently-ungrouped selections together.

        ``validate`` lets a screen veto the batch (e.g. Dispatch's
        cross-repo check) by returning an error message for the candidate
        key set. Returns "" on success, or the error message on failure.
        """
        ungrouped = self.ungrouped_selected()
        if len(ungrouped) < 2:
            return "Need at least 2 ungrouped selections to batch"
        if validate is not None:
            # Hand the validator a copy so it can't mutate (or retain and
            # later mutate) the set that becomes the stored batch.
            error = validate(set(ungrouped))
            if error:
                return error
        self.batches.append(ungrouped)
        return ""

    def retain(self, allowed: set[str]) -> None:
        """Prune selection + batches down to ``allowed`` keys.

        Unlike deselection, pruning keeps a still-populated batch (minus
        the vanished keys) and drops only emptied ones — a cross-surface
        demote/dismiss shouldn't dissolve the rest of the user's batch.
        """
        self.selected &= allowed
        self.batches = [pruned for b in self.batches if (pruned := b & allowed)]

    def clear(self) -> None:
        """Drop all selections and batches (after a successful dispatch)."""
        self.selected.clear()
        self.batches.clear()


class DispatchGuard:
    """Single-flight guard + selection snapshot for a screen's dispatch flow (#59).

    Dispatch is slow (worktree creation, harness boot) and used to give no
    feedback at all: the ``*``/``G1`` markers stayed on screen for the whole
    worker window, so the table looked untouched and a second Enter fired a
    second batch of agents for the same items.

    ``begin`` snapshots the selection, clears it (the screen redraws with the
    markers gone) and latches ``active`` so re-entry can be refused.
    ``finish`` releases the latch and puts back **only** the keys the caller
    asks for — the failed batches. Restoring the whole snapshot would
    re-select items whose agents did launch, recreating the very footgun this
    closes.

    Pure Python on purpose (no Textual), same as ``SelectionState``.
    """

    def __init__(self) -> None:
        self.active = False
        self._snapshot: tuple[set[str], list[set[str]]] | None = None

    def begin(self, selection: SelectionState) -> bool:
        """Snapshot + clear ``selection`` and latch in-flight.

        Returns False, changing nothing, when a dispatch is already running.
        """
        if self.active:
            return False
        self._snapshot = (
            set(selection.selected),
            [set(batch) for batch in selection.batches],
        )
        selection.clear()
        self.active = True
        return True

    def snapshot_keys(self) -> set[str]:
        """Every key ``begin`` captured; empty before begin / after finish.

        The cancel path restores from this rather than from batches rebuilt
        off live data — a batch that lost a member to a background refresh
        would otherwise come back with too few keys and be dropped entirely,
        so merely cancelling a dialog would dissolve the user's grouping.
        """
        if self._snapshot is None:
            return set()
        return set(self._snapshot[0])

    def finish(self, selection: SelectionState, restore_keys: set[str] | None = None) -> None:
        """Release the latch, restoring only snapshot keys in ``restore_keys``.

        Restoration unions into the live selection rather than replacing it —
        anything the user picked during the in-flight window survives. A
        snapshot batch comes back only when at least two of its keys are
        restored and it doesn't overlap a batch the user has since formed.
        """
        if restore_keys and self._snapshot is not None:
            snapshot_selected, snapshot_batches = self._snapshot
            selection.selected |= snapshot_selected & restore_keys
            for batch in snapshot_batches:
                kept = batch & restore_keys
                if len(kept) < 2:
                    continue
                if any(kept & existing for existing in selection.batches):
                    continue
                selection.batches.append(kept)
        self._snapshot = None
        self.active = False


def failed_unit_keys(
    units: list,
    results: list,
    keys_of: Callable[[object], set[str]],
) -> set[str]:
    """Keys of dispatch units that failed *and* definitely never started (#59).

    The dispatch screen pairs its units (repo groups) with results by
    index, so a unit is identified by position. Empty units are skipped
    because the dispatcher skips them too, emitting no result.

    Two rules keep a restore from resurrecting live work:

    - **Length mismatch → restore nothing.** The pairing is untrustworthy,
      and a misaligned restore could re-select a unit that launched.
    - **``result.launched`` failures are excluded.** ``success=False`` is not
      proof that nothing started: dispatch puts the harness command on the
      pane's argv *before* waiting for readiness, so a readiness timeout can
      still boot an agent and create a worktree. Re-selecting those would
      hand the user a duplicate on the next Enter — the exact footgun
      ``DispatchGuard`` exists to close.
    """
    dispatched = [unit for unit in units if unit]
    if len(dispatched) != len(results):
        return set()

    failed: set[str] = set()
    for unit, result in zip(dispatched, results):
        if result.success or getattr(result, "launched", False):
            continue
        failed |= keys_of(unit)
    return failed


class SkipRowTable(DataTable):
    """DataTable whose native movement keys skip non-selectable rows (#261).

    Screens interleave separator/header rows (keyed ``None`` in their row-key
    list) with data rows. The screen-level ``j``/``k`` bindings always skipped
    them, but a focused DataTable's own ``up``/``down``/``pageup``/
    ``pagedown``/``ctrl+home``/``ctrl+end`` bindings win over the screen's,
    so the arrow keys used to strand the cursor on a blank row until the next
    refresh rebuilt the table. Overriding the actions here makes every
    movement key behave the same, and lets the screens' ``j``/``k`` handlers
    simply call the table action.

    ``is_data_row(row_index)`` returns True for a selectable row. It is read
    lazily on every move, so a closure over the screen's row-key list stays
    current across rebuilds.
    """

    def __init__(self, *args, is_data_row: Callable[[int], bool] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.is_data_row: Callable[[int], bool] = is_data_row or (lambda row: True)

    def skip_to_data_row(self, downward: bool = True) -> None:
        """Move the cursor off any non-selectable row.

        Tries the given direction first; on hitting a boundary while still on
        a non-selectable row, reverses — so the cursor never strands on a
        header (e.g. the leading phase/repo header at row 0) or a trailing
        separator. Bounded by row count; a table with no data row at all is
        left where it is.
        """
        for _ in range(self.row_count):
            if self.is_data_row(self.cursor_row):
                return
            if downward:
                if self.cursor_row < self.row_count - 1:
                    super().action_cursor_down()
                else:
                    downward = False  # hit the bottom; reverse
            else:
                if self.cursor_row > 0:
                    super().action_cursor_up()
                else:
                    downward = True  # hit the top; reverse

    # Native movement actions: raw move, then settle on a data row in the
    # direction of travel.

    def action_cursor_down(self) -> None:
        super().action_cursor_down()
        self.skip_to_data_row(downward=True)

    def action_cursor_up(self) -> None:
        super().action_cursor_up()
        self.skip_to_data_row(downward=False)

    def action_page_down(self) -> None:
        super().action_page_down()
        self.skip_to_data_row(downward=True)

    def action_page_up(self) -> None:
        super().action_page_up()
        self.skip_to_data_row(downward=False)

    def action_scroll_top(self) -> None:
        super().action_scroll_top()
        self.skip_to_data_row(downward=True)

    def action_scroll_bottom(self) -> None:
        super().action_scroll_bottom()
        self.skip_to_data_row(downward=False)
