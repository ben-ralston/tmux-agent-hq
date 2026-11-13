"""Tests for agent_hq.issues_hub — Issue↔TriageRecord join layer (#247)."""

from agent_hq.issues_hub import (
    annotate,
    dimension_filter_options,
    ensure_triage_records,
    filter_by_dimensions,
    merge_issue_lists,
    priority_hint_for,
    queue_partition,
)
from agent_hq.models import Issue, Priority
from agent_hq.triage_store import (
    AgentFitness,
    Effort,
    TriageRecord,
    TriageState,
    TriageStore,
)

REPO = "ben-ralston/agent-hq"


def _issue(number, *, repo=REPO, priority=Priority.NO_PRI, title=None):
    return Issue(
        number=number,
        title=title or f"Issue {number}",
        repo=repo,
        status="Todo",
        priority=priority,
    )


def _triaged(
    store, number, *, repo=REPO, value=3, effort=Effort.M, fitness=AgentFitness.PAIR, **kw
):
    rec = TriageRecord(
        issue_key=f"{repo}#{number}",
        value=value,
        effort=effort,
        agent_fitness=fitness,
        updated_at="t0",
        **kw,
    )
    store.upsert(rec, actor="human")  # human writes never flip state on their own
    return store.confirm(rec.issue_key, now="t0")


# ---- annotate ----


def test_annotate_joins_records_by_issue_key(tmp_path):
    store = TriageStore(tmp_path)
    _triaged(store, 1, value=4, effort=Effort.S, fitness=AgentFitness.SOLO)
    issues = [_issue(1), _issue(2)]
    anns = annotate(issues, store)
    one = anns[f"{REPO}#1"]
    assert one.record is not None
    assert one.score == 4 * 2 + 2 + 2
    assert one.breakdown == [("V4", 8), ("S", 2), ("solo", 2)]
    assert one.effort_id == "s"
    assert one.fitness_id == "solo"
    assert one.pinned is False
    two = anns[f"{REPO}#2"]
    assert two.record is None
    assert two.score is None
    assert two.breakdown == []
    assert two.effort_id == "none"
    assert two.fitness_id == "none"


def test_annotate_pinned_flag(tmp_path):
    store = TriageStore(tmp_path)
    store.upsert(TriageRecord(issue_key=f"{REPO}#1", pin=True, updated_at="t0"), actor="human")
    anns = annotate([_issue(1)], store)
    assert anns[f"{REPO}#1"].pinned is True
    assert anns[f"{REPO}#1"].score is None  # pin alone doesn't rank


def test_annotate_respects_custom_weights(tmp_path):
    store = TriageStore(tmp_path)
    _triaged(store, 1, value=1, effort=Effort.L, fitness=AgentFitness.NEEDS_YOU)
    anns = annotate(
        [_issue(1)],
        store,
        weights={
            "value_multiplier": 10,
            "effort_bonus": {"S": 0, "M": 0, "L": 0},
            "fitness_bonus": {"solo": 0, "pair": 0, "needs_you": 0},
            "interest_bonus": 0,
        },
    )
    assert anns[f"{REPO}#1"].score == 10


# ---- queue_partition ----


def test_queue_partition_orders_by_score_then_default_key(tmp_path):
    store = TriageStore(tmp_path)
    _triaged(store, 1, value=2, effort=Effort.L, fitness=AgentFitness.NEEDS_YOU)  # 4
    _triaged(store, 2, value=5, effort=Effort.S, fitness=AgentFitness.SOLO)  # 14
    _triaged(store, 3, value=5, effort=Effort.S, fitness=AgentFitness.SOLO)  # 14, ties → number
    issues = [_issue(1), _issue(2), _issue(3), _issue(4)]
    pinned, ranked, scoreless = queue_partition(issues, annotate(issues, store))
    assert pinned == []
    assert [i.number for i in ranked] == [2, 3, 1]
    assert [i.number for i in scoreless] == [4]


