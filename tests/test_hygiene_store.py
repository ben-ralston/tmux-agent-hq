# tests/test_hygiene_store.py
"""HygieneStore (#249): findings file, fingerprint suppression, lifecycle."""

import json

from agent_hq.hygiene_store import (
    DEFAULT_HYGIENE_THRESHOLDS,
    Finding,
    FindingStatus,
    FindingType,
    HygieneStore,
    finding_id,
    load_hygiene_thresholds,
)

A = "ben-ralston/agent-hq#267"
B = "ben-ralston/agent-hq#268"
NOW = "2026-09-12T10:00:00+00:00"
LATER = "2026-09-13T10:00:00+00:00"


def _dup(keep=A, close=B, reasoning="same item twice"):
    return Finding.new(
        FindingType.DUPLICATE,
        [keep, close],
        action={"kind": "close_as_duplicate", "close": close, "of": keep},
        reasoning=reasoning,
    )


def _sweep(store, findings, *, now=NOW, open_keys=(A, B), sweep_id="s1", model="opus"):
    return store.record_sweep(
        findings, now=now, sweep_id=sweep_id, model=model, open_keys=set(open_keys)
    )


def test_finding_id_is_type_plus_sorted_keys():
    assert finding_id(FindingType.DUPLICATE, [B, A]) == f"duplicate:{A}+{B}"
    assert finding_id(FindingType.CLUSTER, ["o/r#3", "o/r#1"]) == "cluster:o/r#1+o/r#3"


def test_finding_roundtrip_and_defensive_from_dict():
    f = _dup()
    d = f.to_dict()
    assert Finding.from_dict(d) == f
    assert Finding.from_dict({**d, "status": "bogus"}) is None
    assert Finding.from_dict({**d, "type": "bogus"}) is None
    assert Finding.from_dict({**d, "issue_keys": "not-a-list"}) is None


def test_record_sweep_inserts_and_stamps(tmp_path):
    store = HygieneStore(tmp_path)
    w = _sweep(store, [_dup()])
    assert (w.inserted, w.refreshed, w.suppressed, w.resolved) == (1, 0, 0, 0)
    got = store.get(finding_id(FindingType.DUPLICATE, [A, B]))
    assert got is not None and got.status is FindingStatus.OPEN
    assert (got.found_at, got.sweep_id, got.model) == (NOW, "s1", "opus")
    assert store.last_swept_at() == NOW
    assert store.open_count() == 1
    assert (tmp_path / "hygiene" / "findings.json").exists()


def test_record_sweep_refreshes_open_finding_in_place(tmp_path):
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup(reasoning="v1")])
    w = _sweep(store, [_dup(reasoning="v2")], now=LATER, sweep_id="s2")
    assert (w.inserted, w.refreshed) == (0, 1)
    got = store.all_findings()[0]
    assert got.reasoning == "v2" and got.found_at == LATER and got.sweep_id == "s2"


def test_dismissed_accepted_resolved_ids_are_suppressed_forever(tmp_path):
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    fid = finding_id(FindingType.DUPLICATE, [A, B])
    assert store.dismiss(fid, now=NOW) is True
    w = _sweep(store, [_dup(reasoning="again")], now=LATER)
    assert w.suppressed == 1 and w.inserted == 0
    got = store.get(fid)
    assert got.status is FindingStatus.DISMISSED and got.reasoning == "same item twice"
    assert got.decided_at == NOW
    assert store.open_count() == 0


def test_dismiss_revision_guard(tmp_path):
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    fid = finding_id(FindingType.DUPLICATE, [A, B])
    assert store.dismiss(fid, now=LATER, expected_found_at="stale") is False
    assert store.get(fid).status is FindingStatus.OPEN
    assert store.dismiss(fid, now=LATER, expected_found_at=NOW) is True
    assert store.dismiss("nope", now=LATER) is False


def test_record_sweep_resolves_when_every_issue_is_gone(tmp_path):
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    # Still both open → stays open. One of the pair gone → the duplicate's
    # premise is broken, so it resolves (review round: partial closure).
    w = _sweep(store, [], now=LATER, open_keys=(A, B))
    assert w.resolved == 0
    w = _sweep(store, [], now=LATER, open_keys=(A,))
    assert w.resolved == 1
    got = store.all_findings()[0]
    assert got.status is FindingStatus.RESOLVED and got.decided_at == LATER


def test_mark_accepted_and_mark_failed(tmp_path):
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    fid = finding_id(FindingType.DUPLICATE, [A, B])
    execution = {"ok": False, "steps": [{"label": "x", "ok": False, "detail": "boom"}], "at": NOW}
    assert store.mark_failed(fid, execution=execution) is True
    got = store.get(fid)
    assert got.status is FindingStatus.OPEN and got.execution == execution
    execution["ok"] = True
    assert store.mark_accepted(fid, now=LATER, execution=execution) is True
    got = store.get(fid)
    assert got.status is FindingStatus.ACCEPTED and got.decided_at == LATER
    assert store.mark_accepted(fid, now=LATER, execution=execution) is False  # not open


def test_stamp_swept_only_touches_the_timestamp(tmp_path):
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    store.stamp_swept(LATER)
    assert store.last_swept_at() == LATER
    assert store.open_count() == 1


