"""`agent-hq hygiene run [--dry-run]` — manual / dry-run entry to the hygiene sweep (#249)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from . import eventlog
from .config import ConfigError, load_config
from .github import GitHubCache, fetch_issue_summary
from .hygiene_store import Finding, HygieneStore
from .hygiene_worker import collect_offboard_inputs, run_sweep
from .triage_cli import _load_issues, _safe
from .triage_store import TriageStore
from .triage_worker import Runner


def _describe(f: Finding) -> str:
    a = f.action
    kind = a.get("kind")
    if kind == "close_as_duplicate":
        return f"close {a['close']} as duplicate of {a['of']}"
    if kind == "close_stale":
        since = str(a.get("last_activity"))[:10]
        return f"close {a['close']} (no activity since {since}, value {a.get('value')})"
    if kind == "merge_cluster":
        return f"umbrella \"{_safe(a['umbrella_title'])}\" over {', '.join(a['children'])}"
    if kind == "bump_value":
        cur = a.get("current")
        return f"{a['key']}: value {cur if cur is not None else 'unscored'} → {a['proposed']}"
    if kind == "add_to_board":
        title = f" ({_safe(a['title'])})" if a.get("title") else ""
        board = a.get("project_name") or f"#{a['project_number']}"
        return f"add {a['key']}{title} to the {board} board"
    return kind or "?"


def run_hygiene_cli(*, dry_run: bool, runner: Runner | None = None, config_path=None) -> int:
    eventlog.set_process_role("cli")
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"hygiene: invalid config: {exc}", file=sys.stderr)
        return 1
    if cfg is None or cfg.triage is None:
        print(
            "hygiene: not configured — add a `triage:` block with data_dir to projects.yaml",
            file=sys.stderr,
        )
        return 1
    if not cfg.triage.data_dir.is_dir():
        print(
            f"hygiene: data_dir does not exist: {cfg.triage.data_dir} — "
            "clone/create the triage data repo first",
            file=sys.stderr,
        )
        return 1
    triage_store = TriageStore(cfg.triage.data_dir)
    hygiene_store = HygieneStore(cfg.triage.data_dir)
    cache = GitHubCache()
    # Forced (review round 6): a non-forced load can serve a board seconds
    # from its TTL, which has lapsed by the time the off-board collector
    # reads it — the repo would then be skipped as unverified.
    issues = _load_issues(cfg, cache, force=True)
    # Off-board detection (#273) reads the boards through the SAME handle the
    # load above used (review round 1: failure records are per instance, so
    # a second handle would read a stale board as healthy) and the durable
    # issue index.
    report = run_sweep(
        issues,
        triage_store,
        hygiene_store,
        cfg.triage,
        cfg.repos,
        dry_run=dry_run,
        runner=runner,
        now=datetime.now(timezone.utc),
        offboard=collect_offboard_inputs(cfg, cache),
        describe=fetch_issue_summary,
    )
    label = "DRY RUN — nothing written" if dry_run else "run"
    print(
        f"hygiene {label}: {len(issues)} open issue(s); stale {report.stale}, "
        f"duplicates {report.duplicates}, clusters {report.clusters}, "
        f"mismatches {report.mismatches}, off-board {report.offboard}; "
        f"written {report.written}, resolved {report.resolved}"
    )
    for f in report.findings:
        print(f"\n[{f.type.value}] {_describe(f)}")
        print(f"    {_safe(f.reasoning)}")
    if report.error:
        print(f"hygiene: sweep failed — {report.error}", file=sys.stderr)
        return 1
    return 0
