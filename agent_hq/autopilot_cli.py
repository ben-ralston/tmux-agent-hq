"""`agent-hq autopilot run [--dry-run]` (#302) — the nightly entry point.

Prints short status lines for the launchd wrapper's log. The LAST line says
FAILED whenever the run hit an error, and the exit code is then 1, so the
Automations tab and the Autopilot card both show a failed night.
"""

from __future__ import annotations

import fcntl
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent_hq import eventlog
from agent_hq.autopilot import (
    GOAL_TEMPLATE,
    SKIP_BASE_UNKNOWN,
    SKIP_CHANGED,
    SKIP_GITHUB,
    SKIP_PAUSED,
    SKIP_WORKTREE,
    SKIP_WORKTREE_UNKNOWN,
    GoalTemplateError,
    LaunchStatus,
    attempt_branch,
    cleanup_failed_launch,
    default_launcher,
    fetch_default_branch,
    fill_goal_condition,
    issue_branch_numbers,
    load_issues_strict,
    record_still_eligible,
    run_launcher,
    select,
    sync_outcomes,
)
from agent_hq.autopilot_store import Attempt, AutopilotStore, Outcome, ReserveResult, RunRecord
from agent_hq.config import ConfigError, load_config
from agent_hq.triage_store import TriageStore, load_rank_weights

_LAUNCH_OUTCOME = {
    LaunchStatus.OK: Outcome.RUNNING,
    LaunchStatus.FAILED: Outcome.LAUNCH_FAILED,
    LaunchStatus.UNCERTAIN: Outcome.UNKNOWN,
}


# Skip reasons that mean an opted-in candidate was refused because an input
# could not be trusted. Spec rule 9: such a night is a FAILED night.
_FAILURE_SKIPS = (SKIP_GITHUB, SKIP_WORKTREE_UNKNOWN, SKIP_BASE_UNKNOWN)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class _Progress:
    """What a run has done so far, visible to the crash handler so a raise
    mid-run still records the night (otherwise the card keeps showing the
    previous night green). ``recordable`` flips once the state file is known
    readable and the run is a real (non-dry) one."""

    run_id: str = ""
    started: str = ""
    recordable: bool = False
    launched: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    throttled: bool = False


def _failure_skip_errors(skipped: list[tuple[str, str]]) -> list[str]:
    """``"<reason>: <repo>"`` for each repo whose flagged candidate was
    skipped for an untrusted input, first-seen order."""
    out: list[str] = []
    for key, reason in skipped:
        if reason in _FAILURE_SKIPS:
            msg = f"{reason}: {key.rsplit('#', 1)[0]}"
            if msg not in out:
                out.append(msg)
    return out


def _record_crash(store, progress: _Progress, error: str) -> None:
    if not progress.recordable:
        return
    try:
        store.record_run(
            RunRecord(
                progress.run_id,
                progress.started,
                _now(),
                list(progress.launched),
                list(progress.skipped),
                progress.throttled,
                error=error,
            )
        )
    except Exception:  # noqa: BLE001 — best effort; the FAILED line still prints
        pass


