"""Tests for the Idea Inbox orchestrator handler."""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_hq.config import (
    Config,
    DispatchConfig,
    IdeaInboxConfig,
    ProjectConfig,
    RepoConfig,
)
from agent_hq.github import GHError
from agent_hq.idea.classifier import (
    Classification,
    ClassifierAuthError,
    ClassifierKeyRejectedError,
    Draft,
)
from agent_hq.idea.handler import (
    EmptyTextError,
    FiledIdea,
    IdeaError,
    IdeaHandler,
    NoCandidatesError,
)


def _config(threshold: float = 0.75) -> Config:
    return Config(
        repos={
            "ben-ralston/agent-hq": RepoConfig(
                owner_name="ben-ralston/agent-hq",
                local_path=Path("/tmp/agent-hq"),
                project="Agent HQ",
                description="Agent monitoring CLI.",
            ),
            "ben-ralston/scratch": RepoConfig(
                owner_name="ben-ralston/scratch",
                local_path=Path("/tmp/scratch"),
                project="Scratch",
                description=None,
            ),
        },
        projects=[
            ProjectConfig(name="Agent HQ", number=12, owner="@me"),
            ProjectConfig(name="Scratch", number=15, owner="@me"),
        ],
        worktree_root=Path("/tmp/wt"),
        dispatch=DispatchConfig(),
        idea_inbox=IdeaInboxConfig(
            model="claude-sonnet-4-6",
            confidence_threshold=threshold,
            anthropic_api_key_env="MY_KEY",
        ),
    )


def _stub_handler(config: Config, *, monkeypatch, tmp_path, sent_store=None):
    """Build an IdeaHandler with all I/O mocked. Returns (handler, mocks).

    The `set_field_value` / `fetch_field_info` mocks are patched on
    `agent_hq.github` (the source module) so a regression that re-imports
    them inside handler.py would still be caught by `assert_not_called()`.
    """
    classify_mock = MagicMock()
    draft_mock = MagicMock()
    resolve_api_key_mock = MagicMock(return_value="sk-test")
    client_for_key_mock = MagicMock()
    create_issue_mock = MagicMock()
    add_to_project_mock = MagicMock()
    set_field_value_mock = MagicMock(return_value=True)
    fetch_field_info_mock = MagicMock(return_value=None)
    fetch_recent_mock = MagicMock(return_value=[])
    transfer_issue_mock = MagicMock()
    # Default: both inbox labels ensured (reroute checks `idea-inbox` is present).
    ensure_labels_mock = MagicMock(return_value={"idea-inbox", "needs-triage"})

    monkeypatch.setattr("agent_hq.idea.handler.classify", classify_mock)
    monkeypatch.setattr("agent_hq.idea.handler.draft", draft_mock)
    monkeypatch.setattr("agent_hq.idea.handler.resolve_api_key", resolve_api_key_mock)
    monkeypatch.setattr("agent_hq.idea.handler.client_for_key", client_for_key_mock)
    monkeypatch.setattr("agent_hq.idea.handler.create_issue", create_issue_mock)
    monkeypatch.setattr("agent_hq.idea.handler.add_to_project", add_to_project_mock)
    monkeypatch.setattr("agent_hq.github.set_field_value", set_field_value_mock)
    monkeypatch.setattr("agent_hq.github.fetch_field_info", fetch_field_info_mock)
    monkeypatch.setattr("agent_hq.idea.handler.fetch_recent_issues_for_tone", fetch_recent_mock)
    monkeypatch.setattr("agent_hq.idea.handler.transfer_issue", transfer_issue_mock)
    monkeypatch.setattr("agent_hq.idea.handler.ensure_labels", ensure_labels_mock)

    handler = IdeaHandler(
        config,
        label_cache_path=tmp_path / "labels.json",
        sent_store=sent_store,
    )
    return handler, {
        "classify": classify_mock,
        "draft": draft_mock,
        "resolve_api_key": resolve_api_key_mock,
        "client_for_key": client_for_key_mock,
        "create_issue": create_issue_mock,
        "add_to_project": add_to_project_mock,
        "set_field_value": set_field_value_mock,
        "fetch_field_info": fetch_field_info_mock,
        "fetch_recent": fetch_recent_mock,
        "transfer_issue": transfer_issue_mock,
        "ensure_labels": ensure_labels_mock,
    }