def test_corrupt_file_reads_empty_and_is_never_clobbered(tmp_path):
    store = HygieneStore(tmp_path)
    path = tmp_path / "hygiene" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert store.all_findings() == [] and store.last_swept_at() is None
    assert store.dismiss("x", now=NOW) is False  # bool writers degrade, never overwrite
    assert path.read_text() == "{not json"
    path.write_text("[]")
    assert store.mark_failed("x", execution={}) is False
    assert path.read_text() == "[]"


def test_unparseable_entries_survive_writes(tmp_path):
    store = HygieneStore(tmp_path)
    path = tmp_path / "hygiene" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"last_swept_at": None, "findings": {"weird": {"type": "??"}}}))
    _sweep(store, [_dup()])
    data = json.loads(path.read_text())
    assert data["findings"]["weird"] == {"type": "??"}
    assert len(data["findings"]) == 2


def test_load_hygiene_thresholds_defaults_and_overrides(tmp_path):
    assert load_hygiene_thresholds(tmp_path) == DEFAULT_HYGIENE_THRESHOLDS
    (tmp_path / "config.json").write_text(json.dumps({"hygiene": {"stale_days": 30}}))
    assert load_hygiene_thresholds(tmp_path) == {"stale_days": 30, "stale_max_value": 2}
    (tmp_path / "config.json").write_text(json.dumps({"hygiene": {"stale_days": "x", "junk": 1}}))
    assert load_hygiene_thresholds(tmp_path) == DEFAULT_HYGIENE_THRESHOLDS
    (tmp_path / "config.json").write_text("null")
    assert load_hygiene_thresholds(tmp_path) == DEFAULT_HYGIENE_THRESHOLDS
    (tmp_path / "config.json").write_text("{bad")
    assert load_hygiene_thresholds(tmp_path) == DEFAULT_HYGIENE_THRESHOLDS
# ---- review round (2026-09-12): resolve scoping, premise-gone, claim, corrupt-file ----


def _stale(key):
    return Finding.new(
        FindingType.STALE,
        [key],
        action={"kind": "close_stale", "close": key, "last_activity": "2026-01-01", "value": 1},
        reasoning="old",
    )


def test_from_dict_rejects_malformed_action_or_keys():
    d = _dup().to_dict()
    assert Finding.from_dict({**d, "action": {"kind": "close_as_duplicate"}}) is None  # no of/close
    assert Finding.from_dict({**d, "action": "nope"}) is None
    assert Finding.from_dict({**d, "issue_keys": ["no-hash"]}) is None
    cluster = Finding.new(
        FindingType.CLUSTER,
        [A, B],
        action={"kind": "merge_cluster", "repo": "r", "umbrella_title": "t", "children": [A, B]},
        reasoning="x",
    ).to_dict()
    assert Finding.from_dict(cluster) is not None
    assert Finding.from_dict({**cluster, "action": {"kind": "merge_cluster", "repo": "r"}}) is None
    # Codex round 5: the VALUES are typed too — a null key would 500 the
    # card template and a non-int proposed value would reach the store.
    assert Finding.from_dict({**d, "action": {**d["action"], "close": None}}) is None
    assert Finding.from_dict({**d, "action": {**d["action"], "of": "no-hash"}}) is None
    bad_children = {**cluster["action"], "children": [A, 7]}
    assert Finding.from_dict({**cluster, "action": bad_children}) is None
    mismatch = Finding.new(
        FindingType.MISMATCH, [A],
        action={"kind": "bump_value", "key": A, "current": 2, "proposed": 4}, reasoning="x",
    ).to_dict()
    assert Finding.from_dict(mismatch) is not None
    m_action = mismatch["action"]
    assert Finding.from_dict({**mismatch, "action": {**m_action, "proposed": "4"}}) is None
    assert Finding.from_dict({**mismatch, "action": {**m_action, "current": "2"}}) is None


def test_record_sweep_never_resolves_findings_for_repos_not_in_the_sweep(tmp_path):
    """Review P1: a partial/empty issue list (cold cache, failed fetch) must not
    resolve — and thereby permanently suppress — findings for repos it never saw."""
    store = HygieneStore(tmp_path)
    other = "ben-ralston/other#5"
    _sweep(store, [_dup(), _stale(other)], open_keys=(A, B, other))
    w = _sweep(store, [], now=LATER, open_keys=("ben-ralston/other#9",))  # other repo only
    assert w.resolved == 1  # other#5 is gone from a swept repo → resolved
    assert store.get(f"duplicate:{A}+{B}").status is FindingStatus.OPEN  # agent-hq not swept
    w = _sweep(store, [], now=LATER, open_keys=("ben-ralston/agent-hq#999",))
    assert w.resolved == 1  # now agent-hq was swept and the pair is gone
    assert store.open_count() == 0