def run_autopilot_cli(
    *,
    dry_run: bool,
    config_path=None,
    launcher=None,
    pr_lookup=None,
    session_probe=None,
    issues_loader=None,
    branch_probe=None,
    base_lookup=None,
    cleanup=None,
    template_path: Path | None = None,
) -> int:
    eventlog.set_process_role("cli")
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"autopilot: FAILED invalid config: {exc}")
        return 1
    if cfg is None:
        print(f"autopilot: FAILED no config at {config_path or 'the default path'}")
        return 1
    ap = cfg.autopilot
    # Disabled beats a missing triage block: the kill switch must stay quiet.
    if ap is None or not ap.enabled:
        print("autopilot: skipped (disabled)")
        eventlog.emit("info", "autopilot run", {"skipped": "disabled"})
        return 0
    if cfg.triage is None or not cfg.triage.data_dir.is_dir():
        print("autopilot: FAILED needs a `triage:` block whose data_dir exists")
        return 1
    store = AutopilotStore(cfg.triage.data_dir)
    # Tolerant read is fine HERE: it can only stop a run early. The launch
    # gate is reserve() (re-checks paused under the lock) after
    # assert_readable() refuses a corrupt file.
    if store.snapshot().paused:
        print("autopilot: skipped (paused)")
        eventlog.emit("info", "autopilot run", {"skipped": "paused"})
        return 0
    lock_path = store.path.parent / "run.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fh = open(lock_path, "w")  # noqa: SIM115 — held for the whole run below
    except OSError as exc:
        print(f"autopilot: FAILED cannot open run lock: {exc}")
        eventlog.emit("error", "autopilot launch failed", {"stage": "lock", "error": repr(exc)})
        return 1
    with lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("autopilot: skipped (another run holds the lock)")
            eventlog.emit("info", "autopilot run", {"skipped": "locked"})
            return 0
        progress = _Progress()
        try:
            return _run_locked(
                cfg,
                ap,
                store,
                progress,
                dry_run=dry_run,
                launcher=launcher or default_launcher,
                pr_lookup=pr_lookup,
                session_probe=session_probe,
                issues_loader=issues_loader or load_issues_strict,
                branch_probe=branch_probe or issue_branch_numbers,
                base_lookup=base_lookup or fetch_default_branch,
                cleanup=cleanup or cleanup_failed_launch,
                template_path=template_path or GOAL_TEMPLATE,
            )
        except OSError as exc:
            _record_crash(store, progress, f"state unreadable: {exc}")
            print(f"autopilot: FAILED state unreadable: {exc}")
            eventlog.emit(
                "error", "autopilot launch failed", {"stage": "state", "error": repr(exc)}
            )
            return 1
        except Exception as exc:  # noqa: BLE001 — the wrapper keys on a FAILED last line
            _record_crash(store, progress, f"unexpected error: {exc!r}")
            print(f"autopilot: FAILED unexpected error: {exc!r}")
            eventlog.emit(
                "error", "autopilot launch failed", {"stage": "unexpected", "error": repr(exc)}
            )
            return 1


