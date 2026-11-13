"""Tests for agent_hq.triage_store — records, rank, disk store (#246)."""

import json

from agent_hq.triage_store import (
    DEFAULT_WEIGHTS,
    AgentFitness,
    Effort,
    TriageRecord,
    TriageState,
    TriageStore,
    load_rank_weights,
    rank_breakdown,
    rank_score,
)


def _record(**kw):
    base = dict(
        issue_key="ben-ralston/agent-hq#246",
        value=4,
        effort=Effort.M,
        agent_fitness=AgentFitness.SOLO,
        interest=False,
        state=TriageState.CONFIRMED,
        pin=False,
        provenance={"value": "human", "effort": "ai", "agent_fitness": "ai"},
        reasoning={"effort": "single module + tests"},
        proposed_at="2026-08-22T10:00:00Z",
        model="claude-sonnet-5",
        updated_at="2026-08-22T10:05:00Z",
        body_hash="abc123",
        tombstoned_at=None,
    )
    base.update(kw)
    return TriageRecord(**base)


def test_record_roundtrip():
    rec = _record()
    assert TriageRecord.from_dict(rec.to_dict()) == rec


def test_from_dict_tolerates_unknown_and_missing_fields():
    d = _record().to_dict()
    d["future_field"] = "ignored"
    del d["model"]
    rec = TriageRecord.from_dict(d)
    assert rec.model is None
    assert rec.value == 4


def test_untriaged_record_allows_none_dimensions():
    rec = TriageRecord.untriaged("ben-ralston/agent-hq#999", updated_at="2026-08-22T10:00:00Z")
    assert rec.value is None
    assert rec.effort is None
    assert rec.agent_fitness is None
    assert rec.state is TriageState.UNTRIAGED
    assert TriageRecord.from_dict(rec.to_dict()) == rec


def test_from_dict_rejects_garbage_enum_values():
    d = _record().to_dict()
    d["effort"] = "XXL"
    rec = TriageRecord.from_dict(d)
    assert rec.effort is None  # degrade the field, keep the record


# ---- Task 2: rank formula ----


def test_rank_score_formula():
    # value 4 * 2 + effort M (1) + solo (2) + no interest = 11
    assert rank_score(_record()) == 11


def test_rank_score_with_interest():
    assert rank_score(_record(interest=True)) == 13


def test_rank_score_none_when_dimensions_missing():
    assert rank_score(_record(effort=None)) is None
    assert rank_score(_record(value=None)) is None


def test_rank_score_none_when_tombstoned():
    assert rank_score(_record(tombstoned_at="2026-08-22T11:00:00Z")) is None


def test_rank_breakdown_is_legible_and_sums_to_score():
    rec = _record(interest=True)
    parts = rank_breakdown(rec)
    assert parts == [("V4", 8), ("M", 1), ("solo", 2), ("★", 2)]
    assert sum(p for _, p in parts) == rank_score(rec)


def test_load_rank_weights_merges_over_defaults(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"rank_weights": {"interest_bonus": 5}}))
    w = load_rank_weights(tmp_path)
    assert w["interest_bonus"] == 5
    assert w["value_multiplier"] == DEFAULT_WEIGHTS["value_multiplier"]


def test_load_rank_weights_tolerates_missing_and_corrupt(tmp_path):
    assert load_rank_weights(tmp_path) == DEFAULT_WEIGHTS
    (tmp_path / "config.json").write_text("{not json")
    assert load_rank_weights(tmp_path) == DEFAULT_WEIGHTS


# ---- Task 3: TriageStore disk store ----


def test_store_upsert_and_get_roundtrip(tmp_path):
    store = TriageStore(tmp_path)
    rec = _record()
    store.upsert(rec, actor="human")
    got = store.get("ben-ralston/agent-hq#246")
    assert got is not None and got.value == 4
    assert (tmp_path / "triage" / "ben-ralston__agent-hq.json").exists()


def test_store_get_missing_returns_none(tmp_path):
    assert TriageStore(tmp_path).get("x/y#1") is None