def test_record_sweep_resolves_when_the_premise_issue_is_gone(tmp_path):
    """Review: a duplicate whose `close` target was closed by hand is moot even
    though `keep` is still open; a cluster with any child gone likewise."""
    store = HygieneStore(tmp_path)
    cluster = Finding.new(
        FindingType.CLUSTER,
        ["ben-ralston/agent-hq#7", "ben-ralston/agent-hq#8"],
        action={
            "kind": "merge_cluster",
            "repo": "ben-ralston/agent-hq",
            "umbrella_title": "t",
            "children": ["ben-ralston/agent-hq#7", "ben-ralston/agent-hq#8"],
        },
        reasoning="x",
    )
    _sweep(
        store,
        [_dup(), cluster],
        open_keys=(A, B, "ben-ralston/agent-hq#7", "ben-ralston/agent-hq#8"),
    )
    w = _sweep(store, [], now=LATER, open_keys=(A, "ben-ralston/agent-hq#7"))
    assert w.resolved == 2  # the pair is moot; the cluster has one child left
    assert store.open_count() == 0


def test_claim_is_atomic_and_cleared_by_mark_calls(tmp_path):
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    fid = finding_id(FindingType.DUPLICATE, [A, B])
    assert store.claim(fid, now=LATER, expected_found_at="stale") is False
    assert store.claim(fid, now=LATER, expected_found_at=NOW) is True
    assert store.get(fid).execution["in_flight"] is True
    assert store.claim(fid, now=LATER) is False  # already in flight
    assert store.dismiss(fid, now=LATER) is False  # not while executing
    log = {"ok": False, "at": LATER, "steps": [{"label": "x", "ok": False, "detail": "d"}]}
    assert store.mark_failed(fid, execution=log) is True
    got = store.get(fid)
    assert got.status is FindingStatus.OPEN and not got.execution.get("in_flight")
    assert store.claim(fid, now=LATER) is True  # retry allowed after a failure
    assert store.mark_accepted(fid, now=LATER, execution={**log, "ok": True}) is True
    assert store.claim(fid, now=LATER) is False  # accepted → never again
    assert store.claim("nope", now=LATER) is False


def test_claim_keeps_the_prior_step_log(tmp_path):
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    fid = finding_id(FindingType.DUPLICATE, [A, B])
    store.mark_failed(fid, execution={"ok": False, "at": NOW, "steps": [], "umbrella": 900})
    assert store.claim(fid, now=LATER) is True
    assert store.get(fid).execution["umbrella"] == 900


def test_corrupt_file_bool_writers_return_false_but_sweep_writes_raise(tmp_path):
    store = HygieneStore(tmp_path)
    path = tmp_path / "hygiene" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert store.dismiss("x", now=NOW) is False
    assert store.claim("x", now=NOW) is False
    assert store.mark_failed("x", execution={}) is False
    import pytest

    with pytest.raises(OSError):
        _sweep(store, [])
    with pytest.raises(OSError):
        store.stamp_swept(NOW)
    assert path.read_text() == "{not json"


# ---- review round 2 (2026-09-12) ----


def _cluster(*keys, repo="ben-ralston/agent-hq"):
    return Finding.new(
        FindingType.CLUSTER,
        list(keys),
        action={
            "kind": "merge_cluster",
            "repo": repo,
            "umbrella_title": "t",
            "children": list(keys),
        },
        reasoning="x",
    )


def test_record_sweep_does_not_refresh_a_finding_in_flight(tmp_path):
    """A sweep landing mid-Accept must not rewrite the action being executed."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    fid = finding_id(FindingType.DUPLICATE, [A, B])
    assert store.claim(fid, now=LATER) is True
    w = _sweep(store, [_dup(keep=B, close=A, reasoning="flipped")], now=LATER)
    got = store.get(fid)
    assert w.refreshed == 0 and got.reasoning == "same item twice" and got.found_at == NOW
    assert got.action["close"] == B


def test_new_cluster_supersedes_an_open_overlapping_cluster(tmp_path):
    """A regrouped cluster (new id) resolves the older overlapping one instead
    of leaving two open findings for the same issues."""
    store = HygieneStore(tmp_path)
    k = [f"ben-ralston/agent-hq#{n}" for n in (7, 8, 9)]
    _sweep(store, [_cluster(k[0], k[1])], open_keys=k)
    w = _sweep(store, [_cluster(k[0], k[1], k[2])], now=LATER, open_keys=k)
    assert w.inserted == 1 and w.resolved == 1
    old = store.get(finding_id(FindingType.CLUSTER, k[:2]))
    assert old.status is FindingStatus.RESOLVED and old.decided_at == LATER
    assert store.open_count() == 1
    # A dismissed overlapping cluster stays dismissed (never re-opened, never counted).
    store.dismiss(finding_id(FindingType.CLUSTER, k), now=LATER)
    w = _sweep(store, [_cluster(k[1], k[2])], now=LATER, open_keys=k)
    assert w.inserted == 1 and w.resolved == 0


def test_snapshot_reads_the_file_once(tmp_path, monkeypatch):
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    reads = []
    orig = HygieneStore._read

    def counting(self):
        reads.append(1)
        return orig(self)

    monkeypatch.setattr(HygieneStore, "_read", counting)
    findings, swept = store.snapshot()
    assert len(reads) == 1 and len(findings) == 1 and swept == NOW


# ---- hand-test round (2026-09-12): cluster premise ----


def test_cluster_resolves_only_when_fewer_than_two_children_stay_open(tmp_path):
    """A cluster with one child closed by hand is still a cluster of the rest
    (hand-test: two of four closed by earlier duplicate accepts). It resolves
    once fewer than two children remain open."""
    store = HygieneStore(tmp_path)
    c7, c8, c9 = (f"ben-ralston/agent-hq#{n}" for n in (7, 8, 9))
    _sweep(store, [_cluster(c7, c8, c9)], open_keys=(c7, c8, c9))
    assert _sweep(store, [], now=LATER, open_keys=(c7, c8)).resolved == 0
    assert store.open_count() == 1
    assert _sweep(store, [], now=LATER, open_keys=(c7,)).resolved == 1
    assert store.open_count() == 0


def test_record_progress_only_while_in_flight_and_released_on_mark(tmp_path):
    """Hand-test round: Execute shows a live checklist, fed by the step log
    the route records on the finding while the claim is held."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    fid = f"duplicate:{A}+{B}"
    assert store.record_progress(fid, plan=["a", "b"], done=[]) is False  # not claimed
    assert store.claim(fid, now=NOW) is True
    assert store.record_progress(fid, plan=["a", "b"], done=[]) is True
    ex = store.get(fid).execution
    assert ex["plan"] == ["a", "b"] and ex["progress"] == [] and ex["in_flight"]
    done = [{"label": "a", "ok": True, "detail": ""}]
    assert store.record_progress(fid, plan=["a", "b"], done=done, facts={"umbrella": 9}) is True
    ex = store.get(fid).execution
    assert ex["progress"] == done and ex["umbrella"] == 9  # facts land immediately (round 4)
    store.mark_failed(fid, execution={"ok": False, "at": NOW, "steps": done, "plan": ["a", "b"],
                                      "progress": done})
    ex = store.get(fid).execution
    assert "progress" not in ex and "in_flight" not in ex
    assert ex["plan"] == ["a", "b"]  # kept for the final checklist (round 3)


