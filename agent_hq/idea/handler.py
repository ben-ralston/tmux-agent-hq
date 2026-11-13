"""Idea Inbox orchestrator: classify → tone-seed → draft → create → label.

Single class `IdeaHandler` — instantiate once per FastAPI app, call `.handle(text)`
from the POST route, `.reroute(...)` from the reroute route. All I/O is in module-
level helpers (classifier, github helpers, ensure_labels) so tests can monkeypatch
them.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent_hq.config import Config
from agent_hq.github import (
    GHError,
    add_to_project,
    create_issue,
    fetch_idea_inbox_issues,
    fetch_recent_issues_for_tone,
    transfer_issue,
)
from agent_hq.idea.classifier import (
    Candidate,
    Classification,
    ClassifierAuthError,
    ClassifierError,
    ClassifierKeyRejectedError,
    Draft,
    classify,
    client_for_key,
    draft,
    resolve_api_key,
)
from agent_hq.idea.idempotency import IdempotencyCache
from agent_hq.idea.labels import DEFAULT_CACHE_PATH, ensure_labels
from agent_hq.idea.sent_store import SentMessageStore

_LABELS = ["idea-inbox", "needs-triage"]
_RECENT_LIMIT = 10
_RECENT_TTL_SECONDS = 30.0
# Per-repo `gh issue list --limit` for the recent-ideas fetch. Deliberately
# deeper than any one page shown in the UI: the cost of a gh shellout is the
# call itself, not the row count, and the deep cache lets "Show more" page
# further back without refetching. Must be >= the web route's
# _MAX_RECENT_LIMIT so a single-repo config can serve every page the UI can
# request. #128 follow-up
_GH_FETCH_LIMIT = 100
# A pending sent-record older than this is dead (handle() is bounded by the
# LLM + `gh` calls; the CLI client times out at 60s) — surface it as
# "interrupted". Younger pendings render as "Sending…" rows so an in-flight
# send survives navigating away; the page owning the live bubble hides its
# matching row client-side (htmx:afterSwap dedup in inbox.html). #140
_INTERRUPTED_AFTER_SECONDS = 120.0
_ISSUE_NUM_RE = re.compile(r"/issues/(\d+)")


class IdeaError(Exception):
    """Base for caller-visible Idea Inbox errors."""


class EmptyTextError(IdeaError):
    """Raised when the submitted text is empty/whitespace."""


class NoCandidatesError(IdeaError):
    """Raised when no repo in config has a `project:` field, or a reroute target is unknown."""


@dataclass
class FiledIdea:
    issue_url: str
    issue_number: int
    repo: str
    project: str
    title: str
    confidence: float
    needs_triage: bool
    warnings: list[str] = field(default_factory=list)
    destination: str = "github"

    def to_dict(self) -> dict:
        return {
            "issue_url": self.issue_url,
            "issue_number": self.issue_number,
            "repo": self.repo,
            "project": self.project,
            "title": self.title,
            "confidence": self.confidence,
            "needs_triage": self.needs_triage,
            "warnings": list(self.warnings),
            "destination": self.destination,
        }


@dataclass
class ReroutedIdea:
    """Result of a reroute: the moved issue's new URL plus any non-fatal
    warnings (e.g. a failed project-add) the client should surface. #181"""

    issue_url: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class RecentIdea:
    """One row in the /inbox recent list. Fewer fields than FiledIdea —
    confidence and warnings aren't recoverable from GitHub after the fact."""

    issue_url: str
    issue_number: int
    repo: str
    project: str | None
    title: str
    needs_triage: bool
    created_at: str = ""
    # "github" → a real issue (issue_url/number/repo populated); "sent" → a
    # failed/interrupted send from the durable sent-message log
    # (sent_id/status/raw_text populated). #140
    destination: str = "github"
    # Stable key for dispatch targeting: "repo#number". #128
    item_key: str = ""
    # GitHub issue state, upper-cased ("OPEN" / "CLOSED"). A CLOSED/merged
    # idea is shown muted with no dispatch button so completed work can't be
    # re-dispatched. #128
    state: str = ""
    sent_id: str = ""
    status: str = ""  # "" | "pending" | "error" | "interrupted" (sent rows only)
    raw_text: str = ""
    error: str = ""