class TestHandle:
    def test_forwards_api_key_command_to_resolve_api_key(self, monkeypatch, tmp_path):
        config = _config()
        config.idea_inbox = IdeaInboxConfig(
            model="claude-sonnet-4-6",
            confidence_threshold=0.75,
            anthropic_api_key_env="MY_KEY",
            anthropic_api_key_command=("dcli", "n", "agent-hq-key"),
        )
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        mocks["classify"].return_value = Classification(
            repo="ben-ralston/agent-hq", project="Agent HQ", confidence=0.92, reasoning="r"
        )
        mocks["draft"].return_value = Draft(title="T", body="B")
        mocks["create_issue"].return_value = "https://github.com/ben-ralston/agent-hq/issues/42"
        mocks["add_to_project"].return_value = "PVTI_xyz"

        handler.handle("some idea")

        # The configured fetch command must reach the resolver, not be dropped.
        mocks["resolve_api_key"].assert_called_once_with("MY_KEY", ("dcli", "n", "agent-hq-key"))

    def test_high_confidence_files_without_needs_triage(self, monkeypatch, tmp_path):
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)

        mocks["classify"].return_value = Classification(
            repo="ben-ralston/agent-hq",
            project="Agent HQ",
            confidence=0.92,
            reasoning="r",
        )
        mocks["draft"].return_value = Draft(title="T", body="B")
        mocks["create_issue"].return_value = "https://github.com/ben-ralston/agent-hq/issues/42"
        mocks["add_to_project"].return_value = "PVTI_xyz"

        result = handler.handle("Track agent state better")

        assert isinstance(result, FiledIdea)
        assert result.issue_url == "https://github.com/ben-ralston/agent-hq/issues/42"
        assert result.issue_number == 42
        assert result.repo == "ben-ralston/agent-hq"
        assert result.project == "Agent HQ"
        assert result.title == "T"
        assert result.confidence == 0.92
        assert result.needs_triage is False
        assert result.warnings == []

        # create_issue was called with idea-inbox label but NOT needs-triage
        labels = (
            mocks["create_issue"].call_args.kwargs.get("labels")
            or mocks["create_issue"].call_args.args[3]
        )
        assert "idea-inbox" in labels
        assert "needs-triage" not in labels

        # ensure_labels called with both labels (the cache may decide to skip)
        ensure_args = mocks["ensure_labels"].call_args.args
        assert ensure_args[0] == "ben-ralston/agent-hq"
        assert "idea-inbox" in ensure_args[1] and "needs-triage" in ensure_args[1]

        # priority field is left unset on new idea-inbox issues (#89)
        mocks["set_field_value"].assert_not_called()

    def test_no_default_priority_set_on_file(self, monkeypatch, tmp_path):
        """New idea-inbox issues should land in the project with priority unset."""
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)

        mocks["classify"].return_value = Classification(
            repo="ben-ralston/agent-hq",
            project="Agent HQ",
            confidence=0.9,
            reasoning="r",
        )
        mocks["draft"].return_value = Draft(title="T", body="B")
        mocks["create_issue"].return_value = "https://github.com/ben-ralston/agent-hq/issues/42"
        mocks["add_to_project"].return_value = "PVTI_xyz"

        result = handler.handle("text")

        assert result.warnings == []
        mocks["set_field_value"].assert_not_called()
        mocks["fetch_field_info"].assert_not_called()

    def test_low_confidence_adds_needs_triage(self, monkeypatch, tmp_path):
        config = _config(threshold=0.75)
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)

        mocks["classify"].return_value = Classification(
            repo="ben-ralston/scratch",
            project="Scratch",
            confidence=0.50,
            reasoning="ambiguous",
        )
        mocks["draft"].return_value = Draft(title="T", body="B")
        mocks["create_issue"].return_value = "https://github.com/ben-ralston/scratch/issues/7"
        mocks["add_to_project"].return_value = "PVTI_y"

        result = handler.handle("vague nonsense")
        assert result.needs_triage is True
        labels = mocks["create_issue"].call_args.kwargs.get("labels") or []
        assert "needs-triage" in labels
        assert "idea-inbox" in labels

    def test_partial_failure_records_warning(self, monkeypatch, tmp_path):
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)

        mocks["classify"].return_value = Classification(
            repo="ben-ralston/agent-hq",
            project="Agent HQ",
            confidence=0.9,
            reasoning="r",
        )
        mocks["draft"].return_value = Draft(title="T", body="B")
        mocks["create_issue"].return_value = "https://github.com/ben-ralston/agent-hq/issues/42"
        mocks["add_to_project"].side_effect = GHError("project-add: HTTP 502")

        result = handler.handle("text")

        # Issue still filed; warning explains the project-add failure
        assert result.issue_url.endswith("/42")
        assert any("project-add" in w for w in result.warnings)
        # set_field_value never reached because we couldn't add to project
        mocks["set_field_value"].assert_not_called()

    def test_classifier_auth_error_propagates(self, monkeypatch, tmp_path):
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        mocks["resolve_api_key"].side_effect = ClassifierAuthError("MY_KEY missing")

        with pytest.raises(ClassifierAuthError):
            handler.handle("text")

    def test_empty_text_raises_empty_text_error(self, monkeypatch, tmp_path):
        config = _config()
        handler, _ = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        with pytest.raises(EmptyTextError):
            handler.handle("   \n  ")

    def test_no_candidates_raises(self, monkeypatch, tmp_path):
        # All repos lack a `project` field
        bad = _config()
        for repo in bad.repos.values():
            object.__setattr__(repo, "project", None)
        handler, _ = _stub_handler(bad, monkeypatch=monkeypatch, tmp_path=tmp_path)
        with pytest.raises(NoCandidatesError):
            handler.handle("text")

    def test_idempotency_returns_cached_result(self, monkeypatch, tmp_path):
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)

        mocks["classify"].return_value = Classification(
            repo="ben-ralston/agent-hq",
            project="Agent HQ",
            confidence=0.9,
            reasoning="r",
        )
        mocks["draft"].return_value = Draft(title="T", body="B")
        mocks["create_issue"].return_value = "https://github.com/ben-ralston/agent-hq/issues/42"
        mocks["add_to_project"].return_value = "PVTI_xyz"

        r1 = handler.handle("text", token_hash="th", idempotency_key="k1")
        r2 = handler.handle("text", token_hash="th", idempotency_key="k1")
        assert r1.issue_url == r2.issue_url
        # create_issue only ran once — second call hit the cache
        assert mocks["create_issue"].call_count == 1

    def test_concurrent_same_key_files_once(self, monkeypatch, tmp_path):
        """AHQ-005: a same-key retry arriving while the first request is still
        inside the classify+file pipeline must NOT file a second issue. The
        retry blocks on the reservation, then returns the first's cached result.
        """
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)

        classify_entered = threading.Event()
        release = threading.Event()
        call_lock = threading.Lock()
        calls = {"n": 0}

        def blocking_classify(*args, **kwargs):
            with call_lock:
                calls["n"] += 1
                first = calls["n"] == 1
            if first:
                # First caller stalls inside the pipeline while holding the
                # reservation, simulating the slow LLM+gh section.
                classify_entered.set()
                release.wait(timeout=3)
            return Classification(
                repo="ben-ralston/agent-hq",
                project="Agent HQ",
                confidence=0.9,
                reasoning="r",
            )

        mocks["classify"].side_effect = blocking_classify
        mocks["draft"].return_value = Draft(title="T", body="B")
        mocks["create_issue"].return_value = "https://github.com/ben-ralston/agent-hq/issues/42"
        mocks["add_to_project"].return_value = "PVTI_xyz"

        results: dict[str, FiledIdea] = {}

        def call(name):
            results[name] = handler.handle("text", token_hash="th", idempotency_key="k")

        ta = threading.Thread(target=call, args=("a",))
        ta.start()
        assert classify_entered.wait(timeout=3)  # A holds the reservation

        tb = threading.Thread(target=call, args=("b",))
        tb.start()
        time.sleep(0.1)  # let B reach (and block on) the reservation
        release.set()
        ta.join(timeout=3)
        tb.join(timeout=3)

        assert results["a"].issue_url == results["b"].issue_url
        # The pipeline ran exactly once — the retry replayed the cached result.
        assert mocks["create_issue"].call_count == 1
        assert mocks["classify"].call_count == 1