# ---- hand-test round: editing a cluster ----


def test_remove_child_rewrites_the_cluster_and_marks_it_human_edited(tmp_path):
    store = HygieneStore(tmp_path)
    k = [f"ben-ralston/agent-hq#{n}" for n in (7, 8, 9)]
    _sweep(store, [_cluster(*k)], open_keys=k)
    fid = f"cluster:{k[0]}+{k[1]}+{k[2]}"
    assert store.remove_child(fid, k[2], now=LATER, expected_found_at=NOW) is True
    new_id = f"cluster:{k[0]}+{k[1]}"
    f = store.get(new_id)
    assert f.issue_keys == k[:2] and f.action["children"] == k[:2]
    assert f.edited_by == "human" and f.found_at == LATER  # new revision
    assert f.id == new_id  # round 3: re-keyed so Dismiss suppresses THIS grouping
    assert Finding.from_dict(f.to_dict()) == f
    # The AI's original grouping is what the user rejected: tombstoned.
    old = store.get(fid)
    assert old.status is FindingStatus.DISMISSED and old.decided_at == LATER
    assert _sweep(store, [_cluster(*k)], now=LATER, open_keys=k).suppressed == 1
    # Dismissing the edited cluster suppresses the 2-key grouping too.
    assert store.dismiss(new_id, now=LATER) is True
    assert _sweep(store, [_cluster(k[0], k[1])], now=LATER, open_keys=k).suppressed == 1


def test_remove_child_guards(tmp_path):
    store = HygieneStore(tmp_path)
    k = [f"ben-ralston/agent-hq#{n}" for n in (7, 8, 9)]
    _sweep(store, [_cluster(*k), _dup()], open_keys=k + [A, B])
    fid = f"cluster:{k[0]}+{k[1]}+{k[2]}"
    assert store.remove_child(fid, k[2], now=LATER, expected_found_at="stale") is False
    assert store.remove_child(fid, "ben-ralston/agent-hq#99", now=LATER) is False
    assert store.remove_child(f"duplicate:{A}+{B}", A, now=LATER) is False  # not a cluster
    assert store.claim(fid, now=LATER) is True
    assert store.remove_child(fid, k[2], now=LATER) is False  # in flight
    store.mark_failed(fid, execution={"ok": False, "at": LATER, "steps": []})
    assert store.remove_child(fid, k[2], now=LATER) is True
    new_id = f"cluster:{k[0]}+{k[1]}"
    assert store.remove_child(new_id, k[1], now=LATER) is False  # would leave one child
    assert store.get(new_id).issue_keys == k[:2]


def test_sweep_never_rewrites_or_supersedes_a_human_edited_cluster(tmp_path):
    store = HygieneStore(tmp_path)
    k = [f"ben-ralston/agent-hq#{n}" for n in (7, 8, 9)]
    _sweep(store, [_cluster(*k)], open_keys=k)
    fid = f"cluster:{k[0]}+{k[1]}+{k[2]}"
    store.remove_child(fid, k[2], now=LATER)
    fid = f"cluster:{k[0]}+{k[1]}"  # re-keyed (round 3)
    # The AI proposes the edited pair itself: left alone, not refreshed.
    w = _sweep(store, [_cluster(k[0], k[1])], now="2026-09-14T00:00:00+00:00", open_keys=k)
    assert w.refreshed == 0 and store.get(fid).reasoning == "x"
    # An overlapping regrouping would normally supersede the older cluster;
    # a human edit wins instead — the incoming one is dropped.
    other = _cluster(k[1], k[2])
    w = _sweep(store, [other], now="2026-09-14T00:00:00+00:00", open_keys=k)
    assert w.inserted == 0 and w.suppressed == 1
    assert store.get(other.id) is None
    assert store.get(fid).status is FindingStatus.OPEN
    # Premise-gone still applies to the edited children.
    w = _sweep(store, [], now="2026-09-15T00:00:00+00:00", open_keys=[k[0]])
    assert w.resolved == 1