def test_queue_partition_pinned_strip_floats_above(tmp_path):
    store = TriageStore(tmp_path)
    _triaged(store, 1, value=5, effort=Effort.S, fitness=AgentFitness.SOLO)
    store.upsert(TriageRecord(issue_key=f"{REPO}#2", pin=True, updated_at="t0"), actor="human")
    issues = [_issue(1), _issue(2)]
    pinned, ranked, scoreless = queue_partition(issues, annotate(issues, store))
    assert [i.number for i in pinned] == [2]  # pinned even though scoreless
    assert [i.number for i in ranked] == [1]
    assert scoreless == []


def test_queue_partition_confirmed_but_scoreless_lands_in_untriaged(tmp_path):
    store = TriageStore(tmp_path)
    # A partial record can't be confirmed through the store any more (confirm
    # needs all three dims), but legacy data may still hold one: CONFIRMED
    # state with no rank score. Partition is by score presence, not state.
    store.upsert(TriageRecord(issue_key=f"{REPO}#1", value=4, updated_at="t0"), actor="human")
    issues = [_issue(1)]
    anns = annotate(issues, store)
    assert anns[f"{REPO}#1"].record.state is TriageState.UNTRIAGED
    anns[f"{REPO}#1"].record.state = TriageState.CONFIRMED  # legacy shape
    pinned, ranked, scoreless = queue_partition(issues, anns)
    assert ranked == []
    assert [i.number for i in scoreless] == [1]


def test_queue_partition_proposed_score_stays_untriaged(tmp_path):
    store = TriageStore(tmp_path)
    # AI supplied all three dims → PROPOSED. The suggestion is displayable
    # (annotate still scores it) but must not reorder the "what next" list
    # until a human confirms (spec: AI influences rank only after confirm).
    stored = store.upsert(
        TriageRecord(
            issue_key=f"{REPO}#1",
            value=5,
            effort=Effort.S,
            agent_fitness=AgentFitness.SOLO,
            updated_at="t0",
        ),
        actor="ai",
    )
    assert stored.state is TriageState.PROPOSED
    issues = [_issue(1)]
    anns = annotate(issues, store)
    assert anns[f"{REPO}#1"].score is not None
    pinned, ranked, scoreless = queue_partition(issues, anns)
    assert ranked == []
    assert [i.number for i in scoreless] == [1]


def test_queue_partition_direct_pin_on_proposed_record_stays_effective(tmp_path):
    """Codex review of #267: a human's Queue/Browse pin must float the issue
    whatever the record's state — and an AI proposal landing on an already
    pinned issue must not knock it out of the strip."""
    store = TriageStore(tmp_path)
    store.upsert(TriageRecord(issue_key=f"{REPO}#1", pin=True, updated_at="t0"), actor="human")
    store.upsert(
        TriageRecord(
            issue_key=f"{REPO}#1",
            value=5,
            effort=Effort.S,
            agent_fitness=AgentFitness.SOLO,
            updated_at="t1",
        ),
        actor="ai",
    )  # worker proposes on the pinned issue → PROPOSED, pin kept
    issues = [_issue(1)]
    anns = annotate(issues, store)
    assert anns[f"{REPO}#1"].record.state is TriageState.PROPOSED
    pinned, ranked, scoreless = queue_partition(issues, anns)
    assert [i.number for i in pinned] == [1]


def test_queue_partition_ignores_inbox_staged_pin_until_confirm(tmp_path):
    """#267: a pin STAGED from the Triage inbox row (``pin_staged``) does not
    float the issue; Confirm folds it into ``pin`` and then it does."""
    store = TriageStore(tmp_path)
    store.upsert(
        TriageRecord(
            issue_key=f"{REPO}#1",
            value=5,
            effort=Effort.S,
            agent_fitness=AgentFitness.SOLO,
            updated_at="t0",
        ),
        actor="ai",
    )
    store.upsert(
        TriageRecord(issue_key=f"{REPO}#1", pin_staged=True, updated_at="t1"), actor="human"
    )
    issues = [_issue(1)]
    anns = annotate(issues, store)
    assert anns[f"{REPO}#1"].pinned is False
    pinned, ranked, scoreless = queue_partition(issues, anns)
    assert pinned == []
    assert [i.number for i in scoreless] == [1]
    store.confirm(f"{REPO}#1", now="t2")
    pinned, ranked, scoreless = queue_partition(issues, annotate(issues, store))
    assert [i.number for i in pinned] == [1]
    assert ranked == [] and scoreless == []