class TestFiledIdeaDestination:
    def test_default_destination_is_github_in_to_dict(self):
        fi = FiledIdea(
            issue_url="u",
            issue_number=1,
            repo="o/r",
            project="P",
            title="T",
            confidence=0.9,
            needs_triage=False,
        )
        assert fi.destination == "github"
        assert fi.to_dict()["destination"] == "github"


class TestReroute:
    def test_reroute_calls_transfer_and_readds_to_project(self, monkeypatch, tmp_path):
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)

        # Record call order so we can prove labels are ensured BEFORE transfer.
        order: list[str] = []

        def _ensure(*a, **k):
            order.append("ensure")
            return {"idea-inbox", "needs-triage"}

        mocks["ensure_labels"].side_effect = _ensure

        def _transfer(*a, **k):
            order.append("transfer")
            return "https://github.com/ben-ralston/scratch/issues/9"

        mocks["transfer_issue"].side_effect = _transfer
        mocks["add_to_project"].return_value = "PVTI_new"

        result = handler.reroute(
            issue_number=42,
            src_repo="ben-ralston/agent-hq",
            dst_repo="ben-ralston/scratch",
            dst_project="Scratch",
        )
        assert result.issue_url == "https://github.com/ben-ralston/scratch/issues/9"
        assert result.warnings == []
        mocks["transfer_issue"].assert_called_once_with(
            42, "ben-ralston/agent-hq", "ben-ralston/scratch"
        )
        # AHQ-021: labels are pre-created in the DESTINATION, before transfer,
        # so GitHub preserves them (it keeps only labels that already exist in
        # the target) and the idea stays in the /inbox recent list.
        assert order == ["ensure", "transfer"]
        assert mocks["ensure_labels"].call_args.args[0] == "ben-ralston/scratch"
        assert mocks["ensure_labels"].call_args.args[1] == ["idea-inbox", "needs-triage"]
        # New project set to Scratch (#15)
        add_args = mocks["add_to_project"].call_args.args
        assert add_args[0] == 15
        # Priority is NOT auto-set on the rerouted item (#89)
        mocks["set_field_value"].assert_not_called()

    def test_reroute_add_to_project_failure_becomes_warning(self, monkeypatch, tmp_path):
        """AHQ-021: a failed project-add must surface as a warning, not be
        swallowed with bare `pass` while the client sees plain success."""
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        mocks["transfer_issue"].return_value = "https://github.com/ben-ralston/scratch/issues/9"
        mocks["add_to_project"].side_effect = GHError("boom")

        result = handler.reroute(
            issue_number=42,
            src_repo="ben-ralston/agent-hq",
            dst_repo="ben-ralston/scratch",
            dst_project="Scratch",
        )
        # Transfer still succeeded — the issue moved.
        assert result.issue_url == "https://github.com/ben-ralston/scratch/issues/9"
        assert any("project-add failed" in w for w in result.warnings)

    def test_reroute_warns_when_idea_inbox_label_not_ensured(self, monkeypatch, tmp_path):
        """AHQ-021 hardening: if `ensure_labels` couldn't create `idea-inbox` in
        the destination (auth/network failure — it's best-effort and never
        raises), GitHub drops the label on transfer and the idea vanishes from
        /inbox. That must surface as a warning, not silent success."""
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        # idea-inbox could NOT be ensured in the destination repo.
        mocks["ensure_labels"].return_value = {"needs-triage"}
        mocks["transfer_issue"].return_value = "https://github.com/ben-ralston/scratch/issues/9"
        mocks["add_to_project"].return_value = "PVTI_new"

        result = handler.reroute(
            issue_number=42,
            src_repo="ben-ralston/agent-hq",
            dst_repo="ben-ralston/scratch",
            dst_project="Scratch",
        )
        assert result.issue_url == "https://github.com/ben-ralston/scratch/issues/9"
        assert any("idea-inbox" in w for w in result.warnings)

    def test_reroute_unknown_project_becomes_warning(self, monkeypatch, tmp_path):
        """A destination whose project isn't in the projects list warns rather
        than silently skipping the add."""
        config = _config()
        config.repos["ben-ralston/ghost"] = RepoConfig(
            owner_name="ben-ralston/ghost",
            local_path=Path("/tmp/ghost"),
            project="Ghost",  # intentionally absent from config.projects
            description=None,
        )
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        mocks["transfer_issue"].return_value = "https://github.com/ben-ralston/ghost/issues/3"

        result = handler.reroute(
            issue_number=1,
            src_repo="ben-ralston/agent-hq",
            dst_repo="ben-ralston/ghost",
            dst_project="Ghost",
        )
        assert any("unknown project" in w for w in result.warnings)
        mocks["add_to_project"].assert_not_called()

    def test_reroute_rejects_unknown_dst_repo(self, monkeypatch, tmp_path):
        config = _config()
        handler, _ = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        with pytest.raises(NoCandidatesError, match="not configured"):
            handler.reroute(
                issue_number=42,
                src_repo="ben-ralston/agent-hq",
                dst_repo="other/unknown",
                dst_project="Whatever",
            )

    def test_reroute_rejects_unconfigured_src_repo(self, monkeypatch, tmp_path):
        """The transfer acts on src_repo with the host's gh credentials; it
        must be one of the configured repos (#263 F5)."""
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        with pytest.raises(IdeaError, match="not configured"):
            handler.reroute(
                issue_number=42,
                src_repo="victim-org/private",
                dst_repo="ben-ralston/scratch",
                dst_project="Scratch",
            )
        mocks["transfer_issue"].assert_not_called()