# ---- Codex round 2 (2026-09-13): partial executions and stale revalidation ----


def _partial(fid, store):
    """Simulate a cluster whose umbrella landed before a later step failed."""
    assert store.claim(fid, now=LATER) is True
    store.mark_failed(fid, execution={"ok": False, "at": LATER, "umbrella": 900, "steps": []})


def test_partially_executed_cluster_survives_regrouping_and_premise_checks(tmp_path):
    store = HygieneStore(tmp_path)
    k = [f"ben-ralston/agent-hq#{n}" for n in (7, 8, 9)]
    _sweep(store, [_cluster(*k)], open_keys=k)
    fid = f"cluster:{k[0]}+{k[1]}+{k[2]}"
    _partial(fid, store)
    # #7 closed by the first attempt; the model regroups the remaining two.
    w = _sweep(store, [_cluster(k[1], k[2])], now="2026-09-14T00:00:00+00:00", open_keys=k[1:])
    assert w.inserted == 0 and w.suppressed == 1 and w.resolved == 0
    assert store.get(fid).status is FindingStatus.OPEN
    assert store.get(fid).execution["umbrella"] == 900
    # One child left: a fresh cluster would resolve, a retry with an umbrella
    # finishes it — so it stays until nothing is open.
    w = _sweep(store, [], now="2026-09-15T00:00:00+00:00", open_keys=[k[2]])
    assert w.resolved == 0
    w = _sweep(store, [], now="2026-09-16T00:00:00+00:00", open_keys=["ben-ralston/agent-hq#1"])
    assert w.resolved == 1


def _stale(key, value=1):
    return Finding.new(
        FindingType.STALE,
        [key],
        action={"kind": "close_stale", "close": key, "last_activity": "2026-01-01T00:00:00Z",
                "value": value},
        reasoning="old",
    )


def test_open_stale_finding_not_re_reported_by_a_sweep_resolves(tmp_path):
    """Stale detection is code and runs every sweep: an open stale finding
    the sweep did not re-report stopped qualifying (new activity, value
    raised) — resolve it rather than keep offering the close."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_stale(A), _dup()], open_keys=(A, B))
    w = _sweep(store, [_stale(A)], now=LATER, open_keys=(A, B))
    assert w.resolved == 0 and w.refreshed == 1
    w = _sweep(store, [], now=LATER, open_keys=(A, B))  # still open, not stale any more
    assert w.resolved == 1
    assert store.get(f"stale:{A}").status is FindingStatus.RESOLVED
    assert store.get(f"duplicate:{A}+{B}").status is FindingStatus.OPEN  # AI findings: never
    # Not judged for a repo the sweep did not see.
    _sweep(store, [_stale("o/r#1")], now=LATER, open_keys=("o/r#1",))
    w = _sweep(store, [], now=LATER, open_keys=(A, B))
    assert w.resolved == 0


# ---- review round 3 (2026-09-13): claim lease, plan kept in the released log ----


def test_expired_claim_is_treated_as_released(tmp_path):
    """Codex + built-in review: a process that died mid-Execute must not wedge
    the finding forever. A claim older than CLAIM_LEASE_S is released for
    claim / dismiss / remove / sweep, keeping the retry facts."""
    from agent_hq.hygiene_store import CLAIM_LEASE_S

    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    fid = f"duplicate:{A}+{B}"
    assert store.claim(fid, now=NOW) is True
    soon = "2026-09-12T10:10:00+00:00"
    assert store.claim(fid, now=soon) is False
    assert store.dismiss(fid, now=soon) is False
    later = "2026-09-13T10:00:00+00:00"
    assert CLAIM_LEASE_S < 86400
    # Sweep after expiry may refresh it again.
    assert _sweep(store, [_dup(reasoning="new")], now=later).refreshed == 1
    # And a new claim takes over, keeping facts from the dead attempt.
    store.mark_failed(fid, execution={"ok": False, "at": NOW, "umbrella": 7, "steps": []})
    assert store.claim(fid, now=NOW) is True
    assert store.claim(fid, now=later) is True
    assert store.get(fid).execution["umbrella"] == 7
    assert store.get(fid).execution["claimed_at"] == later
    findings, _ = store.snapshot(now="2026-09-14T10:00:00+00:00")
    assert not findings[0].execution.get("in_flight")  # rendered as idle
    findings, _ = store.snapshot(now=later)
    assert findings[0].execution.get("in_flight")


def test_released_log_keeps_the_plan(tmp_path):
    """Built-in review: the final checklist lists the steps that never ran,
    which needs ``plan`` to survive mark_failed."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    fid = f"duplicate:{A}+{B}"
    store.claim(fid, now=NOW)
    store.record_progress(fid, plan=["a", "b", "c"], done=[])
    steps = [{"label": "a", "ok": False, "detail": "x"}]
    store.mark_failed(fid, execution={"ok": False, "at": NOW, "steps": steps})
    ex = store.get(fid).execution
    assert ex["plan"] == ["a", "b", "c"] and "progress" not in ex and "in_flight" not in ex


