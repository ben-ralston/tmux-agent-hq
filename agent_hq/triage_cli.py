"""`agent-hq triage run` — manual / dry-run entry to the AI triage worker (#248)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from . import eventlog
from .config import Config, ConfigError, load_config
from .github import GitHubCache
from .issues_hub import ensure_triage_records, merge_issue_lists
from .models import Issue
from .triage_store import TriageStore
from .triage_worker import Runner, run_triage


def _load_issues(
    cfg: Config, cache: GitHubCache | None = None, force: bool = False
) -> list[Issue]:
    """The same open-issue set the web's ``_load_issues`` builds: the shared
    Done-drop + de-dup flatten, then the closed-key filter. Divergence here
    would make the CLI propose for Done issues and revive records the web
    seeding pass tombstoned. ``cache`` lets a caller keep reading through
    the handle that recorded this load's fetch failures (#273); ``force``
    bypasses the board TTL (the hygiene CLI's off-board collector needs an
    entry that will still be fresh a few seconds later)."""
    cache = cache or GitHubCache()
    fetched = [cache.get_issues(proj.number, proj.owner, force) for proj in cfg.projects]
    issues = merge_issue_lists(fetched)
    repos = {i.repo for i in issues}
    closed = cache.get_closed_keys(repos) if repos else set()
    return [i for i in issues if i.key not in closed]


def _safe(text: str) -> str:
    """Strip non-printable characters from model-sourced text before it
    reaches a terminal — reasoning strings are LLM output, so an escape
    sequence in one must not be able to repaint the operator's screen."""
    return "".join(ch for ch in text if ch == " " or ch.isprintable())


def run_triage_cli(
    *,
    dry_run: bool,
    repo: str | None,
    cap: int | None,
    runner: Runner | None = None,
    config_path=None,
) -> int:
    eventlog.set_process_role("cli")
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"triage: invalid config: {exc}", file=sys.stderr)
        return 1
    if cfg is None or cfg.triage is None:
        print(
            "triage: not configured — add a `triage:` block with data_dir to projects.yaml",
            file=sys.stderr,
        )
        return 1
    if not cfg.triage.data_dir.is_dir():
        # The store would happily create the tree on first write, which means a
        # typo'd or un-cloned data repo silently starts a second, empty store
        # instead of failing loudly.
        print(
            f"triage: data_dir does not exist: {cfg.triage.data_dir} — "
            "clone/create the triage data repo first",
            file=sys.stderr,
        )
        return 1
    store = TriageStore(cfg.triage.data_dir)
    issues = _load_issues(cfg)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not dry_run:
        # Seed/tombstone like the web pass. A failure here (read-only data
        # repo, disk full) must be reported: proposing against an un-seeded
        # store would otherwise look like a successful run.
        try:
            ensure_triage_records(store, issues, now=now)
        except OSError as exc:
            print(f"triage: seeding failed: {exc}", file=sys.stderr)
            return 1
    report = run_triage(
        issues,
        store,
        cfg.triage,
        cfg.repos,
        dry_run=dry_run,
        runner=runner,
        now=now,
        only_repo=repo,
        cap=cap,
    )
    label = "DRY RUN — nothing written" if dry_run else "run"
    print(
        f"triage {label}: {len(report.repos)} repo(s), "
        f"targeted {report.targeted}, proposed {report.proposed}, failed {report.failed}"
    )
    for r in report.repos:
        error_suffix = f", error: {r.error}" if r.error else ""
        print(f"\n{r.repo}: targeted {r.targeted}, proposed {r.proposed}{error_suffix}")
        for p in r.proposals:
            print(f"  #{p.number}  V{p.value} {p.effort.value} {p.agent_fitness.value}")
            for dim, text in p.reasoning.items():
                print(f"      {dim}: {_safe(text)}")
    return 1 if report.failed else 0
