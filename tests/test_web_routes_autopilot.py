"""Autopilot card on the Automations tab (#302)."""

from fastapi.testclient import TestClient

from agent_hq.autopilot_store import (
    Attempt,
    AutopilotStore,
    Outcome,
    OutcomeUpdate,
    RunRecord,
)
from agent_hq.config import AutopilotConfig, Config, TriageConfig
from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value

REPO = "ben-ralston/agent-hq"


def _client(tmp_path, *, autopilot=AutopilotConfig(enabled=True, repos=())):
    config = Config(
        repos={},
        projects=[],
        worktree_root=tmp_path,
        triage=TriageConfig(data_dir=tmp_path),
        autopilot=autopilot,
    )
    store = AutopilotStore(tmp_path)
    refreshes = []
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(
        token_file=tm.token_file,
        config=config,
        autopilot_store=store,
        autopilot_refresh=lambda s, c, now: refreshes.append(now),
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client, store, refreshes


def _put(
    store,
    n,
    outcome,
    *,
    pr_url=None,
    detail=None,
    session_name=None,
    dispatched_at="2026-10-08T01:00:00-05:00",
):
    """Drive the store's real write path: reserve → finish_launch → (CAS)."""
    key, aid = f"{REPO}#{n}", f"a{n}"
    store.reserve(
        Attempt(
            issue_key=key,
            repo=REPO,
            branch=f"feature/{n}-x",
            run_id="r",
            dispatched_at=dispatched_at,
            outcome=Outcome.LAUNCHING,
            attempt_id=aid,
        ),
        max_open=99,
    )
    if outcome is Outcome.LAUNCHING:
        return
    first = Outcome.RUNNING if pr_url else outcome
    store.finish_launch(
        key,
        aid,
        outcome=first,
        session_name=session_name,
        worktree_path=None,
        detail=detail,
        now="t",
    )
    if pr_url:
        store.apply_outcomes(
            {key: OutcomeUpdate(aid, Outcome.RUNNING, outcome, pr_url=pr_url)}, now="t"
        )


def test_panel_hidden_without_config(tmp_path):
    client, _, _ = _client(tmp_path, autopilot=None)
    r = client.get("/automations/autopilot")
    assert r.status_code == 200 and r.text.strip() == ""


def test_panel_shows_waiting_and_attention(tmp_path):
    client, store, refreshes = _client(tmp_path)
    _put(store, 1, Outcome.PR_OPEN, pr_url="https://github.com/x/pull/9")
    _put(store, 2, Outcome.LAUNCH_FAILED, detail="wt not found")
    _put(store, 3, Outcome.MERGED)
    _put(store, 5, Outcome.UNKNOWN, detail="launch.sh timed out after 300s")
    store.record_run(
        RunRecord(
            run_id="r",
            started_at="2026-10-08T01:00:00-05:00",
            finished_at="2026-10-08T01:01:00-05:00",
            launched=[f"{REPO}#1"],
            skipped=[(f"{REPO}#4", "throttled")],
            throttled=False,
        )
    )
    r = client.get("/automations/autopilot")
    assert "Autopilot" in r.text
    assert "https://github.com/x/pull/9" in r.text
    assert "wt not found" in r.text and "Retry" in r.text
    assert f"{REPO}#3" not in r.text  # final merged attempts hidden
    assert "timed out" in r.text and "uncertain" in r.text  # unknown → needs attention
    assert "throttled" in r.text
    assert len(refreshes) == 1  # first GET in this process → refresh ran


def test_card_carries_phone_wrap_hooks(tmp_path):
    """The phone-width wrap CSS (#302) is scoped to these classes."""
    import re

    client, store, _ = _client(tmp_path)
    _put(store, 2, Outcome.LAUNCH_FAILED, detail="wt not found")
    r = client.get("/automations/autopilot")
    assert 'class="automations autopilot"' in r.text
    # Pause + one Retry, each in its own wrap-safe action container.
    actions = re.findall(r'<span class="autopilot__action"[^>]*>\s*<button', r.text)
    assert len(actions) == 2
    css = client.get("/static/components.css").text
    assert ".autopilot .automation-row__main" in css
    assert ".autopilot__action" in css


def test_refresh_throttle_is_in_memory_per_process(tmp_path):
    client, store, refreshes = _client(tmp_path)
    _put(store, 1, Outcome.RUNNING, session_name="s1")
    client.get("/automations/autopilot")
    client.get("/automations/autopilot")
    assert len(refreshes) == 1  # second GET within 300 s doesn't refresh
    client.app.state.autopilot_last_refresh -= 301  # monotonic clock moved on
    client.get("/automations/autopilot")
    assert len(refreshes) == 2


def test_zero_update_refresh_leaves_state_file_untouched(tmp_path):
    from agent_hq.autopilot import NO_PR, sync_outcomes

    client, store, _ = _client(tmp_path)
    _put(store, 1, Outcome.RUNNING, session_name="s1")
    client.app.state.autopilot_refresh = lambda s, c, now: sync_outcomes(
        s, now=now, pr_lookup=lambda repo, branch: NO_PR, session_probe=lambda name: True
    )
    before = store.path.read_bytes()
    assert client.get("/automations/autopilot").status_code == 200
    assert store.path.read_bytes() == before


def test_disabled_and_paused_labels(tmp_path):
    client, store, _ = _client(tmp_path, autopilot=AutopilotConfig(enabled=False))
    assert "Disabled" in client.get("/automations/autopilot").text
    client, store, _ = _client(tmp_path)
    store.set_paused(True)
    assert "Paused" in client.get("/automations/autopilot").text


def test_pause_and_resume(tmp_path):
    client, store, _ = _client(tmp_path)
    r = client.post("/automations/autopilot/pause", data={"paused": "1"})
    assert r.status_code == 200 and store.snapshot().paused is True and "Resume" in r.text
    client.post("/automations/autopilot/pause", data={"paused": "0"})
    assert store.snapshot().paused is False


def test_pause_rejects_bad_value(tmp_path):
    client, store, _ = _client(tmp_path)
    assert client.post("/automations/autopilot/pause", data={"paused": "x"}).status_code == 400


def test_retry_clears_failed_attempt(tmp_path):
    client, store, _ = _client(tmp_path)
    _put(store, 2, Outcome.LAUNCH_FAILED)
    r = client.post("/automations/autopilot/retry", data={"key": f"{REPO}#2"})
    assert r.status_code == 200 and f"{REPO}#2" not in store.snapshot().attempts


def test_retry_refuses_running(tmp_path):
    client, store, _ = _client(tmp_path)
    _put(store, 1, Outcome.RUNNING)
    r = client.post("/automations/autopilot/retry", data={"key": f"{REPO}#1"})
    assert r.status_code == 409 and f"{REPO}#1" in store.snapshot().attempts


def test_stale_launching_shows_retry(tmp_path):
    client, store, _ = _client(tmp_path)
    _put(store, 6, Outcome.LAUNCHING, dispatched_at="2026-01-01T00:00:00+00:00")
    r = client.get("/automations/autopilot")
    assert "launch interrupted — check the session, then Retry" in r.text and "Retry" in r.text
    assert client.post("/automations/autopilot/retry", data={"key": f"{REPO}#6"}).status_code == 200


def test_retry_copy_says_running_agent_is_not_stopped(tmp_path):
    client, store, _ = _client(tmp_path)
    _put(store, 5, Outcome.UNKNOWN, detail="launch.sh timed out after 300s")
    text = client.get("/automations/autopilot").text
    assert "check the session, then Retry" in text
    assert "does not stop" in text


def test_retry_copy_says_old_branch_must_be_removed(tmp_path):
    # Every attempt leaves its feature/<n>-…-ap<id> branch, which the strict
    # branch probe matches: Retry alone frees the slot, not the issue.
    client, store, _ = _client(tmp_path)
    _put(store, 2, Outcome.LAUNCH_FAILED, detail="wt not found")
    _put(store, 5, Outcome.ENDED_WITHOUT_PR)
    text = client.get("/automations/autopilot").text
    assert "frees the slot" in text
    assert "remove the attempt's worktree and branch" in text
    assert 'its row says "cleaned up"' in text  # #312: launch failures self-clean
    assert "frees the issue" not in text and "eligible again" not in text
    for n in (2, 5):  # each Needs-attention row names its attempt's branch
        assert f"feature/{n}-x" in text


def test_injected_store_lands_on_state(tmp_path):
    client, store, _ = _client(tmp_path)
    assert client.app.state.autopilot_store is store


# ---- controller rulings: fail-loud on an unreadable / partly unreadable file ----


def _corrupt(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json")


def test_unreadable_state_is_shown_not_empty(tmp_path):
    client, store, refreshes = _client(tmp_path)
    _corrupt(store)
    r = client.get("/automations/autopilot")
    assert r.status_code == 200
    assert "Autopilot state file unreadable" in r.text and "corrupt" in r.text
    assert "Pause" not in r.text and "Retry" not in r.text
    assert refreshes == []


def test_pause_on_unreadable_state_errors(tmp_path):
    client, store, _ = _client(tmp_path)
    _corrupt(store)
    r = client.post("/automations/autopilot/pause", data={"paused": "1"})
    assert r.status_code != 200 and r.status_code >= 400
    assert store.path.read_text() == "{not json"  # never overwritten


def test_retry_on_unreadable_state_errors(tmp_path):
    client, store, _ = _client(tmp_path)
    _corrupt(store)
    r = client.post("/automations/autopilot/retry", data={"key": f"{REPO}#2"})
    assert r.status_code >= 400 and r.status_code != 409


def test_unparsed_attempts_are_counted(tmp_path):
    import json

    client, store, _ = _client(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"attempts": {f"{REPO}#7": {"junk": 1}}, "runs": []}))
    text = client.get("/automations/autopilot").text
    assert "1 unreadable attempt record(s) — fix state.json by hand" in text
    assert "Retry" not in text