class TestItemKey:
    """item_key is threaded through FiledIdea and RecentIdea. #128"""

    def test_list_recent_github_item_has_item_key(self, monkeypatch, tmp_path):
        """RecentIdea from a GitHub issue exposes item_key == 'repo#number'."""
        config = _config()
        handler, _ = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        fetch_idea_mock = MagicMock(
            side_effect=lambda repo, limit=10: {
                "ben-ralston/agent-hq": [
                    {
                        "number": 42,
                        "title": "Add foo",
                        "url": "https://github.com/ben-ralston/agent-hq/issues/42",
                        "labels": [{"name": "idea-inbox"}],
                        "createdAt": "2026-05-06T01:00:00Z",
                    }
                ],
            }.get(repo, [])
        )
        monkeypatch.setattr("agent_hq.idea.handler.fetch_idea_inbox_issues", fetch_idea_mock)

        recent = handler.list_recent()
        github_item = next(r for r in recent if r.issue_number == 42)
        assert github_item.item_key == "ben-ralston/agent-hq#42"

    def test_list_recent_github_item_carries_state(self, monkeypatch, tmp_path):
        """RecentIdea carries the issue's upper-cased OPEN/CLOSED state so the
        recent list can render a closed/merged idea non-dispatchable. #128"""
        config = _config()
        handler, _ = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        fetch_idea_mock = MagicMock(
            side_effect=lambda repo, limit=10: {
                "ben-ralston/agent-hq": [
                    {
                        "number": 42,
                        "title": "Open one",
                        "url": "u1",
                        "labels": [{"name": "idea-inbox"}],
                        "createdAt": "2026-05-06T02:00:00Z",
                        "state": "OPEN",
                    },
                    {
                        "number": 41,
                        "title": "Merged one",
                        "url": "u2",
                        "labels": [{"name": "idea-inbox"}],
                        "createdAt": "2026-05-06T01:00:00Z",
                        "state": "CLOSED",
                    },
                ],
            }.get(repo, [])
        )
        monkeypatch.setattr("agent_hq.idea.handler.fetch_idea_inbox_issues", fetch_idea_mock)

        recent = {r.issue_number: r for r in handler.list_recent()}
        assert recent[42].state == "OPEN"
        assert recent[41].state == "CLOSED"