# ---- filter_by_dimensions ----


def test_filter_by_dimensions_effort_and_fitness(tmp_path):
    store = TriageStore(tmp_path)
    _triaged(store, 1, effort=Effort.S, fitness=AgentFitness.SOLO)
    _triaged(store, 2, effort=Effort.L, fitness=AgentFitness.SOLO)
    issues = [_issue(1), _issue(2), _issue(3)]
    anns = annotate(issues, store)
    assert [i.number for i in filter_by_dimensions(issues, anns, efforts=["s"], fitnesses=[])] == [
        1
    ]
    assert [
        i.number for i in filter_by_dimensions(issues, anns, efforts=["s", "l"], fitnesses=[])
    ] == [1, 2]
    # AND across dimensions
    assert [
        i.number for i in filter_by_dimensions(issues, anns, efforts=["l"], fitnesses=["solo"])
    ] == [2]
    # unset dims are filterable as "none"
    assert [
        i.number for i in filter_by_dimensions(issues, anns, efforts=["none"], fitnesses=[])
    ] == [3]
    # empty constraint = everything
    assert len(filter_by_dimensions(issues, anns, efforts=[], fitnesses=[])) == 3


# ---- dimension_filter_options ----


def test_dimension_filter_options_counts_and_none_last(tmp_path):
    store = TriageStore(tmp_path)
    _triaged(store, 1, effort=Effort.S, fitness=AgentFitness.SOLO)
    _triaged(store, 2, effort=Effort.S, fitness=AgentFitness.NEEDS_YOU)
    issues = [_issue(1), _issue(2), _issue(3)]
    opts = dimension_filter_options(issues, annotate(issues, store))
    assert opts["efforts"] == [
        {"id": "s", "label": "S", "count": 2},
        {"id": "none", "label": "—", "count": 1},
    ]
    assert opts["fitnesses"] == [
        {"id": "solo", "label": "solo", "count": 1},
        {"id": "needs_you", "label": "needs you", "count": 1},
        {"id": "none", "label": "—", "count": 1},
    ]


def test_dimension_filter_options_empty_issue_list():
    assert dimension_filter_options([], {}) == {"efforts": [], "fitnesses": [], "autopilots": []}


# ---- autopilot flag (#302 follow-up) ----


def test_annotate_carries_autopilot_flag(tmp_path):
    store = TriageStore(tmp_path)
    _triaged(store, 1, autopilot=True)
    _triaged(store, 2)
    issues = [_issue(1), _issue(2), _issue(3)]
    anns = annotate(issues, store)
    assert [anns[i.key].autopilot for i in issues] == [True, False, False]


def test_filter_by_dimensions_autopilot_chip(tmp_path):
    store = TriageStore(tmp_path)
    _triaged(store, 1, autopilot=True)
    _triaged(store, 2)
    issues = [_issue(1), _issue(2), _issue(3)]
    anns = annotate(issues, store)
    got = filter_by_dimensions(issues, anns, efforts=[], fitnesses=[], autopilots=["on"])
    assert [i.number for i in got] == [1]
    assert len(filter_by_dimensions(issues, anns, autopilots=[])) == 3


def test_dimension_filter_options_autopilot_chip_only_when_flagged(tmp_path):
    store = TriageStore(tmp_path)
    _triaged(store, 1, autopilot=True)
    _triaged(store, 2, autopilot=False)
    issues = [_issue(1), _issue(2)]
    opts = dimension_filter_options(issues, annotate(issues, store))
    assert opts["autopilots"] == [{"id": "on", "label": "⏵ Autopilot", "count": 1}]
    opts = dimension_filter_options(issues[1:], annotate(issues[1:], store))
    assert opts["autopilots"] == []