def test_stale_finding_with_a_landed_comment_is_not_resolved_by_the_next_sweep(tmp_path):
    """Built-in review round 4: our own 'Closing as stale' comment moves
    updated_at, so detect_stale stops re-reporting the finding; the
    not-re-reported rule must not resolve a finding whose close is still
    pending, or the issue stays open carrying a closing comment forever."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_stale(A)], open_keys=(A, B))
    fid = f"stale:{A}"
    store.claim(fid, now=NOW)
    store.mark_failed(fid, execution={"ok": False, "at": NOW, "commented_at": NOW, "steps": []})
    w = _sweep(store, [], now=LATER, open_keys=(A, B))
    assert w.resolved == 0 and store.get(fid).status is FindingStatus.OPEN
    # Once the issue is actually closed the premise is gone as usual.
    assert _sweep(store, [], now=LATER, open_keys=(B,)).resolved == 1


def test_revive_goes_through_the_cluster_guards(tmp_path):
    """Built-in review round 4: a revived RESOLVED cluster must face the same
    overlap rules as an insert — it cannot sit beside a human-edited
    cluster, and it supersedes an older overlapping AI one."""
    store = HygieneStore(tmp_path)
    k = [f"ben-ralston/agent-hq#{n}" for n in (1, 2, 3, 4)]
    a = _cluster(k[0], k[1], k[2])
    _sweep(store, [a], open_keys=k)
    b = _cluster(*k)
    w = _sweep(store, [b], now=LATER, open_keys=k)  # regroup: A superseded
    assert w.resolved == 1 and store.get(a.id).status is FindingStatus.RESOLVED
    store.remove_child(b.id, k[2], now=LATER)  # B' = (1, 2, 4), human-edited
    w = _sweep(store, [a], now="2026-09-14T00:00:00+00:00", open_keys=k)  # A proposed again
    assert w.inserted == 0 and w.suppressed == 1
    assert store.get(a.id).status is FindingStatus.RESOLVED
    assert store.open_count() == 1  # only B'
    # Without the edit: reviving A supersedes the overlapping open AI cluster.
    store2 = HygieneStore(tmp_path / "2")
    _sweep(store2, [a], open_keys=k)
    _sweep(store2, [b], now=LATER, open_keys=k)
    w = _sweep(store2, [a], now="2026-09-14T00:00:00+00:00", open_keys=k)
    assert w.inserted == 1 and w.resolved == 1
    assert store2.get(a.id).status is FindingStatus.OPEN
    assert store2.get(b.id).status is FindingStatus.RESOLVED


def test_resolved_finding_is_revived_when_proposed_again(tmp_path):
    """Built-in review: an issue parked in the board's Done column looks
    closed to the sweep (the hub hides Done-but-open issues), which resolved
    the finding; moving it back must let the same proposal return. Dismissed
    and accepted stay suppressed."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_dup()])
    fid = f"duplicate:{A}+{B}"
    assert _sweep(store, [], now=LATER, open_keys=(A,)).resolved == 1
    w = _sweep(store, [_dup(reasoning="again")], now="2026-09-14T00:00:00+00:00")
    assert w.inserted == 1 and w.suppressed == 0
    f = store.get(fid)
    assert f.status is FindingStatus.OPEN and f.decided_at is None and f.reasoning == "again"
    store.dismiss(fid, now=LATER)
    w = _sweep(store, [_dup()], now="2026-09-15T00:00:00+00:00")
    assert w.suppressed == 1 and store.get(fid).status is FindingStatus.DISMISSED


# ---- #273: open issues missing from their project board ----

OFF = "ben-ralston/gadget#8"


def _offboard(key=OFF, title="log sentinel misfiles"):
    return Finding.new(
        FindingType.OFFBOARD,
        [key],
        action={
            "kind": "add_to_board",
            "key": key,
            "project_number": 13,
            "project_owner": "@me",
            "project_name": "Gadget",
            "title": title,
        },
        reasoning="Open in ben-ralston/gadget but not on the Gadget board.",
    )


def test_offboard_finding_roundtrips_and_rejects_a_malformed_action():
    f = _offboard()
    assert f.id == f"offboard:{OFF}"
    back = Finding.from_dict(f.to_dict())
    assert back is not None and back.type is FindingType.OFFBOARD
    assert back.action["project_number"] == 13 and back.action["title"] == "log sentinel misfiles"
    bad = f.to_dict()
    bad["action"] = {"kind": "add_to_board", "key": OFF, "project_owner": "@me"}  # no number
    assert Finding.from_dict(bad) is None
    bad["action"] = {"kind": "add_to_board", "key": OFF, "project_number": "13",
                     "project_owner": "@me"}
    assert Finding.from_dict(bad) is None


