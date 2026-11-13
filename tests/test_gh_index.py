"""Tests for the durable REST issue index (#255)."""

import json

from agent_hq.gh_index import IssueIndex


def _row(number, state="open", created="2026-01-01T00:00:00Z", updated="2026-01-02T00:00:00Z"):
    return {"number": number, "state": state, "created_at": created, "updated_at": updated}


def test_merge_then_read_timestamps(tmp_path):
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge("o/r", [_row(1)], seeded=True, synced_at=100.0)
    assert idx.timestamps_for("o/r") == {
        1: {"created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z"}
    }


def test_closed_keys_are_derived_from_state(tmp_path):
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge("o/r", [_row(1), _row(2, state="closed")], seeded=True, synced_at=100.0)
    assert idx.closed_keys_for("o/r") == {"o/r#2"}


def test_merge_updates_an_existing_issue_state(tmp_path):
    """A close arrives as a fresh row for a number already in the index."""
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge("o/r", [_row(1)], seeded=True, synced_at=100.0)
    idx.merge(
        "o/r",
        [_row(1, state="closed", updated="2026-02-01T00:00:00Z")],
        seeded=True,
        synced_at=200.0,
    )
    assert idx.closed_keys_for("o/r") == {"o/r#1"}
    assert idx.cursor_for("o/r") == "2026-02-01T00:00:00Z"


def test_reopening_clears_the_closed_key(tmp_path):
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge("o/r", [_row(1, state="closed")], seeded=True, synced_at=100.0)
    idx.merge(
        "o/r",
        [_row(1, state="open", updated="2026-03-01T00:00:00Z")],
        seeded=True,
        synced_at=200.0,
    )
    assert idx.closed_keys_for("o/r") == set()


def test_cursor_is_the_max_updated_at(tmp_path):
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge(
        "o/r",
        [_row(1, updated="2026-01-05T00:00:00Z"), _row(2, updated="2026-01-03T00:00:00Z")],
        seeded=True,
        synced_at=100.0,
    )
    assert idx.cursor_for("o/r") == "2026-01-05T00:00:00Z"


def test_empty_merge_does_not_rewind_the_cursor(tmp_path):
    """An incremental sync returning nothing means nothing changed."""
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge("o/r", [_row(1, updated="2026-01-05T00:00:00Z")], seeded=True, synced_at=100.0)
    idx.merge("o/r", [], seeded=True, synced_at=200.0)
    assert idx.cursor_for("o/r") == "2026-01-05T00:00:00Z"
    assert idx.synced_at("o/r") == 200.0


def test_repos_are_independent(tmp_path):
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge("o/a", [_row(1, state="closed")], seeded=True, synced_at=100.0)
    idx.merge("o/b", [_row(1)], seeded=True, synced_at=100.0)
    assert idx.closed_keys_for("o/a") == {"o/a#1"}
    assert idx.closed_keys_for("o/b") == set()


def test_state_survives_a_reload_from_disk(tmp_path):
    path = tmp_path / "index.json"
    IssueIndex(path=path).merge("o/r", [_row(7, state="closed")], seeded=True, synced_at=100.0)
    assert IssueIndex(path=path).closed_keys_for("o/r") == {"o/r#7"}


def test_second_instance_sees_the_first_instances_write(tmp_path):
    """The file is the coordination point across processes, not a seed.

    This is the bug at the heart of #255, in miniature: GitHubCache loaded
    from disk exactly once, so a long-lived process never saw a peer's work.
    """
    path = tmp_path / "index.json"
    a, b = IssueIndex(path=path), IssueIndex(path=path)
    assert b.closed_keys_for("o/r") == set()
    a.merge("o/r", [_row(3, state="closed")], seeded=True, synced_at=100.0)
    assert b.closed_keys_for("o/r") == {"o/r#3"}


def test_a_peers_write_to_another_repo_is_not_clobbered(tmp_path):
    """Whole-file writes must merge, not overwrite a peer's repo."""
    path = tmp_path / "index.json"
    a, b = IssueIndex(path=path), IssueIndex(path=path)
    a.merge("o/a", [_row(1)], seeded=True, synced_at=100.0)
    b.merge("o/b", [_row(2)], seeded=True, synced_at=100.0)
    fresh = IssueIndex(path=path)
    assert fresh.timestamps_for("o/a") and fresh.timestamps_for("o/b")


def test_mark_closed_hides_an_issue_before_the_next_sync(tmp_path):
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge("o/r", [_row(1)], seeded=True, synced_at=100.0)
    idx.mark_closed("o/r", 1)
    assert idx.closed_keys_for("o/r") == {"o/r#1"}