class TestListRecent:
    def test_list_recent_aggregates_across_repos(self, monkeypatch, tmp_path):
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        fetch_idea_mock = MagicMock(
            side_effect=lambda repo, limit=10: {
                "ben-ralston/agent-hq": [
                    {
                        "number": 42,
                        "title": "Add foo",
                        "url": "https://github.com/ben-ralston/agent-hq/issues/42",
                        "labels": [{"name": "idea-inbox"}],
                        "createdAt": "2026-05-06T01:00:00Z",
                    }
                ],
                "ben-ralston/scratch": [
                    {
                        "number": 7,
                        "title": "Try thing",
                        "url": "https://github.com/ben-ralston/scratch/issues/7",
                        "labels": [
                            {"name": "idea-inbox"},
                            {"name": "needs-triage"},
                        ],
                        "createdAt": "2026-05-06T02:00:00Z",
                    }
                ],
            }.get(repo, [])
        )
        monkeypatch.setattr("agent_hq.idea.handler.fetch_idea_inbox_issues", fetch_idea_mock)

        recent = handler.list_recent()
        # Sorted newest-first by createdAt
        assert [r.issue_number for r in recent] == [7, 42]
        assert recent[0].repo == "ben-ralston/scratch"
        assert recent[0].project == "Scratch"
        assert recent[0].needs_triage is True
        assert recent[1].needs_triage is False

    def test_list_recent_caches_for_ttl(self, monkeypatch, tmp_path):
        config = _config()
        handler, _ = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        fetch_idea_mock = MagicMock(return_value=[])
        monkeypatch.setattr("agent_hq.idea.handler.fetch_idea_inbox_issues", fetch_idea_mock)

        handler.list_recent()
        handler.list_recent()
        # Two configured repos × first call only — second call hits cache
        assert fetch_idea_mock.call_count == 2

    def test_list_recent_fetches_gh_cap_not_display_limit(self, monkeypatch, tmp_path):
        # The per-repo `gh` fetch always asks for _GH_FETCH_LIMIT issues — the
        # display `limit` only truncates the merged list. One gh call per repo
        # costs the same regardless of --limit, and a deep fetch lets "Show
        # more" page into the cache without refetching. #128 follow-up
        from agent_hq.idea.handler import _GH_FETCH_LIMIT

        config = _config()
        handler, _ = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        fetch_idea_mock = MagicMock(return_value=[])
        monkeypatch.setattr("agent_hq.idea.handler.fetch_idea_inbox_issues", fetch_idea_mock)

        handler.list_recent(limit=5)

        for call in fetch_idea_mock.call_args_list:
            assert call.kwargs.get("limit") == _GH_FETCH_LIMIT

    def test_list_recent_deeper_limit_served_from_cache(self, monkeypatch, tmp_path):
        # A "Show more" request (bigger limit, within the fetch cap) must be
        # answered from the cached deep fetch — no new gh shellouts.
        config = _config()
        handler, _ = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        issues = [
            {
                "number": n,
                "title": f"Idea {n}",
                "url": f"https://github.com/ben-ralston/agent-hq/issues/{n}",
                "labels": [{"name": "idea-inbox"}],
                "createdAt": f"2026-05-06T{n:02d}:00:00Z",
            }
            for n in range(1, 16)
        ]
        fetch_idea_mock = MagicMock(
            side_effect=lambda repo, limit=10: issues if repo == "ben-ralston/agent-hq" else []
        )
        monkeypatch.setattr("agent_hq.idea.handler.fetch_idea_inbox_issues", fetch_idea_mock)

        assert len(handler.list_recent(limit=10)) == 10
        first_call_count = fetch_idea_mock.call_count
        assert len(handler.list_recent(limit=15)) == 15
        assert fetch_idea_mock.call_count == first_call_count

    def test_list_recent_force_bypasses_cache(self, monkeypatch, tmp_path):
        config = _config()
        handler, _ = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        fetch_idea_mock = MagicMock(return_value=[])
        monkeypatch.setattr("agent_hq.idea.handler.fetch_idea_inbox_issues", fetch_idea_mock)

        handler.list_recent()
        handler.list_recent(force=True)
        # 2 repos × 2 calls = 4
        assert fetch_idea_mock.call_count == 4

    def test_handle_invalidates_recent_cache(self, monkeypatch, tmp_path):
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        fetch_idea_mock = MagicMock(return_value=[])
        monkeypatch.setattr("agent_hq.idea.handler.fetch_idea_inbox_issues", fetch_idea_mock)

        mocks["classify"].return_value = Classification(
            repo="ben-ralston/agent-hq", project="Agent HQ", confidence=0.9, reasoning="r"
        )
        mocks["draft"].return_value = Draft(title="T", body="B")
        mocks["create_issue"].return_value = "https://github.com/ben-ralston/agent-hq/issues/42"
        mocks["add_to_project"].return_value = "PVTI"

        handler.list_recent()  # populate cache: 2 repo fetches
        assert fetch_idea_mock.call_count == 2
        handler.handle("file something new")  # write → invalidate
        handler.list_recent()  # cache miss → refetch: +2
        assert fetch_idea_mock.call_count == 4

    def test_reroute_invalidates_recent_cache(self, monkeypatch, tmp_path):
        config = _config()
        handler, mocks = _stub_handler(config, monkeypatch=monkeypatch, tmp_path=tmp_path)
        fetch_idea_mock = MagicMock(return_value=[])
        monkeypatch.setattr("agent_hq.idea.handler.fetch_idea_inbox_issues", fetch_idea_mock)
        mocks["transfer_issue"].return_value = "https://github.com/ben-ralston/scratch/issues/9"

        handler.list_recent()  # populate cache: 2 repo fetches
        assert fetch_idea_mock.call_count == 2
        handler.reroute(
            issue_number=42,
            src_repo="ben-ralston/agent-hq",
            dst_repo="ben-ralston/scratch",
            dst_project="Scratch",
        )
        handler.list_recent()  # cache miss → refetch: +2
        assert fetch_idea_mock.call_count == 4