def test_offboard_finding_resolves_once_the_issue_is_on_the_board(tmp_path):
    """The premise is "not on the board": the key showing up in the sweep's
    board set (open_keys) means it landed — resolved, not refreshed."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_offboard()], open_keys=(A,))
    assert store.get(f"offboard:{OFF}").status is FindingStatus.OPEN
    w = _sweep(store, [], now=LATER, open_keys=(A, OFF))
    assert w.resolved == 1
    assert store.get(f"offboard:{OFF}").status is FindingStatus.RESOLVED


def test_offboard_finding_not_re_reported_resolves_only_for_swept_repos(tmp_path):
    """Detection is code and runs every sweep: an open off-board finding the
    detector did not re-report is closed or gone — but only when its repo was
    actually checked (``offboard_repos``); a sweep that skipped the repo (cold
    board cache, unseeded index) must leave it alone."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_offboard()], open_keys=(A,))
    w = store.record_sweep(
        [], now=LATER, sweep_id="s2", model="opus", open_keys={A}, offboard_repos=set()
    )
    assert w.resolved == 0
    w = store.record_sweep(
        [], now=LATER, sweep_id="s3", model="opus", open_keys={A},
        offboard_repos={"ben-ralston/gadget"},
    )
    assert w.resolved == 1
    assert store.get(f"offboard:{OFF}").status is FindingStatus.RESOLVED


def test_offboard_refresh_keeps_a_known_title_when_the_lookup_missed(tmp_path):
    """Review round 1: the title comes from a capped, failure-tolerant REST
    lookup — a sweep that misses it must not degrade the card."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_offboard(title="log sentinel misfiles")], open_keys=(A,))
    w = _sweep(store, [_offboard(title=None)], now=LATER, open_keys=(A,))
    assert w.refreshed == 1
    assert store.get(f"offboard:{OFF}").action["title"] == "log sentinel misfiles"
    w = _sweep(store, [_offboard(title="renamed")], now=LATER, open_keys=(A,))
    assert store.get(f"offboard:{OFF}").action["title"] == "renamed"


def test_partially_added_offboard_finding_survives_the_next_sweep(tmp_path):
    """Review round 2: item-add landed, the Todo write failed. The issue is
    on the board now, so both resolve rules would fire and take the failed
    log with them — the item would sit with no Status and nobody told.
    Like a cluster with an umbrella, a partial attempt keeps the card."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_offboard()], open_keys=(A,))
    fid = f"offboard:{OFF}"
    assert store.claim(fid, now=NOW)
    assert store.mark_failed(
        fid,
        execution={"ok": False, "at": NOW, "item_id": "PVTI_1",
                   "steps": [{"label": "add", "ok": True}, {"label": "todo", "ok": False}]},
    )
    w = store.record_sweep(
        [], now=LATER, sweep_id="s2", model=None, open_keys={A, OFF},
        offboard_repos={"ben-ralston/gadget"}, offboard_statusless={OFF},
    )
    assert w.resolved == 0
    f = store.get(fid)
    assert f.status is FindingStatus.OPEN and f.execution["item_id"] == "PVTI_1"
    # Round 3: once the item HAS a Status (set by hand, or the board fixed
    # and the retry landed elsewhere) the premise really is gone — resolve,
    # or the card is stuck forever with a non-actionable retry.
    w = store.record_sweep(
        [], now=LATER, sweep_id="s3", model=None, open_keys={A, OFF},
        offboard_repos={"ben-ralston/gadget"}, offboard_statusless=set(),
    )
    assert w.resolved == 1 and store.get(fid).status is FindingStatus.RESOLVED


def test_partially_added_offboard_finding_survives_a_sweep_that_skipped_its_board(tmp_path):
    """Review round 4: when the board refetch failed the detector skipped the
    repo, so its key is in neither ``statusless`` nor ``checked`` — but the
    last-good open set still lists the status-less item. Unchecked means
    unknown: keep the card."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_offboard()], open_keys=(A,))
    fid = f"offboard:{OFF}"
    assert store.claim(fid, now=NOW)
    assert store.mark_failed(
        fid, execution={"ok": False, "at": NOW, "item_id": "PVTI_1", "steps": []}
    )
    w = store.record_sweep(
        [], now=LATER, sweep_id="s2", model=None, open_keys={A, OFF},
        offboard_repos=set(), offboard_statusless=set(),
    )
    assert w.resolved == 0 and store.get(fid).status is FindingStatus.OPEN


def test_accepted_offboard_finding_revives_when_the_board_changes(tmp_path):
    """Review round 5: ACCEPTED is suppressed forever, but an off-board
    finding accepted against one board says nothing about a re-pointed
    ``project:`` — re-detected against a DIFFERENT board it comes back.
    Dismissed stays dismissed; the same board stays accepted."""
    store = HygieneStore(tmp_path)
    fid = f"offboard:{OFF}"
    _sweep(store, [_offboard()], open_keys=(A,))
    assert store.claim(fid, now=NOW)
    assert store.mark_accepted(fid, now=NOW, execution={"ok": True, "at": NOW, "steps": []})
    soon = "2026-09-12T10:05:00+00:00"  # inside the round-8 revive grace
    w = _sweep(store, [_offboard()], now=soon, open_keys=(A,))
    assert w.suppressed == 1 and store.get(fid).status is FindingStatus.ACCEPTED
    moved = _offboard()
    moved.action["project_number"] = 99
    w = _sweep(store, [moved], now=soon, open_keys=(A,))
    assert w.inserted == 1
    f = store.get(fid)
    assert f.status is FindingStatus.OPEN and f.action["project_number"] == 99


def test_offboard_finding_inserted_by_this_sweep_is_never_resolved_by_it(tmp_path):
    """Review round 6: the web runner keeps a stale SWR list when the forced
    refetch returns nothing, so open_keys can still list an issue the
    detector just found off the board — insert and resolve in one write
    would render nothing."""
    store = HygieneStore(tmp_path)
    w = _sweep(store, [_offboard()], open_keys=(A, OFF))
    assert w.inserted == 1 and w.resolved == 0
    assert store.get(f"offboard:{OFF}").status is FindingStatus.OPEN


def test_offboard_landed_is_judged_on_the_repos_own_board(tmp_path):
    """Review round 8: the merged open set spans every board; an issue on a
    DIFFERENT board has not landed. The sweep passes the target-board set."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_offboard()], open_keys=(A,))
    fid = f"offboard:{OFF}"
    w = store.record_sweep(
        [], now=LATER, sweep_id="s2", model=None, open_keys={A, OFF},
        offboard_repos=set(), offboard_board_keys=set(),
    )
    assert w.resolved == 0 and store.get(fid).status is FindingStatus.OPEN
    w = store.record_sweep(
        [], now=LATER, sweep_id="s3", model=None, open_keys={A},
        offboard_repos=set(), offboard_board_keys={OFF},
    )
    assert w.resolved == 1 and store.get(fid).status is FindingStatus.RESOLVED