def test_mark_closed_works_for_an_unknown_issue(tmp_path):
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.mark_closed("o/r", 42)
    assert idx.closed_keys_for("o/r") == {"o/r#42"}


def test_mark_closed_preserves_existing_timestamps(tmp_path):
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge("o/r", [_row(1)], seeded=True, synced_at=100.0)
    idx.mark_closed("o/r", 1)
    assert idx.timestamps_for("o/r")[1]["created_at"] == "2026-01-01T00:00:00Z"


def test_unseeded_repo_reads_empty(tmp_path):
    idx = IssueIndex(path=tmp_path / "index.json")
    assert idx.is_seeded("o/r") is False
    assert idx.timestamps_for("o/r") == {}
    assert idx.closed_keys_for("o/r") == set()
    assert idx.cursor_for("o/r") is None
    assert idx.synced_at("o/r") is None


def test_corrupt_file_is_tolerated(tmp_path):
    path = tmp_path / "index.json"
    path.write_text("{not json")
    idx = IssueIndex(path=path)
    assert idx.closed_keys_for("o/r") == set()
    idx.merge("o/r", [_row(1)], seeded=True, synced_at=100.0)
    assert idx.timestamps_for("o/r")


def test_unknown_version_is_ignored(tmp_path):
    path = tmp_path / "index.json"
    path.write_text(
        json.dumps({"version": 99, "repos": {"o/r": {"issues": {"1": {"s": "closed"}}}}})
    )
    assert IssueIndex(path=path).closed_keys_for("o/r") == set()


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "index.json"
    idx = IssueIndex(path=path)
    idx.merge("o/r", [_row(1)], seeded=True, synced_at=100.0)
    assert json.loads(path.read_text())["version"] == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_rows_without_a_number_are_skipped(tmp_path):
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge(
        "o/r",
        [{"state": "open", "updated_at": "2026-01-02T00:00:00Z"}, _row(1)],
        seeded=True,
        synced_at=100.0,
    )
    assert set(idx.timestamps_for("o/r")) == {1}


# --- sync_repo: incremental REST sync (#255) ------------------------------

import subprocess  # noqa: E402

import pytest  # noqa: E402

from agent_hq import gh_index  # noqa: E402