def _run_locked(
    cfg,
    ap,
    store,
    progress: _Progress,
    *,
    dry_run,
    launcher,
    pr_lookup,
    session_probe,
    issues_loader,
    branch_probe,
    base_lookup,
    cleanup,
    template_path,
) -> int:
    run_id = started = _now()
    store.assert_readable()  # corrupt state → OSError → exit 1, before anything launches
    progress.run_id, progress.started = run_id, started
    progress.recordable = not dry_run

    try:
        template_text = Path(template_path).read_text()
        fill_goal_condition(template_text, issue_number=1, branch="probe", turn_bound=1)
    except (OSError, UnicodeDecodeError, GoalTemplateError) as exc:
        msg = f"goal template unusable: {exc}"
        if not dry_run:
            store.record_run(RunRecord(run_id, started, _now(), [], [], False, error=msg))
        print(f"autopilot: FAILED {msg}")
        eventlog.emit("error", "autopilot launch failed", {"stage": "template", "error": msg})
        return 1

    probes = {k: v for k, v in (("pr_lookup", pr_lookup), ("session_probe", session_probe)) if v}
    state = sync_outcomes(store, now=started, write=not dry_run, **probes)

    triage = TriageStore(cfg.triage.data_dir)
    records = {r.issue_key: r for r in triage.all_records(include_tombstoned=True)}
    issues, unsafe = issues_loader(cfg)
    unsafe = dict(unsafe)
    active: set[str] = set()
    for repo in ap.repos:
        nums = branch_probe(cfg.repos[repo].local_path)
        if nums is None:
            unsafe.setdefault(repo, SKIP_WORKTREE_UNKNOWN)
        else:
            active |= {f"{repo}#{n}" for n in nums}
    selection = select(
        issues,
        records,
        state.blocked_keys,
        in_flight=state.in_flight,
        active_issue_keys=active,
        unsafe_repos=unsafe,
        cfg=ap,
        weights=load_rank_weights(cfg.triage.data_dir),
    )

    # Base branch per repo (#312): config override, else GitHub's default
    # branch. Resolved lazily (only repos actually reached) and once per run.
    bases: dict[str, str | None] = {}

    def base_for(repo: str) -> str | None:
        if repo not in bases:
            bases[repo] = cfg.repos[repo].base_branch or base_lookup(repo)
        return bases[repo]

    if dry_run:
        unknown = [i for i in selection.launch if base_for(i.repo) is None]
        dry_skips = [(i.key, SKIP_BASE_UNKNOWN) for i in unknown]
        would = [i for i in selection.launch if bases[i.repo] is not None]
        print(f"autopilot DRY RUN — no state written. would launch {len(would)}")
        for issue in would:
            base = bases[issue.repo]
            branch = attempt_branch(issue, "xxxxxx")
            goal = fill_goal_condition(
                template_text, issue_number=issue.number, branch=branch, turn_bound=ap.turn_bound
            )
            print(f"\n{issue.key} → {branch} (base {base})\n{goal}")
        for key, reason in dry_skips + list(selection.skipped):
            print(f"  skip {key}: {reason}")
        return 0

    # Shared with the crash handler: a raise below still records the night.
    launched = progress.launched
    skipped = progress.skipped
    skipped.extend(selection.skipped)
    progress.throttled = selection.throttled
    errors: list[str] = []
    for idx, issue in enumerate(selection.launch):
        # Revalidate right before the reservation: the opt-in / confirmation
        # may have changed while earlier launches ran.
        if not record_still_eligible(triage.get(issue.key)):
            skipped.append((issue.key, SKIP_CHANGED))
            continue
        # Re-probe branches too: another dispatcher may have created
        # bugfix/<n>-… while an earlier launch ran. A failed probe refuses.
        nums = branch_probe(cfg.repos[issue.repo].local_path)
        if nums is None:
            skipped.append((issue.key, SKIP_WORKTREE_UNKNOWN))
            continue
        if issue.number in nums:
            skipped.append((issue.key, SKIP_WORKTREE))
            continue
        # Before the reservation: an unknown base must not leave one behind.
        base = base_for(issue.repo)
        if base is None:
            skipped.append((issue.key, SKIP_BASE_UNKNOWN))
            continue
        attempt_id = uuid.uuid4().hex
        branch = attempt_branch(issue, attempt_id)
        prompt = fill_goal_condition(
            template_text, issue_number=issue.number, branch=branch, turn_bound=ap.turn_bound
        )
        attempt = Attempt(
            issue_key=issue.key,
            repo=issue.repo,
            branch=branch,
            run_id=run_id,
            dispatched_at=_now(),
            outcome=Outcome.LAUNCHING,
            attempt_id=attempt_id,
        )
        reserved = store.reserve(attempt, max_open=ap.max_open)
        if reserved is ReserveResult.PAUSED:
            skipped.extend((i.key, SKIP_PAUSED) for i in selection.launch[idx:])
            break
        if reserved is not ReserveResult.OK:
            skipped.append((issue.key, reserved.value))
            continue
        res = run_launcher(
            branch=branch,
            prompt=prompt,
            cfg=ap,
            repo_path=cfg.repos[issue.repo].local_path,
            launcher=launcher,
            base=base,
        )
        # A launcher-stage failure (nothing running) may still have created the
        # worktree + branch; remove them so Retry works (#312). Never after an
        # uncertain launch — a session may be running in that worktree.
        cleanup_note = None
        detail = res.detail
        if res.launcher_failure:
            try:
                cleanup_note = cleanup(cfg.repos[issue.repo].local_path, branch)
            except Exception as exc:  # noqa: BLE001 — the outcome below must still land
                cleanup_note = f"cleanup failed: {exc!r}"
            detail = f"{res.detail} ({cleanup_note})"
        store.finish_launch(
            issue.key,
            attempt_id,
            outcome=_LAUNCH_OUTCOME[res.status],
            session_name=res.session_name,
            worktree_path=res.worktree_path,
            detail=detail,
            now=_now(),
        )
        if res.status is LaunchStatus.OK:
            launched.append(issue.key)
            print(f"autopilot: launched {issue.key} on {branch}")
            continue
        errors.append(f"{issue.key}: {detail}")
        print(f"autopilot: launch {res.status.value} {issue.key}: {detail}")
        if res.stderr_tail:
            print(f"autopilot: stderr tail:\n{res.stderr_tail}")
        extra = {"issue": issue.key, "status": res.status.value, "detail": res.detail}
        if res.stderr_tail:
            extra["stderr_tail"] = res.stderr_tail
        if cleanup_note:
            extra["cleanup"] = cleanup_note
        eventlog.emit("error", "autopilot launch failed", extra)
        # A launcher-stage failure will fail again; an uncertain one may have
        # left a session running. Either way, stop the night.
        if res.launcher_failure or res.status is LaunchStatus.UNCERTAIN:
            break

    error = "; ".join(_failure_skip_errors(skipped) + errors) or None
    store.record_run(
        RunRecord(run_id, started, _now(), launched, skipped, selection.throttled, error=error)
    )
    eventlog.emit(
        "info",
        "autopilot run",
        {
            "launched": launched,
            "skipped": len(skipped),
            "throttled": selection.throttled,
            "error": error,
        },
    )
    verdict = "FAILED" if error else "ok"
    print(
        f"autopilot: {verdict} — launched {len(launched)}, skipped {len(skipped)}"
        f"{', throttled' if selection.throttled else ''}"
    )
    return 1 if error else 0