class TestSentRecording:
    """handle() records every non-empty, non-replay submission durably. #140"""

    def _handler(self, monkeypatch, tmp_path, config=None):
        from agent_hq.idea.sent_store import SentMessageStore

        store = SentMessageStore(tmp_path / "sent.jsonl")
        handler, mocks = _stub_handler(
            config or _config(), monkeypatch=monkeypatch, tmp_path=tmp_path, sent_store=store
        )
        return handler, mocks, store

    def _arm_success(self, mocks):
        mocks["classify"].return_value = Classification(
            repo="ben-ralston/agent-hq", project="Agent HQ", confidence=0.92, reasoning="r"
        )
        mocks["draft"].return_value = Draft(title="Drafted", body="B")
        mocks["create_issue"].return_value = "https://github.com/ben-ralston/agent-hq/issues/42"
        mocks["add_to_project"].return_value = "PVTI_xyz"

    def test_success_records_filed_with_audit_fields(self, monkeypatch, tmp_path):
        handler, mocks, store = self._handler(monkeypatch, tmp_path)
        self._arm_success(mocks)
        mocks["add_to_project"].side_effect = GHError("proj down")  # → warning

        handler.handle("raw idea text", token_hash="th", idempotency_key="key-1")

        records = store.list()
        assert len(records) == 1
        rec = records[0]
        assert rec.id == "key-1"
        assert rec.text == "raw idea text"
        assert rec.status == "filed"
        assert rec.destination == "github"
        assert rec.issue_url == "https://github.com/ben-ralston/agent-hq/issues/42"
        assert rec.title == "Drafted"
        assert rec.confidence == 0.92
        assert rec.needs_triage is False
        assert rec.warnings and "project-add failed" in rec.warnings[0]

    def test_github_failure_records_error_and_reraises(self, monkeypatch, tmp_path):
        handler, mocks, store = self._handler(monkeypatch, tmp_path)
        self._arm_success(mocks)
        mocks["create_issue"].side_effect = GHError("rate limited")

        with pytest.raises(GHError):
            handler.handle("doomed idea", idempotency_key="key-2")

        rec = store.list()[0]
        assert rec.status == "error"
        assert rec.text == "doomed idea"
        assert "rate limited" in rec.error

    def test_no_candidates_records_error(self, monkeypatch, tmp_path):
        bad = _config()
        for repo in bad.repos.values():
            object.__setattr__(repo, "project", None)
        handler, _mocks, store = self._handler(monkeypatch, tmp_path, config=bad)

        with pytest.raises(NoCandidatesError):
            handler.handle("text", idempotency_key="key-3")

        rec = store.list()[0]
        assert rec.status == "error"
        assert "projects.yaml" in rec.error

    def test_empty_text_records_nothing(self, monkeypatch, tmp_path):
        handler, _mocks, store = self._handler(monkeypatch, tmp_path)
        with pytest.raises(EmptyTextError):
            handler.handle("   \n ")
        assert store.list() == []

    def test_idempotency_replay_records_nothing_new(self, monkeypatch, tmp_path):
        handler, mocks, store = self._handler(monkeypatch, tmp_path)
        self._arm_success(mocks)

        handler.handle("text", token_hash="th", idempotency_key="k1")
        handler.handle("text", token_hash="th", idempotency_key="k1")

        # One folded record, and no second pending line was appended.
        assert len(store.list()) == 1
        raw_lines = (tmp_path / "sent.jsonl").read_text().splitlines()
        assert len(raw_lines) == 2  # pending + filed from the first call only

    def test_missing_idempotency_key_generates_sent_id(self, monkeypatch, tmp_path):
        handler, mocks, store = self._handler(monkeypatch, tmp_path)
        self._arm_success(mocks)

        handler.handle("text")

        rec = store.list()[0]
        assert rec.status == "filed"
        assert len(rec.id) == 32  # uuid4().hex