class _FakeGh:
    """Serves canned REST pages and records the URLs requested."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    def __call__(self, argv, **kwargs):
        self.urls.append(argv[2])
        body = self.pages.pop(0) if self.pages else []
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")


def test_seed_paginates_until_a_short_page(tmp_path, monkeypatch):
    full = [_row(n) for n in range(1, 101)]
    fake = _FakeGh([full, [_row(101)]])
    monkeypatch.setattr(gh_index.gh_meter, "run_gh", fake)
    idx = IssueIndex(path=tmp_path / "index.json")
    gh_index.sync_repo("o/r", idx, now=1000.0)
    assert len(fake.urls) == 2
    assert idx.is_seeded("o/r") is True
    assert len(idx.timestamps_for("o/r")) == 101


def test_seed_omits_since(tmp_path, monkeypatch):
    fake = _FakeGh([[]])
    monkeypatch.setattr(gh_index.gh_meter, "run_gh", fake)
    gh_index.sync_repo("o/r", IssueIndex(path=tmp_path / "index.json"), now=1000.0)
    assert "since=" not in fake.urls[0]


def test_incremental_sync_sends_the_cursor(tmp_path, monkeypatch):
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge(
        "o/r",
        [_row(1, updated="2026-01-05T00:00:00Z")],
        seeded=True,
        synced_at=1000.0,
        full_synced_at=1000.0,
    )
    fake = _FakeGh([[]])
    monkeypatch.setattr(gh_index.gh_meter, "run_gh", fake)
    gh_index.sync_repo("o/r", idx, now=1100.0)
    assert "since=" in fake.urls[0]
    assert "2026-01-05" in fake.urls[0]


def test_incremental_sync_is_one_call_in_steady_state(tmp_path, monkeypatch):
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge("o/r", [_row(1)], seeded=True, synced_at=1000.0)
    fake = _FakeGh([[_row(1)]])
    monkeypatch.setattr(gh_index.gh_meter, "run_gh", fake)
    gh_index.sync_repo("o/r", idx, now=1100.0)
    assert len(fake.urls) == 1


def test_pull_requests_are_dropped(tmp_path, monkeypatch):
    pr = _row(9)
    pr["pull_request"] = {"url": "x"}
    fake = _FakeGh([[_row(1), pr]])
    monkeypatch.setattr(gh_index.gh_meter, "run_gh", fake)
    idx = IssueIndex(path=tmp_path / "index.json")
    gh_index.sync_repo("o/r", idx, now=1000.0)
    assert set(idx.timestamps_for("o/r")) == {1}


def test_seed_stops_at_the_page_cap_and_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(gh_index, "_MAX_PAGES", 3)
    full = [_row(n) for n in range(1, 101)]
    fake = _FakeGh([full, full, full, full])
    monkeypatch.setattr(gh_index.gh_meter, "run_gh", fake)
    with caplog.at_level("WARNING"):
        gh_index.sync_repo("o/r", IssueIndex(path=tmp_path / "index.json"), now=1000.0)
    assert len(fake.urls) == 3
    assert "truncated" in caplog.text.lower()


def test_stale_index_triggers_a_full_reseed(tmp_path, monkeypatch):
    """`since=` can never notice a transferred or deleted issue."""
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge(
        "o/r",
        [_row(1, updated="2026-01-05T00:00:00Z")],
        seeded=True,
        synced_at=1000.0,
        full_synced_at=1000.0,
    )
    fake = _FakeGh([[]])
    monkeypatch.setattr(gh_index.gh_meter, "run_gh", fake)
    gh_index.sync_repo("o/r", idx, now=1000.0 + gh_index.RESEED_AFTER_S + 1)
    assert "since=" not in fake.urls[0]


def test_reseed_clock_is_not_reset_by_incremental_syncs(tmp_path, monkeypatch):
    """The daily reseed must measure from the last FULL sync, not the last sync.

    Stamping one `synced_at` on every sync makes `now - last_full` ~0 forever,
    because a refresh cycle syncs every few minutes — the reseed would never
    fire in production while still passing a single-sync test.
    """
    idx = IssueIndex(path=tmp_path / "index.json")
    # A row on the seed so a cursor exists — without one `since=` is absent
    # whatever the reseed decision, and the assertion proves nothing.
    fake = _FakeGh([[_row(1)]])
    monkeypatch.setattr(gh_index.gh_meter, "run_gh", fake)

    # Seed, then sync incrementally — stopping short of the horizon, so any
    # reseed observed afterwards is the one under test rather than one the
    # loop itself tripped.
    gh_index.sync_repo("o/r", idx, now=1000.0)
    horizon = 1000.0 + gh_index.RESEED_AFTER_S
    now = 1000.0
    while now + 300.0 < horizon:
        now += 300.0
        gh_index.sync_repo("o/r", idx, now=now)

    fake.urls.clear()
    gh_index.sync_repo("o/r", idx, now=1000.0 + gh_index.RESEED_AFTER_S + 1)
    assert "since=" not in fake.urls[0], "reseed never fired — clock reset by incrementals"


def test_a_full_reseed_restarts_the_clock(tmp_path, monkeypatch):
    idx = IssueIndex(path=tmp_path / "index.json")
    fake = _FakeGh([[_row(1)]])
    monkeypatch.setattr(gh_index.gh_meter, "run_gh", fake)
    gh_index.sync_repo("o/r", idx, now=1000.0)  # seeds (full)
    fake.urls.clear()
    gh_index.sync_repo("o/r", idx, now=1000.0 + gh_index.RESEED_AFTER_S - 1)
    assert "since=" in fake.urls[0]


def test_fetch_failure_propagates_and_leaves_the_index_intact(tmp_path, monkeypatch):
    """Raw subprocess errors propagate for #254's _fetch_failure to map."""
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge("o/r", [_row(1)], seeded=True, synced_at=1000.0)

    def _boom(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr="API rate limit exceeded")

    monkeypatch.setattr(gh_index.gh_meter, "run_gh", _boom)
    with pytest.raises(subprocess.CalledProcessError):
        gh_index.sync_repo("o/r", idx, now=1100.0)
    assert idx.timestamps_for("o/r")  # preserved


def test_malformed_page_raises_with_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gh_index.gh_meter,
        "run_gh",
        lambda argv, **k: subprocess.CompletedProcess(
            argv, 0, stdout='{"message": "Not Found"}', stderr=""
        ),
    )
    with pytest.raises(gh_index.IndexSyncError) as caught:
        gh_index.sync_repo("o/r", IssueIndex(path=tmp_path / "index.json"), now=1000.0)
    assert "Not Found" in caught.value.stderr or "malformed" in caught.value.stderr.lower()