def test_automations_page_hosts_the_card(tmp_path):
    client, _, _ = _client(tmp_path)
    assert 'hx-get="/automations/autopilot"' in client.get("/automations").text


def test_closed_pr_needs_attention_and_retries(tmp_path):
    client, store, _ = _client(tmp_path)
    _put(store, 8, Outcome.CLOSED, pr_url="https://github.com/x/pull/8")
    assert store.snapshot().attempts[f"{REPO}#8"].outcome is Outcome.CLOSED
    text = client.get("/automations/autopilot").text
    assert "PR closed without merge" in text and "Retry" in text
    assert "https://github.com/x/pull/8" in text
    r = client.post("/automations/autopilot/retry", data={"key": f"{REPO}#8"})
    assert r.status_code == 200 and f"{REPO}#8" not in store.snapshot().attempts


def test_fresh_launching_reads_launching_since(tmp_path):
    from datetime import datetime

    client, store, _ = _client(tmp_path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    _put(store, 9, Outcome.LAUNCHING, dispatched_at=now)
    text = client.get("/automations/autopilot").text
    assert "launching since" in text and "running since" not in text
    assert "launch interrupted" not in text


# ---- #302 follow-up: Discard, row → agent sheet, ⏵ on agent cards ----


def _flag(client, n, on=True):
    from agent_hq.triage_store import TriageRecord

    client.app.state.triage_store.upsert(
        TriageRecord(issue_key=f"{REPO}#{n}", autopilot=on), actor="human"
    )


def _watch(client, *agents):
    from unittest.mock import MagicMock

    client.app.state.watcher = MagicMock()
    client.app.state.watcher.snapshot.return_value = list(agents)


def _agent(pane_id, branch):
    from agent_hq.models import Agent, AgentState, AgentType

    return Agent(
        pane_id=pane_id,
        session_name="ses",
        window_index=0,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd="/tmp",
        repo_name="agent-hq",
        branch=branch,
        state=AgentState.IDLE,
    )


def test_discard_forgets_attempt_and_unflags_issue(tmp_path):
    client, store, _ = _client(tmp_path)
    _put(store, 8, Outcome.CLOSED, pr_url="https://github.com/x/pull/8")
    _flag(client, 8)
    r = client.post("/automations/autopilot/discard", data={"key": f"{REPO}#8"})
    assert r.status_code == 200
    assert f"{REPO}#8" not in store.snapshot().attempts
    assert client.app.state.triage_store.get(f"{REPO}#8").autopilot is False


def test_discard_refuses_a_running_attempt_and_keeps_the_flag(tmp_path):
    client, store, _ = _client(tmp_path)
    _put(store, 9, Outcome.RUNNING)
    _flag(client, 9)
    r = client.post("/automations/autopilot/discard", data={"key": f"{REPO}#9"})
    assert r.status_code == 409
    assert f"{REPO}#9" in store.snapshot().attempts
    assert client.app.state.triage_store.get(f"{REPO}#9").autopilot is True


def test_attention_rows_offer_discard(tmp_path):
    client, store, _ = _client(tmp_path)
    _put(store, 2, Outcome.LAUNCH_FAILED, detail="wt not found")
    text = client.get("/automations/autopilot").text
    assert 'hx-post="/automations/autopilot/discard"' in text
    assert "Confirm discard" in text  # two-step: the POST sits behind a confirm


def test_waiting_row_opens_the_agent_sheet(tmp_path):
    client, store, _ = _client(tmp_path)
    _put(store, 1, Outcome.PR_OPEN, pr_url="https://github.com/x/pull/9")
    _watch(client, _agent("%42", "feature/1-x"))
    text = client.get("/automations/autopilot").text
    assert 'hx-get="/agents/42"' in text and 'hx-target="#detail-slot"' in text


def test_waiting_row_without_a_live_agent_opens_the_pr(tmp_path):
    client, store, _ = _client(tmp_path)
    _put(store, 1, Outcome.PR_OPEN, pr_url="https://github.com/x/pull/9")
    _watch(client)
    text = client.get("/automations/autopilot").text
    assert 'hx-get="/agents/' not in text
    assert 'class="automation-row automation-row--link" href="https://github.com/x/pull/9"' in text


def test_agent_cards_badge_autopilot_branches(tmp_path):
    client, store, _ = _client(tmp_path)
    _put(store, 1, Outcome.RUNNING)
    _watch(client, _agent("%1", "feature/1-x"), _agent("%2", "feature/2-hand-made"))
    text = client.get("/agents/list").text
    first = text[text.index('data-card-slug="1"') :]
    second = text[text.index('data-card-slug="2"') :]
    assert "autopilot-badge" in first[: first.index("</article>")]
    assert "autopilot-badge" not in second[: second.index("</article>")]
    assert "autopilot-badge" in client.get("/agents/1/card").text


def test_discard_keeps_the_attempt_when_the_flag_write_fails(tmp_path, monkeypatch):
    # Review round 1: TriageStore.upsert swallows an unreadable file and
    # returns None. Forgetting the attempt anyway would free the slot with
    # the flag still on, so the nightly run would relaunch the issue.
    client, store, _ = _client(tmp_path)
    _put(store, 8, Outcome.CLOSED)
    _flag(client, 8)
    monkeypatch.setattr(client.app.state.triage_store, "upsert", lambda *a, **k: None)
    r = client.post("/automations/autopilot/discard", data={"key": f"{REPO}#8"})
    assert r.status_code == 503
    assert f"{REPO}#8" in store.snapshot().attempts


def test_discard_restores_the_flag_when_the_attempt_went_live(tmp_path, monkeypatch):
    # Review round 1: the attempt can turn RUNNING between the unlocked
    # can_retry check and retry's locked re-check. Discard must not leave a
    # live attempt with its flag cleared.
    client, store, _ = _client(tmp_path)
    _put(store, 8, Outcome.UNKNOWN)
    _flag(client, 8)
    monkeypatch.setattr(store, "retry", lambda key, *, now: False)
    r = client.post("/automations/autopilot/discard", data={"key": f"{REPO}#8"})
    assert r.status_code == 409
    assert client.app.state.triage_store.get(f"{REPO}#8").autopilot is True


def test_discard_publishes_issue_invalidated(tmp_path):
    # Review round 1: open /issues tabs must drop the card's autopilot chip.
    client, store, _ = _client(tmp_path)
    _put(store, 8, Outcome.CLOSED)
    _flag(client, 8)
    published = []

    class _Broker:
        async def publish(self, event):
            published.append((event.topic, event.data))

    client.app.state.broker = _Broker()
    client.post("/automations/autopilot/discard", data={"key": f"{REPO}#8"})
    assert ("issue.invalidated", {"key": f"{REPO}#8"}) in published


def test_autopilot_poll_pauses_while_a_discard_confirm_is_open(tmp_path):
    # Review round 1: a poll mid-confirm reset the two-step and put Retry
    # under the second tap. The poll skips while a confirm is open.
    client, store, _ = _client(tmp_path)
    page = client.get("/automations").text
    assert "every 30s [!document.querySelector('#autopilot-section [data-confirming]')]" in page
    _put(store, 2, Outcome.LAUNCH_FAILED)
    assert ':data-confirming="confirming || null"' in client.get("/automations/autopilot").text


def test_concurrent_discard_does_not_restore_the_flag(tmp_path, monkeypatch):
    # Review round 2 (Codex): two tabs discard at once. The second request's
    # retry finds the attempt already gone; that is a completed discard, not
    # an attempt that went live, so the flag must stay off.
    client, store, _ = _client(tmp_path)
    _put(store, 8, Outcome.CLOSED)
    _flag(client, 8)
    real_retry = store.retry

    def racing_retry(key, *, now):
        real_retry(key, now=now)  # the other tab's discard lands first
        return False

    monkeypatch.setattr(store, "retry", racing_retry)
    r = client.post("/automations/autopilot/discard", data={"key": f"{REPO}#8"})
    assert r.status_code == 200
    assert client.app.state.triage_store.get(f"{REPO}#8").autopilot is False