class TestListRecentSentRows:
    """list_recent surfaces failed/interrupted sends durably. #140"""

    def _handler(self, monkeypatch, tmp_path, github_issues=None):
        from agent_hq.idea.sent_store import SentMessageStore

        store = SentMessageStore(tmp_path / "sent.jsonl")
        handler, _mocks = _stub_handler(
            _config(), monkeypatch=monkeypatch, tmp_path=tmp_path, sent_store=store
        )
        monkeypatch.setattr(
            "agent_hq.idea.handler.fetch_idea_inbox_issues",
            MagicMock(side_effect=lambda repo, limit=10: (github_issues or {}).get(repo, [])),
        )
        return handler, store

    @staticmethod
    def _seed_line(tmp_path, **obj):
        import json

        defaults = {
            "id": "x",
            "text": "seeded text",
            "created_at": "2026-06-10T00:00:00+00:00",
            "status": "pending",
        }
        defaults.update(obj)
        with open(tmp_path / "sent.jsonl", "a") as f:
            f.write(json.dumps(defaults) + "\n")

    def test_error_rows_surface_with_raw_text(self, monkeypatch, tmp_path):
        handler, store = self._handler(monkeypatch, tmp_path)
        store.record_pending("first line\nmore detail", sent_id="e1")
        store.record_outcome("e1", status="error", error="github: rate limited")

        rows = [r for r in handler.list_recent() if r.sent_id]
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "error"
        assert row.sent_id == "e1"
        assert row.raw_text == "first line\nmore detail"
        assert row.title == "first line"
        assert row.issue_url == ""

    def test_stale_pending_surfaces_as_interrupted(self, monkeypatch, tmp_path):
        handler, _store = self._handler(monkeypatch, tmp_path)
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        self._seed_line(tmp_path, id="p2", text="lost send", created_at=old)

        rows = [r for r in handler.list_recent() if r.sent_id]
        assert len(rows) == 1
        assert rows[0].status == "interrupted"
        assert rows[0].raw_text == "lost send"

    def test_dismissed_rows_absent(self, monkeypatch, tmp_path):
        handler, store = self._handler(monkeypatch, tmp_path)
        store.record_pending("t", sent_id="d1")
        store.record_outcome("d1", status="error", error="boom")
        store.dismiss("d1")
        assert [r for r in handler.list_recent() if r.sent_id] == []

    def test_filed_records_contribute_no_row(self, monkeypatch, tmp_path):
        # The real GitHub row already represents a filed send.
        handler, store = self._handler(monkeypatch, tmp_path)
        store.record_pending("t", sent_id="f1")
        store.record_outcome("f1", status="filed", destination="github")
        assert [r for r in handler.list_recent() if r.sent_id] == []

    def test_failure_rows_immune_to_limit_truncation(self, monkeypatch, tmp_path):
        # 12 newer GitHub issues would push an old failed row out of
        # items[:10] — its raw text would then be unreachable forever.
        issues = {
            "ben-ralston/agent-hq": [
                {
                    "number": n,
                    "title": f"Issue {n}",
                    "url": f"https://github.com/ben-ralston/agent-hq/issues/{n}",
                    "labels": [{"name": "idea-inbox"}],
                    "createdAt": f"2026-06-09T{n:02d}:00:00Z",
                }
                for n in range(1, 13)
            ]
        }
        handler, _store = self._handler(monkeypatch, tmp_path, github_issues=issues)
        self._seed_line(
            tmp_path,
            id="old-fail",
            text="old failed send",
            created_at="2026-06-01T00:00:00+00:00",
            status="error",
            error="boom",
        )

        recent = handler.list_recent()
        assert any(r.sent_id == "old-fail" for r in recent)

    def test_error_rows_carry_error_message(self, monkeypatch, tmp_path):
        handler, store = self._handler(monkeypatch, tmp_path)
        store.record_pending("t", sent_id="e2")
        store.record_outcome("e2", status="error", error="github: rate limited")
        row = next(r for r in handler.list_recent() if r.sent_id == "e2")
        assert row.error == "github: rate limited"

    def test_dismiss_sent_delegates_to_store(self, monkeypatch, tmp_path):
        handler, store = self._handler(monkeypatch, tmp_path)
        store.record_pending("t", sent_id="d2")
        store.record_outcome("d2", status="error", error="x")
        assert handler.dismiss_sent("d2") is True
        assert handler.dismiss_sent("unknown") is False

    def test_young_pending_surfaces_as_pending_row(self, monkeypatch, tmp_path):
        """An in-flight send must stay visible across navigation/reload: the
        partial renders it as a 'Sending…' row; the client hides it only on
        the page that owns the live bubble. (#140 follow-up)"""
        handler, store = self._handler(monkeypatch, tmp_path)
        store.record_pending("in flight", sent_id="p3")

        rows = [r for r in handler.list_recent() if r.sent_id]
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].raw_text == "in flight"
        assert rows[0].sent_id == "p3"


def _arm_github_filing(mocks) -> None:
    mocks["classify"].return_value = Classification(
        repo="ben-ralston/agent-hq", project="Agent HQ", confidence=0.92, reasoning="r"
    )
    mocks["draft"].return_value = Draft(title="T", body="B")
    mocks["create_issue"].return_value = "https://github.com/ben-ralston/agent-hq/issues/42"
    mocks["add_to_project"].return_value = "PVTI_xyz"