def test_sync_is_metered_as_rest(tmp_path, monkeypatch):
    from agent_hq import gh_meter

    monkeypatch.setattr(subprocess, "run", _FakeGh([[]]))
    gh_index.sync_repo("o/r", IssueIndex(path=tmp_path / "index.json"), now=1000.0)
    summary = gh_meter.summarize(hours=1)
    assert summary.by_api.get("rest") == 1
    assert "issue-index" in summary.by_kind


class TestFullReseedPurges:
    """The daily reseed's whole purpose is clearing rows GitHub dropped.

    A transferred or deleted issue is invisible to an incremental ``since=``
    sweep, so the reseed exists to notice. It merged additively, so the row
    (and its cumulative closed key) survived forever and the clock fired at a
    no-op (#255 review).
    """

    def test_complete_full_sweep_drops_vanished_rows(self, tmp_path):
        idx = IssueIndex(path=tmp_path / "index.json")
        idx.merge("o/r", [_row(1), _row(2)], seeded=True, synced_at=100.0)

        # Issue 2 was transferred away: a complete sweep simply omits it.
        idx.merge(
            "o/r",
            [_row(1)],
            seeded=True,
            synced_at=200.0,
            full_synced_at=200.0,
            replace=True,
        )

        assert set(idx.timestamps_for("o/r")) == {1}

    def test_closed_key_of_a_vanished_issue_is_dropped_too(self, tmp_path):
        idx = IssueIndex(path=tmp_path / "index.json")
        idx.merge("o/r", [_row(9, state="closed")], seeded=True, synced_at=100.0)
        assert idx.closed_keys_for("o/r") == {"o/r#9"}

        idx.merge("o/r", [], seeded=True, synced_at=200.0, full_synced_at=200.0, replace=True)
        assert idx.closed_keys_for("o/r") == set()

    def test_incremental_sync_never_purges(self, tmp_path):
        """`since=` returns only what changed — replacing would erase the rest."""
        idx = IssueIndex(path=tmp_path / "index.json")
        idx.merge("o/r", [_row(1), _row(2)], seeded=True, synced_at=100.0)
        idx.merge(
            "o/r",
            [_row(2, updated="2026-05-01T00:00:00Z")],
            seeded=True,
            synced_at=200.0,
        )
        assert set(idx.timestamps_for("o/r")) == {1, 2}


class TestOutOfOrderMergesDoNotRegress:
    """Two syncs racing must not resurrect stale state (#255 review).

    Without single-flighting, sync A can fetch issue X as open, stall, and
    then merge *after* sync B recorded X as closed at a later timestamp. The
    row was written unconditionally, so A's stale copy won while the cursor
    kept B's newer value — leaving nothing to repair it until the issue
    changed again or the daily reseed ran.
    """

    def test_older_row_does_not_overwrite_a_newer_one(self, tmp_path):
        idx = IssueIndex(path=tmp_path / "index.json")
        # B lands first: issue closed at a later timestamp.
        idx.merge(
            "o/r",
            [_row(1, state="closed", updated="2026-06-02T00:00:00Z")],
            seeded=True,
            synced_at=200.0,
        )
        # A, which read the issue earlier, arrives late with a stale row.
        idx.merge(
            "o/r",
            [_row(1, state="open", updated="2026-06-01T00:00:00Z")],
            seeded=True,
            synced_at=210.0,
        )
        assert idx.closed_keys_for("o/r") == {"o/r#1"}

    def test_newer_row_still_wins(self, tmp_path):
        idx = IssueIndex(path=tmp_path / "index.json")
        idx.merge("o/r", [_row(1, updated="2026-06-01T00:00:00Z")], seeded=True, synced_at=100.0)
        idx.merge(
            "o/r",
            [_row(1, state="closed", updated="2026-06-02T00:00:00Z")],
            seeded=True,
            synced_at=200.0,
        )
        assert idx.closed_keys_for("o/r") == {"o/r#1"}