def _seed_mixed(store, record, provenance):
    """Write a PROPOSED record with mixed provenance straight to disk.

    Any human dimension edit CONFIRMS the record, and a confirmed record is now
    frozen against AI dimension writes entirely (review fix 1, see
    ``test_ai_upsert_cannot_overwrite_confirmed_dimensions``). The
    human-provenance guard in ``_merge`` is therefore only reachable by a
    record that carries human provenance while still PROPOSED — the shape a
    pre-#248 store file can hold — so seed that shape directly.
    """
    path = store._repo_file(record.issue_key.rsplit("#", 1)[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = record.to_dict()
    payload["provenance"] = dict(provenance)
    payload["state"] = TriageState.PROPOSED.value
    path.write_text(json.dumps({record.issue_key: payload}))


def test_ai_upsert_cannot_overwrite_human_dimensions(tmp_path):
    store = TriageStore(tmp_path)
    # human owns value only; effort/agent_fitness stay open for the worker
    _seed_mixed(store, _record(value=5, effort=None, agent_fitness=None), {"value": "human"})
    # a later AI run tries to change everything
    ai = _record(value=1, effort=Effort.L, interest=True, pin=True)
    store.upsert(ai, actor="ai")
    got = store.get(ai.issue_key)
    assert got.value == 5  # human value preserved
    assert got.effort == Effort.L  # ai may set/change ai-provenance dims
    assert got.interest is False  # ai never sets interest
    assert got.pin is False  # ai never sets pin
    assert got.provenance["value"] == "human"
    assert got.provenance["effort"] == "ai"


def test_partial_human_update_keeps_unsupplied_dimensions(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_record(), actor="ai")
    store.upsert(_record(value=2, effort=None, agent_fitness=None), actor="human")
    got = store.get(_record().issue_key)
    assert got.value == 2
    assert got.effort == Effort.M  # kept from the AI proposal
    assert got.agent_fitness == AgentFitness.SOLO
    assert got.provenance == {"value": "human", "effort": "ai", "agent_fitness": "ai"}


def test_human_upsert_sets_human_provenance(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_record(), actor="human")
    got = store.get(_record().issue_key)
    assert all(got.provenance[d] == "human" for d in ("value", "effort", "agent_fitness"))


def test_corrupt_file_never_clobbered_on_upsert(tmp_path):
    store = TriageStore(tmp_path)
    path = tmp_path / "triage" / "ben-ralston__agent-hq.json"
    path.parent.mkdir(parents=True)
    path.write_text("{corrupt")
    # must log + skip (returning None so callers can't report success), not raise
    assert store.upsert(_record(), actor="human") is None
    assert path.read_text() == "{corrupt"  # original data untouched


def test_corrupt_file_reads_as_empty(tmp_path):
    path = tmp_path / "triage" / "ben-ralston__agent-hq.json"
    path.parent.mkdir(parents=True)
    path.write_text("{corrupt")
    assert TriageStore(tmp_path).all_records() == []


def test_apply_open_issue_set_tombstones_and_revives(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_record(issue_key="ben-ralston/agent-hq#1"), actor="human")
    store.upsert(_record(issue_key="ben-ralston/agent-hq#2"), actor="human")
    changed = store.apply_open_issue_set("ben-ralston/agent-hq", {2}, now="2026-08-22T12:00:00Z")
    assert changed == 1
    assert store.get("ben-ralston/agent-hq#1").tombstoned_at == "2026-08-22T12:00:00Z"
    assert store.get("ben-ralston/agent-hq#2").tombstoned_at is None
    assert [r.issue_key for r in store.all_records()] == ["ben-ralston/agent-hq#2"]
    assert len(store.all_records(include_tombstoned=True)) == 2
    # reopened issue revives
    changed = store.apply_open_issue_set("ben-ralston/agent-hq", {1, 2}, now="2026-08-22T13:00:00Z")
    assert changed == 1
    assert store.get("ben-ralston/agent-hq#1").tombstoned_at is None


# ---- Review fixes (multi-review round 1) ----


def test_non_mapping_record_value_tolerated_and_preserved(tmp_path):
    # {"k": "oops"} must not crash readers, and must survive a write untouched.
    path = tmp_path / "triage" / "ben-ralston__agent-hq.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"ben-ralston/agent-hq#9": "oops", "ben-ralston/agent-hq#8": {}}))
    store = TriageStore(tmp_path)
    assert store.all_records() == []  # neither parses; no crash
    store.upsert(_record(), actor="human")
    data = json.loads(path.read_text())
    assert data["ben-ralston/agent-hq#9"] == "oops"  # preserved verbatim
    assert data["ben-ralston/agent-hq#8"] == {}  # preserved verbatim
    assert "ben-ralston/agent-hq#246" in data


def test_ai_upsert_cannot_downgrade_confirmed_or_revive_tombstone(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_record(), actor="human")
    store.confirm(_record().issue_key, now="c")  # dims + explicit confirm
    store.apply_open_issue_set("ben-ralston/agent-hq", set(), now="2026-08-23T10:00:00Z")
    ai = _record(value=1, state=TriageState.PROPOSED, tombstoned_at=None)
    store.upsert(ai, actor="ai")
    got = store.get(ai.issue_key)
    assert got.state is TriageState.CONFIRMED  # no silent downgrade
    assert got.tombstoned_at == "2026-08-23T10:00:00Z"  # no silent revival


def test_ai_first_upsert_is_proposed(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_record(state=TriageState.CONFIRMED), actor="ai")
    assert store.get(_record().issue_key).state is TriageState.PROPOSED


def test_partial_human_update_keeps_flags_and_metadata(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_record(), actor="ai")
    store.upsert(_record(interest=True, pin=True), actor="human")
    # the exact partial shape the UI will send: only a value correction
    store.upsert(
        TriageRecord(issue_key=_record().issue_key, value=5, updated_at="x"),
        actor="human",
    )
    got = store.get(_record().issue_key)
    assert got.value == 5
    assert got.interest is True  # not wiped by the partial update
    assert got.pin is True  # not wiped
    assert got.state is TriageState.PROPOSED  # human edits never confirm on their own
    assert got.body_hash == "abc123"  # Phase-3 staleness baseline kept
    assert got.model == "claude-sonnet-5"


def test_load_rank_weights_tolerates_non_dict_json(tmp_path):
    (tmp_path / "config.json").write_text("null")
    assert load_rank_weights(tmp_path) == DEFAULT_WEIGHTS
    (tmp_path / "config.json").write_text("[]")
    assert load_rank_weights(tmp_path) == DEFAULT_WEIGHTS


def test_load_rank_weights_rejects_non_int_values(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"rank_weights": {"effort_bonus": {"S": "two"}, "interest_bonus": True}})
    )
    w = load_rank_weights(tmp_path)
    assert w == DEFAULT_WEIGHTS  # string + bool values filtered out
    assert rank_score(_record()) == 11  # ranking still works