class TestApiKeyCache:
    """#300: the key is fetched once per process, not on every capture — a
    password-manager fetch prompts on the Mac once its vault locks."""

    def test_key_resolved_once_across_captures(self, monkeypatch, tmp_path):
        handler, mocks = _stub_handler(_config(), monkeypatch=monkeypatch, tmp_path=tmp_path)
        _arm_github_filing(mocks)

        handler.handle("first idea", idempotency_key="k1")
        handler.handle("second idea", idempotency_key="k2")

        assert mocks["resolve_api_key"].call_count == 1
        assert mocks["create_issue"].call_count == 2
        mocks["client_for_key"].assert_called_with("sk-test")

    def test_resolve_failure_is_not_cached(self, monkeypatch, tmp_path):
        handler, mocks = _stub_handler(_config(), monkeypatch=monkeypatch, tmp_path=tmp_path)
        _arm_github_filing(mocks)
        mocks["resolve_api_key"].side_effect = [ClassifierAuthError("locked"), "sk-test"]

        with pytest.raises(ClassifierAuthError):
            handler.handle("first idea", idempotency_key="k1")
        result = handler.handle("second idea", idempotency_key="k2")

        assert result.issue_number == 42
        assert mocks["resolve_api_key"].call_count == 2

    def test_rejected_key_refetched_and_retried_once(self, monkeypatch, tmp_path):
        handler, mocks = _stub_handler(_config(), monkeypatch=monkeypatch, tmp_path=tmp_path)
        _arm_github_filing(mocks)
        mocks["resolve_api_key"].side_effect = ["sk-old", "sk-new"]
        classification = mocks["classify"].return_value
        mocks["classify"].return_value = None
        mocks["classify"].side_effect = [ClassifierKeyRejectedError("401"), classification]

        result = handler.handle("idea", idempotency_key="k1")

        assert result.issue_number == 42
        assert mocks["resolve_api_key"].call_count == 2
        assert [c.args[0] for c in mocks["client_for_key"].call_args_list] == [
            "sk-old",
            "sk-new",
        ]
        mocks["create_issue"].assert_called_once()
        # The refreshed key is what the next capture reuses.
        mocks["classify"].side_effect = None
        mocks["classify"].return_value = classification
        handler.handle("another", idempotency_key="k2")
        assert mocks["resolve_api_key"].call_count == 2

    def test_rejected_key_on_draft_retries_without_double_filing(self, monkeypatch, tmp_path):
        handler, mocks = _stub_handler(_config(), monkeypatch=monkeypatch, tmp_path=tmp_path)
        _arm_github_filing(mocks)
        mocks["resolve_api_key"].side_effect = ["sk-old", "sk-new"]
        drafted = mocks["draft"].return_value
        mocks["draft"].side_effect = [ClassifierKeyRejectedError("401"), drafted]

        handler.handle("idea", idempotency_key="k1")

        assert mocks["resolve_api_key"].call_count == 2
        mocks["create_issue"].assert_called_once()

    def test_second_rejection_raises_and_files_nothing(self, monkeypatch, tmp_path):
        from agent_hq.idea.sent_store import SentMessageStore

        sent_store = SentMessageStore(tmp_path / "sent.jsonl")
        handler, mocks = _stub_handler(
            _config(), monkeypatch=monkeypatch, tmp_path=tmp_path, sent_store=sent_store
        )
        _arm_github_filing(mocks)
        mocks["resolve_api_key"].side_effect = ["sk-old", "sk-new", "sk-newer", "sk-newest"]
        mocks["classify"].side_effect = ClassifierKeyRejectedError("401")

        with pytest.raises(ClassifierAuthError):
            handler.handle("idea", idempotency_key="k1")

        assert mocks["resolve_api_key"].call_count == 2
        assert mocks["classify"].call_count == 2
        mocks["create_issue"].assert_not_called()
        rec = sent_store.list()[0]
        assert rec.status == "error"
        assert "401" in rec.error

        # The retry's key was rejected too, so it must not stay cached: the
        # next capture fetches afresh instead of starting with a known-bad key.
        with pytest.raises(ClassifierAuthError):
            handler.handle("idea 2", idempotency_key="k2")
        assert mocks["client_for_key"].call_args_list[2].args[0] == "sk-newer"

    def test_refetched_same_key_does_not_retry(self, monkeypatch, tmp_path):
        """A re-fetch that returns the rejected key again (env var set, store
        not yet updated) would make a call certain to fail — don't."""
        handler, mocks = _stub_handler(_config(), monkeypatch=monkeypatch, tmp_path=tmp_path)
        _arm_github_filing(mocks)
        mocks["classify"].side_effect = ClassifierKeyRejectedError("401")

        with pytest.raises(ClassifierKeyRejectedError):
            handler.handle("idea", idempotency_key="k1")

        assert mocks["resolve_api_key"].call_count == 2
        assert mocks["classify"].call_count == 1

    def test_queued_captures_share_a_failed_fetch(self, monkeypatch, tmp_path):
        """Captures queued behind a failing fetch (a locked vault times out
        after 10s) share its failure instead of each running the command in
        turn — N queued captures must not wait N x timeout."""
        handler, mocks = _stub_handler(_config(), monkeypatch=monkeypatch, tmp_path=tmp_path)
        _arm_github_filing(mocks)

        def slow_fail(*_args):
            time.sleep(0.1)
            raise ClassifierAuthError("timed out")

        mocks["resolve_api_key"].side_effect = slow_fail
        errors: list[BaseException] = []

        def capture(i: int) -> None:
            try:
                handler.handle(f"idea {i}", idempotency_key=f"k{i}")
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=capture, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
            time.sleep(0.01)
        for t in threads:
            t.join()

        assert len(errors) == 4
        assert all(isinstance(e, ClassifierAuthError) for e in errors)
        assert mocks["resolve_api_key"].call_count == 1

        # A capture arriving after the failure fetches again (not cached).
        mocks["resolve_api_key"].side_effect = None
        mocks["resolve_api_key"].return_value = "sk-test"
        handler.handle("later", idempotency_key="k9")
        assert mocks["resolve_api_key"].call_count == 2

    def test_concurrent_captures_resolve_once(self, monkeypatch, tmp_path):
        handler, mocks = _stub_handler(_config(), monkeypatch=monkeypatch, tmp_path=tmp_path)
        _arm_github_filing(mocks)

        def slow_resolve(*_args):
            time.sleep(0.05)
            return "sk-test"

        mocks["resolve_api_key"].side_effect = slow_resolve
        errors: list[BaseException] = []

        def capture(i: int) -> None:
            try:
                handler.handle(f"idea {i}", idempotency_key=f"k{i}")
            except BaseException as e:  # pragma: no cover - surfaced below
                errors.append(e)

        threads = [threading.Thread(target=capture, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert mocks["resolve_api_key"].call_count == 1