class IdeaHandler:
    def __init__(
        self,
        config: Config,
        *,
        label_cache_path: Path | None = None,
        idempotency_cache: IdempotencyCache | None = None,
        sent_store: SentMessageStore | None = None,
    ):
        self._config = config
        self._label_cache_path = label_cache_path or DEFAULT_CACHE_PATH
        self._idempotency = idempotency_cache or IdempotencyCache()
        self._sent_store = sent_store or SentMessageStore()
        self._recent_cache: list[RecentIdea] | None = None
        self._recent_cache_ts: float = 0.0
        # The Anthropic key, resolved once per process (#300). The fetch command
        # is typically a password-manager CLI that prompts on the Mac once its
        # vault locks, so re-running it per capture breaks capture from the
        # phone. Dropped only when the API rejects the key.
        self._api_key: str | None = None
        self._api_key_lock = threading.Lock()
        # The last failed fetch and when it finished (monotonic). Captures that
        # queued on the lock behind it share the failure rather than each
        # re-running a command that just failed (a locked vault times out
        # after 10s — serial retries would stack N x 10s).
        self._api_key_error: ClassifierAuthError | None = None
        self._api_key_error_at = 0.0

    # --- public ----------------------------------------------------------------

    def handle(
        self,
        text: str,
        *,
        token_hash: str = "",
        idempotency_key: str | None = None,
    ) -> FiledIdea:
        text = (text or "").strip()
        if not text:
            raise EmptyTextError("text is empty")

        # Serialize on the idempotency key so a same-key retry that arrives
        # while this request is still inside the (60s+) classify+file pipeline
        # blocks here, then finds the cached result below instead of filing a
        # duplicate issue (#181, AHQ-005). The cache read is re-done inside the
        # reservation — the winning request caches its result before releasing.
        with self._idempotency.reserve(token_hash, idempotency_key):
            cached = self._idempotency.get(token_hash, idempotency_key)
            if cached is not None:
                return cached

            # Durable sent-message record (#140). The id doubles as the client's
            # idempotency key so a retry's lines supersede the failed attempt in
            # the store instead of duplicating it.
            sent_id = idempotency_key or uuid.uuid4().hex
            self._sent_store.record_pending(text, sent_id=sent_id)
            try:
                result = self._classify_and_file(text)
            except Exception as e:
                self._sent_store.record_outcome(sent_id, status="error", error=str(e))
                raise
            self._sent_store.record_outcome(
                sent_id,
                status="filed",
                destination=result.destination,
                issue_url=result.issue_url,
                issue_number=result.issue_number,
                repo=result.repo,
                project=result.project,
                title=result.title,
                warnings=list(result.warnings),
                confidence=result.confidence,
                needs_triage=result.needs_triage,
            )

            self._idempotency.set(token_hash, idempotency_key, result)
            return result

    def _classify_and_file(self, text: str) -> FiledIdea:
        """The classify → draft → file pipeline (everything that can fail)."""
        candidates = self._candidates()
        if not candidates:
            raise NoCandidatesError("no repos with a `project:` field in projects.yaml")

        # Nothing is filed until classify (+ draft) succeed, so retrying them
        # once with a re-fetched key after a 401 can't double-file.
        key = self._resolve_api_key()
        try:
            classification, drafted = self._classify_and_draft(text, candidates, key)
        except ClassifierKeyRejectedError:
            self._forget_api_key(key)
            fresh = self._resolve_api_key()
            if fresh == key:
                # The source still hands out the rejected key (env var set, or
                # the store not yet updated): a retry is certain to fail.
                self._forget_api_key(key)
                raise
            try:
                classification, drafted = self._classify_and_draft(text, candidates, fresh)
            except ClassifierKeyRejectedError:
                # Known bad too — don't let the next capture start with it.
                self._forget_api_key(fresh)
                raise

        needs_triage = classification.confidence < self._config.idea_inbox.confidence_threshold
        result = self._file(classification, drafted, needs_triage=needs_triage)
        self._invalidate_recent_cache()
        return result

    def _classify_and_draft(
        self,
        text: str,
        candidates: list[Candidate],
        api_key: str,
    ) -> tuple[Classification, Draft]:
        """Classify, then draft."""
        client = client_for_key(api_key)
        model = self._config.idea_inbox.model
        classification = classify(text, candidates, model=model, client=client)
        recent = fetch_recent_issues_for_tone(classification.repo, limit=_RECENT_LIMIT)
        return classification, draft(text, classification.repo, recent, model=model, client=client)

    def _resolve_api_key(self) -> str:
        """Return the cached key, fetching it once under a lock if absent.

        Concurrent captures wait on one fetch rather than each spawning the
        command. A failed fetch isn't cached, so the next capture retries.
        """
        queued_at = time.monotonic()
        with self._api_key_lock:
            if self._api_key is not None:
                return self._api_key
            if self._api_key_error is not None and self._api_key_error_at >= queued_at:
                raise self._api_key_error
            try:
                self._api_key = resolve_api_key(
                    self._config.idea_inbox.anthropic_api_key_env,
                    self._config.idea_inbox.anthropic_api_key_command,
                )
            except ClassifierAuthError as e:
                self._api_key_error = e
                self._api_key_error_at = time.monotonic()
                raise
            self._api_key_error = None
            return self._api_key

    def _forget_api_key(self, rejected: str) -> None:
        """Drop the cached key — unless a concurrent capture already replaced it."""
        with self._api_key_lock:
            if self._api_key == rejected:
                self._api_key = None

    def list_recent(self, *, limit: int = 10, force: bool = False) -> list[RecentIdea]:
        """Recently filed ideas: `idea-inbox` GitHub issues + sent-log rows.

        Only the GitHub aggregation (slow `gh` shellouts) is cached for ~30s.
        """
        items = list(self._github_recent(force=force))
        items.sort(key=lambda i: i.created_at, reverse=True)
        items = items[:limit]
        # Sent rows merge AFTER the truncation so a burst of newer issues can
        # never push one out of view — a failed row's raw text would
        # otherwise be unreachable. Read fresh each call.
        items.extend(self._sent_rows())
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items

    def _sent_rows(self) -> list[RecentIdea]:
        """Sent-log rows worth showing: in-flight pendings, errors, and stale
        ("interrupted") pendings.

        Filed records contribute nothing — the real GitHub row
        already represents them. Young pendings render as "Sending…" rows so
        an in-flight send survives navigating away mid-classification; the
        page that owns the live bubble hides its matching row client-side.
        """
        now = datetime.now(timezone.utc)
        rows: list[RecentIdea] = []
        for rec in self._sent_store.list():
            status = rec.status
            if status == "pending":
                if _age_seconds(rec.created_at, now) >= _INTERRUPTED_AFTER_SECONDS:
                    status = "interrupted"
            elif status != "error":
                continue
            rows.append(
                RecentIdea(
                    issue_url="",
                    issue_number=0,
                    repo="",
                    project=None,
                    title=_split_capture(rec.text)[0],
                    needs_triage=False,
                    created_at=rec.created_at,
                    destination="sent",
                    sent_id=rec.id,
                    status=status,
                    raw_text=rec.text,
                    error=rec.error,
                )
            )
        return rows

    def dismiss_sent(self, sent_id: str) -> bool:
        """Terminally dismiss a sent-log row. False for unknown ids. #140"""
        return self._sent_store.dismiss(sent_id)

    def _github_recent(self, *, force: bool) -> list[RecentIdea]:
        """The cached half of ``list_recent``: `idea-inbox` issues across repos.

        Cached in-process for ~30s so the htmx auto-refresh on /inbox doesn't
        beat on `gh` constantly. Always fetches ``_GH_FETCH_LIMIT`` per repo —
        the caller's display limit only truncates the merged list.
        """
        now = time.monotonic()
        if (
            not force
            and self._recent_cache is not None
            and now - self._recent_cache_ts < _RECENT_TTL_SECONDS
        ):
            return self._recent_cache

        items: list[RecentIdea] = []
        for repo in self._config.repos.values():
            if not repo.project:
                continue
            for raw in fetch_idea_inbox_issues(repo.owner_name, limit=_GH_FETCH_LIMIT):
                labels = [
                    lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
                    for lbl in raw.get("labels", [])
                ]
                number = raw.get("number", 0)
                items.append(
                    RecentIdea(
                        issue_url=raw.get("url", ""),
                        issue_number=number,
                        repo=repo.owner_name,
                        project=repo.project,
                        title=raw.get("title", ""),
                        needs_triage="needs-triage" in labels,
                        created_at=raw.get("createdAt", ""),
                        item_key=f"{repo.owner_name}#{number}",
                        state=(raw.get("state") or "").upper(),
                    )
                )

        self._recent_cache = items
        self._recent_cache_ts = now
        return items

    def _require_configured_src(self, src_repo: str) -> None:
        """Reroutes transfer or close ``src_repo``'s issue with the host's gh
        credentials, so it must be a configured repo (#263 F5)."""
        if src_repo not in self._config.repos:
            raise IdeaError(f"source repo {src_repo!r} is not configured")

    def reroute(
        self,
        *,
        issue_number: int,
        src_repo: str,
        dst_repo: str,
        dst_project: str,
    ) -> ReroutedIdea:
        self._require_configured_src(src_repo)
        repo_cfg = self._config.repos.get(dst_repo)
        if repo_cfg is None or repo_cfg.project != dst_project:
            raise NoCandidatesError(
                f"destination ({dst_repo!r}, {dst_project!r}) is not configured"
            )

        warnings: list[str] = []

        # Pre-create the inbox labels in the DESTINATION before transferring:
        # GitHub keeps a transferred issue's labels only if a same-named label
        # already exists in the target, so without this `idea-inbox` is dropped
        # and the idea vanishes from the /inbox recent list (#181, AHQ-021).
        # Mirrors the `_file` create path.
        ensured = ensure_labels(dst_repo, _LABELS, self._label_cache_path)

        new_url = transfer_issue(issue_number, src_repo, dst_repo)
        self._invalidate_recent_cache()

        # `ensure_labels` is best-effort (never raises): if `idea-inbox` couldn't
        # be created in the destination (auth/network failure), the transfer just
        # dropped it and the moved issue won't appear in /inbox. Surface that
        # rather than reporting silent success — the exact failure #181 targets.
        if "idea-inbox" not in ensured:
            warnings.append(
                f"destination repo {dst_repo!r} is missing the 'idea-inbox' label — "
                "the moved issue won't appear in the inbox until it's re-labeled"
            )

        # Re-add to the destination project. Priority stays unset so the
        # rerouted issue lands untriaged in its new project. A failure is
        # surfaced as a warning rather than swallowed — the client would
        # otherwise see unqualified success (#181, AHQ-021).
        project_number, project_owner = self._project_lookup(dst_project)
        if project_number is None:
            warnings.append(f"unknown project {dst_project!r} — issue not added to any project")
        else:
            try:
                add_to_project(project_number, project_owner, new_url)
            except GHError as e:
                warnings.append(f"project-add failed: {e}")
        return ReroutedIdea(issue_url=new_url, warnings=warnings)

    # --- internals -------------------------------------------------------------

    def _invalidate_recent_cache(self) -> None:
        """Drop the cached recent-ideas list so the next read refetches.

        Called after any write (file / reroute) — the cache would otherwise
        serve a stale list for up to ``_RECENT_TTL_SECONDS`` after the change.
        """
        self._recent_cache = None
        self._recent_cache_ts = 0.0

    def _candidates(self) -> list[Candidate]:
        return [
            Candidate(
                repo=repo.owner_name,
                project=repo.project,
                description=repo.description,
            )
            for repo in self._config.repos.values()
            if repo.project
        ]

    def _project_lookup(self, name: str) -> tuple[int | None, str]:
        for proj in self._config.projects:
            if proj.name == name:
                return proj.number, proj.owner
        return None, ""

    def _file(
        self,
        classification: Classification,
        drafted: Draft,
        *,
        needs_triage: bool,
    ) -> FiledIdea:
        warnings: list[str] = []
        labels = ["idea-inbox"] + (["needs-triage"] if needs_triage else [])

        # Pre-create labels so `gh issue create` doesn't trip on a missing label.
        ensure_labels(classification.repo, _LABELS, self._label_cache_path)

        issue_url = create_issue(classification.repo, drafted.title, drafted.body, labels=labels)
        issue_number = _parse_issue_number(issue_url)

        project_number, project_owner = self._project_lookup(classification.project)
        if project_number is None:
            warnings.append(
                f"unknown project {classification.project!r} — issue not added to any project"
            )
        else:
            try:
                add_to_project(project_number, project_owner, issue_url)
            except GHError as e:
                warnings.append(f"project-add failed: {e}")

        return FiledIdea(
            issue_url=issue_url,
            issue_number=issue_number,
            repo=classification.repo,
            project=classification.project,
            title=drafted.title,
            confidence=classification.confidence,
            needs_triage=needs_triage,
            warnings=warnings,
        )


def _age_seconds(created_at: str, now: datetime) -> float:
    """Age of an ISO timestamp; unparseable → inf (surface it, don't hide it)."""
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return float("inf")
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds()


def _split_capture(text: str) -> tuple[str, str]:
    """Split a capture into (title, body): first non-empty line vs the rest."""
    lines = text.splitlines()
    title = lines[0].strip() if lines else text.strip()
    body = "\n".join(lines[1:]).strip()
    return title, body


def _parse_issue_number(url: str) -> int:
    match = _ISSUE_NUM_RE.search(url)
    if not match:
        return 0
    return int(match.group(1))


__all__ = [
    "ClassifierError",
    "EmptyTextError",
    "FiledIdea",
    "IdeaError",
    "IdeaHandler",
    "NoCandidatesError",
    "RecentIdea",
]