# ---- priority_hint_for ----


def test_priority_hint_for_maps_labels():
    assert priority_hint_for(Priority.NEED) == "Need"
    assert priority_hint_for(Priority.WANT) == "Want"
    assert priority_hint_for(Priority.IDEA) == "Idea"
    assert priority_hint_for(Priority.NO_PRI) is None


# ---- merge_issue_lists ----


def test_merge_issue_lists_drops_done_and_dedups_by_key():
    """The one flatten rule for per-project issue lists (#248): the web view
    and ``agent-hq triage run`` must see the same set, or the CLI proposes
    for Done issues and revives records the web pass tombstoned."""
    a = _issue(1)
    dup = _issue(1)  # same issue on a second board
    done = _issue(2)
    done.status = "Done"
    other = _issue(3)
    merged = merge_issue_lists([[a, done], [dup, other]])
    assert [i.number for i in merged] == [1, 3]


def test_merge_issue_lists_done_match_is_case_insensitive_and_none_safe():
    lower = _issue(1)
    lower.status = "done"
    none_status = _issue(2)
    none_status.status = None
    assert [i.number for i in merge_issue_lists([[lower, none_status]])] == [2]


# ---- ensure_triage_records ----


def test_ensure_seeds_missing_with_hints(tmp_path):
    store = TriageStore(tmp_path)
    issues = [_issue(1, priority=Priority.NEED), _issue(2)]
    created = ensure_triage_records(store, issues, now="t0")
    assert created == 2
    assert store.get(f"{REPO}#1").priority_hint == "Need"
    assert store.get(f"{REPO}#1").state is TriageState.UNTRIAGED
    assert store.get(f"{REPO}#2").priority_hint is None


def test_ensure_tombstones_closed_and_revives_reopened(tmp_path):
    store = TriageStore(tmp_path)
    ensure_triage_records(store, [_issue(1), _issue(2)], now="t0")
    ensure_triage_records(store, [_issue(2)], now="t1")  # 1 disappeared
    assert store.get(f"{REPO}#1").tombstoned_at == "t1"
    assert store.get(f"{REPO}#2").tombstoned_at is None
    ensure_triage_records(store, [_issue(1), _issue(2)], now="t2")  # reopened
    assert store.get(f"{REPO}#1").tombstoned_at is None


def test_ensure_counts_tombstone_and_revive_writes(tmp_path):
    # The return is "records written", not "records seeded" — callers use
    # a non-zero return to know their pre-pass store snapshot went stale.
    store = TriageStore(tmp_path)
    assert ensure_triage_records(store, [_issue(1), _issue(2)], now="t0") == 2
    assert ensure_triage_records(store, [_issue(2)], now="t1") == 1  # tombstone
    assert ensure_triage_records(store, [_issue(1), _issue(2)], now="t2") == 1  # revive


def test_ensure_steady_state_writes_nothing(tmp_path):
    store = TriageStore(tmp_path)
    ensure_triage_records(store, [_issue(1)], now="t0")
    path = tmp_path / "triage" / "ben-ralston__agent-hq.json"
    before = path.read_text()
    stamp = path.stat().st_mtime_ns
    assert ensure_triage_records(store, [_issue(1)], now="t1") == 0
    assert path.read_text() == before
    assert path.stat().st_mtime_ns == stamp


def test_ensure_groups_by_repo(tmp_path):
    store = TriageStore(tmp_path)
    issues = [_issue(1), _issue(1, repo="other/repo"), _issue(2, repo="other/repo")]
    created = ensure_triage_records(store, issues, now="t0")
    assert created == 3
    assert (tmp_path / "triage" / "ben-ralston__agent-hq.json").exists()
    assert (tmp_path / "triage" / "other__repo.json").exists()
    # closing one repo's issue doesn't tombstone the other repo's records
    ensure_triage_records(store, [_issue(1), _issue(2, repo="other/repo")], now="t1")
    assert store.get("other/repo#1").tombstoned_at == "t1"
    assert store.get(f"{REPO}#1").tombstoned_at is None