# ---- Review fixes (multi-review round 2) ----


def test_owner_qualified_filenames_prevent_shortname_collision(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_record(issue_key="ownerA/tools#1"), actor="human")
    store.upsert(_record(issue_key="ownerB/tools#1"), actor="human")
    assert (tmp_path / "triage" / "ownerA__tools.json").exists()
    assert (tmp_path / "triage" / "ownerB__tools.json").exists()
    assert store.get("ownerA/tools#1").issue_key == "ownerA/tools#1"
    assert len(store.all_records()) == 2


def test_ai_reasoning_never_replaces_human_dimension_reasoning(tmp_path):
    store = TriageStore(tmp_path)
    # Only value is human-owned; effort stays open (see _seed_mixed for why
    # this state is seeded rather than produced by a human upsert).
    _seed_mixed(
        store,
        _record(
            value=5,
            effort=None,
            agent_fitness=None,
            reasoning={"value": "human: critical automation"},
        ),
        {"value": "human"},
    )
    store.upsert(
        _record(value=1, reasoning={"value": "ai: low impact", "effort": "ai: small"}),
        actor="ai",
    )
    got = store.get(_record().issue_key)
    assert got.value == 5
    assert got.reasoning["value"] == "human: critical automation"  # not the AI's rebuttal
    assert got.reasoning["effort"] == "ai: small"  # ai-owned dim reasoning updates fine


def test_malformed_issue_key_does_not_crash_tombstone_pass(tmp_path):
    path = tmp_path / "triage" / "ben-ralston__agent-hq.json"
    path.parent.mkdir(parents=True)
    good = _record(issue_key="ben-ralston/agent-hq#1").to_dict()
    bad = _record(issue_key="garbage-no-hash").to_dict()
    path.write_text(json.dumps({"ben-ralston/agent-hq#1": good, "garbage-no-hash": bad}))
    store = TriageStore(tmp_path)
    changed = store.apply_open_issue_set("ben-ralston/agent-hq", set(), now="2026-08-23T12:00:00Z")
    assert changed == 1  # good record tombstoned; bad key skipped, no IndexError