def test_accepted_offboard_finding_revives_when_the_item_is_removed_again(tmp_path):
    """Review round 8: an add can be undone (item removed by hand). A
    re-report well after the decision is a recurred premise — revive; a
    re-report within the grace is the sweep racing the accept, suppress."""
    store = HygieneStore(tmp_path)
    fid = f"offboard:{OFF}"
    _sweep(store, [_offboard()], open_keys=(A,))
    assert store.claim(fid, now=NOW)
    assert store.mark_accepted(fid, now=NOW, execution={"ok": True, "at": NOW, "steps": []})
    soon = "2026-09-12T10:05:00+00:00"
    w = _sweep(store, [_offboard()], now=soon, open_keys=(A,))
    assert w.suppressed == 1 and store.get(fid).status is FindingStatus.ACCEPTED
    w = _sweep(store, [_offboard()], now=LATER, open_keys=(A,))
    assert w.inserted == 1 and store.get(fid).status is FindingStatus.OPEN


def test_partial_offboard_guard_matches_status_less_keys_case_insensitively(tmp_path):
    """Review round 10: finding keys carry the config's case, status-less
    keys GitHub's; the guard must not fall through on case alone."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_offboard()], open_keys=(A,))
    fid = f"offboard:{OFF}"
    assert store.claim(fid, now=NOW)
    assert store.mark_failed(
        fid, execution={"ok": False, "at": NOW, "item_id": "PVTI_1", "steps": []}
    )
    w = store.record_sweep(
        [], now=LATER, sweep_id="s2", model=None, open_keys={A},
        offboard_repos={"ben-ralston/gadget"}, offboard_statusless={OFF.upper()},
        offboard_board_keys={OFF.upper()},
    )
    assert w.resolved == 0 and store.get(fid).status is FindingStatus.OPEN


def test_offboard_repo_checks_match_case_insensitively(tmp_path):
    """Codex review: an empty board keeps the config's repo spelling, the
    first add switches the sweep to GitHub's — a resolve keyed on the
    checked repo must not miss the old spelling."""
    store = HygieneStore(tmp_path)
    _sweep(store, [_offboard()], open_keys=(A,))
    fid = f"offboard:{OFF}"
    w = store.record_sweep(
        [], now=LATER, sweep_id="s2", model=None, open_keys={A},
        offboard_repos={"Ben-Ralston/Gadget"}, offboard_board_keys=set(),
    )
    assert w.resolved == 1 and store.get(fid).status is FindingStatus.RESOLVED


def test_offboard_finding_identity_survives_a_repo_case_change(tmp_path):
    """Codex review: the same issue re-reported under GitHub's spelling is
    the SAME finding — refreshed in place, never a duplicate card; and a
    dismissed one stays dismissed under the new spelling."""
    store = HygieneStore(tmp_path)
    upper = "Ben-Ralston/Gadget#8"
    _sweep(store, [_offboard()], open_keys=(A,))
    w = store.record_sweep(
        [_offboard(key=upper)], now=LATER, sweep_id="s2", model=None, open_keys={A},
        offboard_repos={"Ben-Ralston/Gadget"}, offboard_board_keys=set(),
    )
    assert (w.inserted, w.refreshed, w.resolved) == (0, 1, 0)
    open_ids = [f.id for f in store.snapshot()[0] if f.status is FindingStatus.OPEN]
    assert open_ids == [f"offboard:{OFF}"]
    assert store.get(f"offboard:{OFF}").found_at == LATER
    assert store.dismiss(f"offboard:{OFF}", now=LATER, expected_found_at=LATER)
    w = store.record_sweep(
        [_offboard(key=upper)], now=LATER, sweep_id="s3", model=None, open_keys={A},
        offboard_repos={"Ben-Ralston/Gadget"}, offboard_board_keys=set(),
    )
    assert w.suppressed == 1 and w.inserted == 0
    assert [f.status for f in store.snapshot()[0]] == [FindingStatus.DISMISSED]