class TestReloadHoldsTheInstanceLock:
    """A reload that pauses between reading the file and installing it must
    not let a merge on the same instance slip in between (#255 review).

    The interleaving: reader R parses version V1 and is preempted; writer A
    merges repo "a" (V2 on disk, ``_mtime`` = V2); writer B's in-lock reload
    sees V2 already loaded and skips; R installs V1; B writes repo "b" from
    that V1 snapshot and A's repo is gone from disk. Holding the instance
    lock across stat + read + install makes the preemption impossible.
    """

    def test_a_merge_waits_for_an_in_flight_reload(self, tmp_path, monkeypatch):
        import threading

        from agent_hq import gh_index

        path = tmp_path / "issue-index.json"
        IssueIndex(path=path).merge("seed/repo", [], seeded=True, synced_at=1.0)
        index = IssueIndex(path=path)
        # Force the next reload to actually read the file.
        index._mtime = None

        paused = threading.Event()
        release = threading.Event()
        real_loads = gh_index.json.loads

        def _slow_loads(raw):
            data = real_loads(raw)
            if not paused.is_set():
                paused.set()
                release.wait(5)
            return data

        monkeypatch.setattr(gh_index.json, "loads", _slow_loads)

        reader = threading.Thread(target=index._entry, args=("seed/repo",))
        reader.start()
        assert paused.wait(5)

        merged = threading.Event()

        def _merge():
            index.merge("other/repo", [_row(1)], seeded=True, synced_at=2.0)
            merged.set()

        writer = threading.Thread(target=_merge)
        writer.start()
        assert not merged.wait(0.3), "merge ran while a reload was mid-flight"

        release.set()
        reader.join(5)
        writer.join(5)
        assert merged.is_set()
        assert index.is_seeded("other/repo")
        assert json.loads(path.read_text())["repos"].keys() >= {"seed/repo", "other/repo"}


class TestSweepOrderAndCursor:
    """Sweeps page newest-first and the cursor covers everything seen."""

    def test_pages_are_fetched_newest_first(self, tmp_path, monkeypatch):
        """Numbered pages over a mutable ``updated`` order skip rows when a
        row is updated mid-sweep and moves behind the page boundary. Paging
        newest-first turns that skip into a harmless duplicate, and a
        truncated seed then keeps the *recent* rows a board actually shows
        instead of the 3,000 oldest (#255 review)."""
        fake = _FakeGh([[_row(1)]])
        monkeypatch.setattr(gh_index.gh_meter, "run_gh", fake)
        gh_index.sync_repo("o/r", IssueIndex(path=tmp_path / "i.json"))
        assert "direction=desc" in fake.urls[0]

    def test_cursor_advances_over_pull_requests(self, tmp_path, monkeypatch):
        """A page made only of PRs has still been *seen*. Without advancing
        the cursor, a PR-heavy prefix re-seeds from scratch on every sync
        (#255 review)."""
        pr = _row(9, updated="2026-09-01T00:00:00Z")
        pr["pull_request"] = {"url": "x"}
        fake = _FakeGh([[pr]])
        monkeypatch.setattr(gh_index.gh_meter, "run_gh", fake)
        index = IssueIndex(path=tmp_path / "i.json")
        gh_index.sync_repo("o/r", index)
        assert index.timestamps_for("o/r") == {}
        assert index.cursor_for("o/r") == "2026-09-01T00:00:00Z"


class TestMarkClosedOutlivesAnInFlightSync:
    def test_an_older_open_row_cannot_reopen_a_marked_close(self, tmp_path):
        """A sync fetched the issue open, then the close landed and
        ``mark_closed`` recorded it; the sync's stale row merges last. The
        older-row guard only helps if the mark carries a newer stamp
        (#255 review)."""
        index = IssueIndex(path=tmp_path / "i.json")
        index.merge("o/r", [_row(1, updated="2026-09-01T00:00:00Z")], seeded=True, synced_at=1.0)
        index.mark_closed("o/r", 1)
        index.merge("o/r", [_row(1, updated="2026-09-01T00:00:00Z")], seeded=True, synced_at=2.0)
        assert index.closed_keys_for("o/r") == {"o/r#1"}


def test_open_keys_are_derived_from_state(tmp_path):
    """#273: the off-board detector diffs the index's OPEN rows against the board."""
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge("o/r", [_row(1), _row(2, state="closed"), _row(3)], seeded=True, synced_at=100.0)
    assert idx.open_keys_for("o/r") == {"o/r#1", "o/r#3"}
    assert idx.open_keys_for("o/other") == set()


def test_open_keys_skip_a_malformed_row_key(tmp_path):
    """#273 review round 6: timestamps_for tolerates a non-numeric key in a
    hand-editable index file; the open-keys read must too, or the detector
    skips the whole repo every sweep."""
    idx = IssueIndex(path=tmp_path / "index.json")
    idx.merge("o/r", [_row(1)], seeded=True, synced_at=100.0)
    idx._repos["o/r"]["issues"]["junk"] = {"c": None, "u": None, "s": "open"}
    assert idx.open_keys_for("o/r") == {"o/r#1"}