def test_dims_free_ai_upsert_does_not_flip_untriaged_to_proposed(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(TriageRecord.untriaged(_record().issue_key, updated_at="t0"), actor="human")
    # metadata-only AI write (the Phase-3 body_hash refresh shape)
    store.upsert(
        TriageRecord(issue_key=_record().issue_key, body_hash="new", updated_at="t1"),
        actor="ai",
    )
    got = store.get(_record().issue_key)
    assert got.state is TriageState.UNTRIAGED  # no unrankable "proposal"
    assert got.body_hash == "new"


# ---- Codex review fixes (PR #252) ----


def test_from_dict_rejects_boolean_dimension_value():
    # isinstance(True, int) is True in Python — a bool must not rank as V1.
    d = _record().to_dict()
    d["value"] = True
    assert TriageRecord.from_dict(d).value is None


def test_string_reasoning_tolerated_and_preserved(tmp_path):
    # dict("a string") raises ValueError — must skip-and-preserve, not crash.
    path = tmp_path / "triage" / "ben-ralston__agent-hq.json"
    path.parent.mkdir(parents=True)
    entry = _record(issue_key="ben-ralston/agent-hq#7").to_dict()
    entry["reasoning"] = "not a dict"
    path.write_text(json.dumps({"ben-ralston/agent-hq#7": entry}))
    store = TriageStore(tmp_path)
    assert store.all_records() == []  # skipped, not raised
    store.upsert(_record(issue_key="ben-ralston/agent-hq#8"), actor="human")
    data = json.loads(path.read_text())
    assert data["ben-ralston/agent-hq#7"]["reasoning"] == "not a dict"  # verbatim
    assert "ben-ralston/agent-hq#8" in data


def _hint_record(**kw):
    kw.setdefault("priority_hint", "Need")
    return _record(**kw)


def test_priority_hint_roundtrip():
    rec = _hint_record()
    assert rec.priority_hint == "Need"
    assert TriageRecord.from_dict(rec.to_dict()) == rec


def test_from_dict_coerces_non_string_priority_hint_to_none():
    d = _hint_record().to_dict()
    d["priority_hint"] = 3
    assert TriageRecord.from_dict(d).priority_hint is None


def test_priority_hint_none_incoming_keeps_existing_for_both_actors(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_hint_record(), actor="human")
    store.upsert(
        TriageRecord(issue_key=_record().issue_key, value=5, updated_at="t1"),
        actor="human",
    )
    assert store.get(_record().issue_key).priority_hint == "Need"
    store.upsert(
        TriageRecord(issue_key=_record().issue_key, effort=Effort.S, updated_at="t2"),
        actor="ai",
    )
    assert store.get(_record().issue_key).priority_hint == "Need"


def test_priority_hint_incoming_value_sets(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_hint_record(), actor="human")
    store.upsert(
        TriageRecord(issue_key=_record().issue_key, priority_hint="Want", updated_at="t1"),
        actor="ai",
    )
    assert store.get(_record().issue_key).priority_hint == "Want"


# ---- Phase 2 (#247): seed_missing ----


def test_seed_missing_creates_untriaged_records_with_hints(tmp_path):
    store = TriageStore(tmp_path)
    created = store.seed_missing(
        "ben-ralston/agent-hq", [(1, "Need"), (2, None)], now="2026-08-23T10:00:00Z"
    )
    assert created == 2
    one = store.get("ben-ralston/agent-hq#1")
    assert one.state is TriageState.UNTRIAGED
    assert one.priority_hint == "Need"
    assert one.value is None and one.effort is None and one.agent_fitness is None
    assert one.updated_at == "2026-08-23T10:00:00Z"
    assert store.get("ben-ralston/agent-hq#2").priority_hint is None


def test_seed_missing_never_touches_existing_records(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_record(issue_key="ben-ralston/agent-hq#1"), actor="human")
    store.apply_open_issue_set("ben-ralston/agent-hq", set(), now="t0")  # tombstone it
    created = store.seed_missing("ben-ralston/agent-hq", [(1, "Idea"), (2, "Want")], now="t1")
    assert created == 1
    one = store.get("ben-ralston/agent-hq#1")
    assert one.value == 4  # untouched
    assert one.priority_hint is None  # hint not stamped onto an existing record
    assert one.tombstoned_at == "t0"  # not revived
    assert store.get("ben-ralston/agent-hq#2").priority_hint == "Want"


def test_seed_missing_idempotent(tmp_path):
    store = TriageStore(tmp_path)
    assert store.seed_missing("o/r", [(1, "Need")], now="t0") == 1
    assert store.seed_missing("o/r", [(1, "Need")], now="t1") == 0
    assert store.get("o/r#1").updated_at == "t0"


def test_seed_missing_skips_corrupt_file(tmp_path):
    path = tmp_path / "triage" / "o__r.json"
    path.parent.mkdir(parents=True)
    path.write_text("{corrupt")
    store = TriageStore(tmp_path)
    assert store.seed_missing("o/r", [(1, "Need")], now="t0") == 0
    assert path.read_text() == "{corrupt"


def test_non_object_root_never_clobbered_on_upsert(tmp_path):
    # Valid JSON whose root isn't an object (e.g. a future list schema) is
    # corrupt to us — upsert must skip the write, not replace the file.
    body = '[{"issue_key": "ben-ralston/agent-hq#9"}]'
    path = tmp_path / "triage" / "ben-ralston__agent-hq.json"
    path.parent.mkdir(parents=True)
    path.write_text(body)
    store = TriageStore(tmp_path)
    assert store.upsert(_record(), actor="human") is None  # log + skip, not raise
    assert path.read_text() == body  # original data untouched
    assert store.all_records() == []  # read path still degrades to empty


# ---- Phase 3 (#248): confirm ----


def _proposed(store, number, repo="ben-ralston/agent-hq"):
    return store.upsert(
        TriageRecord(
            issue_key=f"{repo}#{number}",
            value=3,
            effort=Effort.M,
            agent_fitness=AgentFitness.PAIR,
            reasoning={"value": "r"},
            proposed_at="p",
            model="sonnet",
            body_hash="h",
            updated_at="t",
        ),
        actor="ai",
    )


def test_confirm_flips_proposed_keeping_ai_provenance(tmp_path):
    store = TriageStore(tmp_path)
    _proposed(store, 1)
    rec = store.confirm("ben-ralston/agent-hq#1", now="n")
    assert rec.state is TriageState.CONFIRMED and rec.updated_at == "n"
    assert rec.provenance == {"value": "ai", "effort": "ai", "agent_fitness": "ai"}
    assert rec.reasoning == {"value": "r"} and rec.model == "sonnet" and rec.body_hash == "h"
    assert store.get("ben-ralston/agent-hq#1").state is TriageState.CONFIRMED


def test_confirm_refuses_incomplete_missing_and_tombstoned(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(
        TriageRecord(issue_key="ben-ralston/agent-hq#2", value=3, updated_at="t"), actor="ai"
    )  # effort/fitness None
    assert store.confirm("ben-ralston/agent-hq#2", now="n") is None
    assert store.get("ben-ralston/agent-hq#2").state is TriageState.PROPOSED
    assert store.confirm("ben-ralston/agent-hq#404", now="n") is None
    _proposed(store, 3)
    store.apply_open_issue_set("ben-ralston/agent-hq", {2}, now="x")  # tombstones #3
    assert store.confirm("ben-ralston/agent-hq#3", now="n") is None


def test_confirm_is_idempotent(tmp_path):
    store = TriageStore(tmp_path)
    _proposed(store, 1)
    store.confirm("ben-ralston/agent-hq#1", now="n1")
    rec = store.confirm("ben-ralston/agent-hq#1", now="n2")
    assert rec.state is TriageState.CONFIRMED and rec.updated_at == "n1"


def test_confirm_many_counts_flips_across_repos(tmp_path):
    store = TriageStore(tmp_path)
    _proposed(store, 1)
    _proposed(store, 2)
    _proposed(store, 7, repo="o/other")
    store.confirm("ben-ralston/agent-hq#2", now="n0")
    n = store.confirm_many(
        [
            "ben-ralston/agent-hq#1",
            "ben-ralston/agent-hq#2",
            "o/other#7",
            "o/other#404",
        ],
        now="n",
    )
    assert n == 2
    assert all(
        store.get(k).state is TriageState.CONFIRMED for k in ("ben-ralston/agent-hq#1", "o/other#7")
    )


def test_ai_upsert_cannot_overwrite_confirmed_dimensions(tmp_path):
    """Review fix 1: ``confirm`` keeps provenance "ai", so the human-provenance
    guard in ``_merge`` does NOT protect a confirmed record. A worker run still
    in flight when the user hits Confirm would land afterwards and silently
    replace the dimensions the human just signed off on."""
    store = TriageStore(tmp_path)
    key = "ben-ralston/agent-hq#1"
    store.upsert(
        TriageRecord(
            issue_key=key,
            value=2,
            effort=Effort.S,
            agent_fitness=AgentFitness.SOLO,
            reasoning={"value": "original"},
            proposed_at="p1",
            model="sonnet",
            body_hash="h1",
            updated_at="t1",
        ),
        actor="ai",
    )
    store.confirm(key, now="n")
    store.upsert(
        TriageRecord(
            issue_key=key,
            value=5,
            effort=Effort.L,
            agent_fitness=AgentFitness.NEEDS_YOU,
            reasoning={"value": "rewritten", "effort": "rewritten"},
            proposed_at="p2",
            model="opus",
            body_hash="h2",
            updated_at="t2",
        ),
        actor="ai",
    )
    got = store.get(key)
    assert got.state is TriageState.CONFIRMED
    assert (got.value, got.effort, got.agent_fitness) == (2, Effort.S, AgentFitness.SOLO)
    assert got.reasoning == {"value": "original"}
    assert got.provenance == {"value": "ai", "effort": "ai", "agent_fitness": "ai"}


def test_confirm_skips_unreadable_file(tmp_path):
    store = TriageStore(tmp_path)
    _proposed(store, 1)
    path = tmp_path / "triage" / "ben-ralston__agent-hq.json"
    path.write_text("{not json")
    assert store.confirm("ben-ralston/agent-hq#1", now="n") is None
    assert path.read_text() == "{not json"


def test_confirm_rejects_a_stale_proposal_revision(tmp_path):
    """Codex P2 (#248): Confirm means "approve what I saw". A worker
    re-proposal that replaced the record after the card was painted must
    not be confirmed unseen — the caller's ``expected_proposed_at`` has to
    match the stored revision."""
    store = TriageStore(tmp_path)
    _proposed(store, 1)  # proposed_at == "p"
    assert store.confirm("ben-ralston/agent-hq#1", now="n", expected_proposed_at="older") is None
    assert store.get("ben-ralston/agent-hq#1").state is TriageState.PROPOSED
    rec = store.confirm("ben-ralston/agent-hq#1", now="n", expected_proposed_at="p")
    assert rec.state is TriageState.CONFIRMED


def test_confirm_many_skips_stale_revisions_and_keeps_unconditional_keys(tmp_path):
    """A key with no expectation (legacy markup) still confirms; a key whose
    expectation mismatches is skipped, not an error."""
    store = TriageStore(tmp_path)
    for n in (1, 2, 3):
        _proposed(store, n)
    flipped = store.confirm_many(
        ["ben-ralston/agent-hq#1", "ben-ralston/agent-hq#2", "ben-ralston/agent-hq#3"],
        now="n",
        expected={"ben-ralston/agent-hq#1": "p", "ben-ralston/agent-hq#2": "older"},
    )
    assert flipped == 2
    assert store.get("ben-ralston/agent-hq#1").state is TriageState.CONFIRMED
    assert store.get("ben-ralston/agent-hq#2").state is TriageState.PROPOSED
    assert store.get("ben-ralston/agent-hq#3").state is TriageState.CONFIRMED


# ---- #248 follow-up: human edits never confirm; Confirm is explicit ----


def test_human_dimension_edit_keeps_proposed_until_explicit_confirm(tmp_path):
    """Editing one chip on an AI proposal is a correction, not an approval:
    the record stays PROPOSED (still in the inbox) with human provenance on
    the edited dim only, until ``confirm`` flips it."""
    store = TriageStore(tmp_path)
    _proposed(store, 1)
    store.upsert(
        TriageRecord(issue_key="ben-ralston/agent-hq#1", value=5, updated_at="x"),
        actor="human",
    )
    got = store.get("ben-ralston/agent-hq#1")
    assert got.state is TriageState.PROPOSED
    assert got.value == 5
    assert got.provenance == {"value": "human", "effort": "ai", "agent_fitness": "ai"}
    rec = store.confirm("ben-ralston/agent-hq#1", now="n", expected_proposed_at="p")
    assert rec.state is TriageState.CONFIRMED
    assert (rec.value, rec.effort, rec.agent_fitness) == (5, Effort.M, AgentFitness.PAIR)
    assert rec.provenance == {"value": "human", "effort": "ai", "agent_fitness": "ai"}


def test_human_hand_triage_stays_untriaged_until_confirm(tmp_path):
    """Hand triage from scratch is the same rule: three human dims leave the
    record UNTRIAGED; ``confirm`` needs all three and then flips it. Two
    dims are not confirmable."""
    store = TriageStore(tmp_path)
    key = "ben-ralston/agent-hq#9"
    store.upsert(TriageRecord(issue_key=key, value=3, updated_at="a"), actor="human")
    store.upsert(TriageRecord(issue_key=key, effort=Effort.S, updated_at="b"), actor="human")
    assert store.get(key).state is TriageState.UNTRIAGED
    assert store.confirm(key, now="n") is None
    store.upsert(
        TriageRecord(issue_key=key, agent_fitness=AgentFitness.SOLO, updated_at="c"), actor="human"
    )
    assert store.get(key).state is TriageState.UNTRIAGED
    rec = store.confirm(key, now="n")
    assert rec.state is TriageState.CONFIRMED
    assert rec.provenance == {"value": "human", "effort": "human", "agent_fitness": "human"}


def test_human_edit_after_confirm_keeps_confirmed(tmp_path):
    """A confirmed record edited later stays confirmed — the rule is "never
    flip forward", not "reset"."""
    store = TriageStore(tmp_path)
    _proposed(store, 1)
    store.confirm("ben-ralston/agent-hq#1", now="n")
    store.upsert(
        TriageRecord(issue_key="ben-ralston/agent-hq#1", effort=Effort.L, updated_at="x"),
        actor="human",
    )
    got = store.get("ben-ralston/agent-hq#1")
    assert got.state is TriageState.CONFIRMED and got.effort is Effort.L


# ---- #267: staged pin (Triage inbox row) ----


def _stage_pin(store, key, on=True):
    return store.upsert(
        TriageRecord(issue_key=key, pin_staged=on, updated_at="s"), actor="human"
    )


def test_pin_staged_round_trips_and_defaults_none():
    rec = _record(pin_staged=True)
    assert TriageRecord.from_dict(rec.to_dict()).pin_staged is True
    assert TriageRecord.from_dict({"issue_key": "o/r#1"}).pin_staged is None
    assert TriageRecord.from_dict({"issue_key": "o/r#1", "pin_staged": "yes"}).pin_staged is None


def test_staged_pin_leaves_effective_pin_alone_and_ai_never_touches_it(tmp_path):
    """The inbox row writes the STAGED flag only; ``pin`` (what the Queue
    reads) is untouched until Confirm, and an AI re-proposal in between
    neither clears the staged value nor commits it."""
    store = TriageStore(tmp_path)
    key = "ben-ralston/agent-hq#1"
    _proposed(store, 1)
    got = _stage_pin(store, key)
    assert got.pin_staged is True and got.pin is None
    assert got.state is TriageState.PROPOSED
    _proposed(store, 1)  # worker lands again
    got = store.get(key)
    assert got.pin_staged is True and got.pin is None
    # Partial human update (a dim chip) keeps the staged value too.
    store.upsert(TriageRecord(issue_key=key, value=5, updated_at="u"), actor="human")
    assert store.get(key).pin_staged is True


def test_confirm_folds_staged_pin_into_pin(tmp_path):
    """Confirm is the one write that commits the staged flag (#267)."""
    store = TriageStore(tmp_path)
    key = "ben-ralston/agent-hq#1"
    _proposed(store, 1)
    _stage_pin(store, key)
    rec = store.confirm(key, now="n")
    assert rec.state is TriageState.CONFIRMED
    assert rec.pin is True and rec.pin_staged is None
    # Staged unpin on an already-pinned issue folds to False the same way.
    _proposed(store, 2)
    store.upsert(
        TriageRecord(issue_key="ben-ralston/agent-hq#2", pin=True, updated_at="p"), actor="human"
    )
    _stage_pin(store, "ben-ralston/agent-hq#2", on=False)
    assert store.get("ben-ralston/agent-hq#2").pin is True  # still effective pre-confirm
    rec = store.confirm("ben-ralston/agent-hq#2", now="n")
    assert rec.pin is False and rec.pin_staged is None


def test_confirm_many_folds_staged_pin_only_on_records_it_flips(tmp_path):
    store = TriageStore(tmp_path)
    _proposed(store, 1)
    _stage_pin(store, "ben-ralston/agent-hq#1")
    store.upsert(
        TriageRecord(issue_key="ben-ralston/agent-hq#2", value=3, updated_at="t"), actor="ai"
    )  # incomplete → skipped by confirm
    _stage_pin(store, "ben-ralston/agent-hq#2")
    flipped = store.confirm_many(["ben-ralston/agent-hq#1", "ben-ralston/agent-hq#2"], now="n")
    assert flipped == 1
    one = store.get("ben-ralston/agent-hq#1")
    assert one.pin is True and one.pin_staged is None
    two = store.get("ben-ralston/agent-hq#2")
    assert two.pin is None and two.pin_staged is True  # untouched, still staged


def test_direct_pin_write_clears_a_stale_staged_pin(tmp_path):
    """A human pinning/unpinning from Queue or the sheet supersedes whatever
    was staged in the inbox — Confirm must not later replay the stale
    staged value over the newer explicit one."""
    store = TriageStore(tmp_path)
    key = "ben-ralston/agent-hq#1"
    _proposed(store, 1)
    _stage_pin(store, key)
    store.upsert(TriageRecord(issue_key=key, pin=False, updated_at="p"), actor="human")
    got = store.get(key)
    assert got.pin is False and got.pin_staged is None
    rec = store.confirm(key, now="n")
    assert rec.pin is False


def test_staged_pin_write_onto_a_non_proposed_record_pins_directly(tmp_path):
    """Codex round 2 of #267: the route can't check "still PROPOSED" and
    write atomically, so the STORE owns the invariant under its lock —
    ``pin_staged`` only ever lives on a PROPOSED record. A staged write
    landing on a record confirmed meanwhile (or never proposed) becomes a
    direct pin, never an orphan that no Confirm would fold."""
    store = TriageStore(tmp_path)
    key = "ben-ralston/agent-hq#1"
    _proposed(store, 1)
    store.confirm(key, now="n")
    got = _stage_pin(store, key)
    assert got.state is TriageState.CONFIRMED
    assert got.pin is True and got.pin_staged is None
    # Untriaged record (never in the inbox): same demotion.
    got = _stage_pin(store, "ben-ralston/agent-hq#2", on=True)
    assert got.state is TriageState.UNTRIAGED
    assert got.pin is True and got.pin_staged is None
    # A live PROPOSED record still stages.
    _proposed(store, 3)
    got = _stage_pin(store, "ben-ralston/agent-hq#3")
    assert got.pin is None and got.pin_staged is True


class TestAutopilotFlag:
    KEY = "ben-ralston/agent-hq#900"

    def test_round_trips_and_tolerates_garbage(self):
        rec = TriageRecord(issue_key=self.KEY, autopilot=True)
        assert TriageRecord.from_dict(rec.to_dict()).autopilot is True
        d = rec.to_dict()
        d["autopilot"] = "yes"
        assert TriageRecord.from_dict(d).autopilot is None
        d.pop("autopilot")
        assert TriageRecord.from_dict(d).autopilot is None

    def test_human_write_sets_and_partial_write_keeps(self, tmp_path):
        store = TriageStore(tmp_path)
        store.upsert(
            TriageRecord(issue_key=self.KEY, autopilot=True, updated_at="t1"), actor="human"
        )
        store.upsert(TriageRecord(issue_key=self.KEY, value=3, updated_at="t2"), actor="human")
        assert store.get(self.KEY).autopilot is True
        store.upsert(
            TriageRecord(issue_key=self.KEY, autopilot=False, updated_at="t3"), actor="human"
        )
        assert store.get(self.KEY).autopilot is False

    def test_ai_write_never_sets_or_clears(self, tmp_path):
        store = TriageStore(tmp_path)
        store.upsert(
            TriageRecord(issue_key=self.KEY, autopilot=True, updated_at="t1"), actor="human"
        )
        store.upsert(
            TriageRecord(issue_key=self.KEY, value=2, autopilot=False, updated_at="t2"), actor="ai"
        )
        assert store.get(self.KEY).autopilot is True
        other = "ben-ralston/agent-hq#901"
        store.upsert(
            TriageRecord(issue_key=other, value=2, autopilot=True, updated_at="t3"), actor="ai"
        )
        assert store.get(other).autopilot is None

    def test_confirm_preserves_flag(self, tmp_path):
        store = TriageStore(tmp_path)
        store.upsert(
            TriageRecord(
                issue_key=self.KEY,
                value=3,
                effort=Effort.S,
                agent_fitness=AgentFitness.SOLO,
                autopilot=True,
                updated_at="t1",
            ),
            actor="human",
        )
        rec = store.confirm(self.KEY, now="t2")
        assert rec is not None and rec.state is TriageState.CONFIRMED and rec.autopilot is True


# --- #397: the "duet" actor (Claude Duet writes over the remote connector) ---


def _duet(**kw):
    """A Duet write: dimensions only, no flags, no proposal metadata."""
    base = dict(issue_key="ben-ralston/agent-hq#246", updated_at="2026-10-31T09:00:00Z")
    base.update(kw)
    return TriageRecord(**base)


def test_duet_upsert_stamps_duet_provenance(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_duet(value=3, effort=Effort.S, agent_fitness=AgentFitness.PAIR), actor="duet")
    got = store.get(_duet().issue_key)
    assert got.provenance == {"value": "duet", "effort": "duet", "agent_fitness": "duet"}
    assert got.state is TriageState.UNTRIAGED  # a duet write never changes state


def test_duet_provenance_survives_from_dict():
    rec = _record(provenance={"value": "duet", "effort": "ai", "agent_fitness": "human"})
    assert TriageRecord.from_dict(rec.to_dict()).provenance == rec.provenance


def test_duet_overwrites_human_dims_and_confirmed_record(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_record(), actor="human")  # human-owned
    store.confirm(_record().issue_key, now="2026-10-31T08:00:00Z")
    store.upsert(_duet(value=1), actor="duet")
    got = store.get(_record().issue_key)
    assert got.value == 1
    assert got.provenance["value"] == "duet"
    assert got.provenance["effort"] == "human"
    assert got.state is TriageState.CONFIRMED


def test_duet_write_keeps_flags_and_metadata(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_record(interest=True, pin=True, autopilot=True), actor="human")
    store.upsert(_duet(value=2), actor="duet")
    got = store.get(_record().issue_key)
    assert (got.interest, got.pin, got.autopilot) == (True, True, True)
    assert got.proposed_at == "2026-08-22T10:00:00Z"
    assert got.model == "claude-sonnet-5"


def test_duet_reasoning_replaces_supplied_dims_and_clears_without_it(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(
        _record(reasoning={"value": "old value why", "effort": "old effort why"}), actor="ai"
    )
    store.upsert(_duet(value=5, reasoning={"value": "the owner needs it this week"}), actor="duet")
    got = store.get(_record().issue_key)
    assert got.reasoning == {"value": "the owner needs it this week", "effort": "old effort why"}
    store.upsert(_duet(effort=Effort.L), actor="duet")  # no reasoning supplied
    got = store.get(_record().issue_key)
    assert "effort" not in got.reasoning  # stale AI justification cleared
    assert got.reasoning["value"] == "the owner needs it this week"


def test_ai_never_overwrites_duet_dims_or_reasoning(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(_duet(value=5, reasoning={"value": "duet why"}), actor="duet")
    store.upsert(
        _record(
            value=1, state=TriageState.PROPOSED, reasoning={"value": "ai why", "effort": "e"}
        ),
        actor="ai",
    )
    got = store.get(_record().issue_key)
    assert got.value == 5
    assert got.provenance["value"] == "duet"
    assert got.reasoning["value"] == "duet why"
    assert got.effort == Effort.M  # the unowned dims still take the AI proposal
