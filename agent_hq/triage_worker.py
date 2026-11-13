"""AI triage worker (#248, Phase 3 of #245).

Pure functions that turn the cached issue list + a triage-store snapshot
into per-repo ``claude -p`` batches, parse the structured output strictly,
and write ``actor="ai"`` records. The subprocess boundary is injected
(``runner``) so tests never spawn Claude; the store's ``_merge`` already
enforces the guardrails (human provenance wins, AI never sets interest/pin,
confirmed never downgrades).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import eventlog
from .config import RepoConfig, TriageConfig
from .models import Issue
from .triage_store import (
    DIMENSIONS,
    AgentFitness,
    Effort,
    TriageRecord,
    TriageState,
    TriageStore,
)

MAX_REASONING_CHARS = 200
HINT_TO_VALUE = {"Need": 4, "Want": 3, "Idea": 2}
MAX_BODY_CHARS = 2000

# Rubric v4 (2026-09-07): revised after the prompt-eval rounds in this branch —
# present-impact value anchors, the sentinel −1 rule, narrowed needs_you /
# widened pair, L = multi-session AND multi-subsystem. Long lines are the
# rubric's own sentences; keep each dimension on one line so the model sees
# one rule per bullet.
RUBRIC = (
    'You are triaging GitHub issues for a personal AI-automation ecosystem owned by one '
    'person. Deadlines do NOT matter; rank by leverage on what is actually happening '
    'today.\n'
    'For EACH issue listed, propose three dimensions plus ONE short sentence (under 150 '
    'characters) of reasoning per dimension:\n'
    '- value (1-5): impact on the ecosystem AS IT STANDS NOW, not hypothetical exposure. '
    '5 = something is broken now, actively losing data, or a live security exposure '
    'exists; 4 = a daily workflow measurably improves, or a known, recurring pain goes '
    'away; 3 = useful, nobody is blocked; 2 = hardening, hygiene, cleanup, or '
    'nice-to-have with no present pain; 1 = cosmetic, someday. Preventive work — CI '
    'gates, secret hygiene, test chores, dependency upgrades with no current breakage — '
    'is a 2 or 3 unless an incident has ALREADY happened. Sentinel rule: an issue filed '
    'by an automated sentinel (label `sentinel`, or a `[sentinel]` title prefix) scores '
    'ONE value point lower than the same problem filed by a human, because it reflects a '
    'single machine observation rather than a person judging that it matters.\n'
    '- effort (S | M | L): S = one focused session, a handful of files, including small '
    'chores; M = several modules, or a UI + backend change, or a redesign that stays '
    'inside one module even if its exact scope is unclear; L = ONLY when it needs BOTH '
    'multiple sessions AND multiple subsystems, where a subsystem is a module or layer '
    'WITHIN the repo (a new subsystem, a cross-cutting rewrite, a whole-repo audit). '
    'Unclear scope alone is M, not L.\n'
    '- agent_fitness (solo | pair | needs_you): solo = a coding agent can finish alone '
    'with tests and self-verification; pair = the agent does the bulk of the work and the'
    ' human reviews, hand-tests UI/UX, or supplies taste and feedback — this includes '
    'explanatory copy, layout, and interaction design, which are pair, not needs_you; '
    'needs_you = the agent CANNOT do the bulk of the work without the human doing part of'
    ' it: credentials or accounts the agent cannot hold, physical-world steps, or a '
    'decision that only the owner can make. When in doubt between pair and needs_you, '
    'choose pair.\n'
    'A "previous priority label" is a weak hint from the old scheme (Need ≈ value 4, Want'
    ' ≈ 3, Idea ≈ 2) — deviate freely when the issue text argues otherwise.\n'
    'Return ONLY the JSON object required by the schema. Include every issue number from '
    'the list exactly once.'
)


class TriageParseError(ValueError):
    """claude -p output did not match the contract — the whole batch is rejected."""


class TriageInvokeError(RuntimeError):
    """claude -p failed to run (missing binary, non-zero exit, timeout, bad envelope)."""


@dataclass(frozen=True)
class Proposal:
    number: int
    value: int
    effort: Effort
    agent_fitness: AgentFitness
    reasoning: dict[str, str]


def body_hash(issue: Issue) -> str:
    """Hash of what the model saw: title, body, sorted labels. Stored on the
    record so a later run re-proposes only when the issue actually changed."""
    h = hashlib.sha256()
    for part in (issue.title, issue.body or "", ",".join(sorted(issue.labels))):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def select_targets(
    issues: list[Issue], records: Iterable[TriageRecord], *, cap: int
) -> dict[str, list[Issue]]:
    """Per-repo lists of issues the worker should propose for, in list order,
    capped per repo. UNTRIAGED (or record-less) always; PROPOSED only when
    the body hash moved; CONFIRMED and tombstoned never (design decision)."""
    by_key = {r.issue_key: r for r in records}
    out: dict[str, list[Issue]] = {}
    for issue in issues:
        rec = by_key.get(issue.key)
        if rec is not None:
            if rec.tombstoned_at is not None or rec.state is TriageState.CONFIRMED:
                continue
            if rec.state is TriageState.PROPOSED and rec.body_hash == body_hash(issue):
                continue
        bucket = out.setdefault(issue.repo, [])
        if len(bucket) < cap:
            bucket.append(issue)
    return {repo: lst for repo, lst in out.items() if lst}


_REASONING_SCHEMA = {
    "type": "object",
    "properties": {dim: {"type": "string", "maxLength": MAX_REASONING_CHARS} for dim in DIMENSIONS},
    "required": list(DIMENSIONS),
}

TRIAGE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "value": {"type": "integer", "minimum": 1, "maximum": 5},
                    "effort": {"type": "string", "enum": [e.value for e in Effort]},
                    "agent_fitness": {"type": "string", "enum": [f.value for f in AgentFitness]},
                    "reasoning": _REASONING_SCHEMA,
                },
                "required": ["number", "value", "effort", "agent_fitness", "reasoning"],
                # Closed object (#267): interest and pin are human-only
                # flags and must never be offered to — or accepted from —
                # the model. ``parse_proposals`` enforces the same rule.
                "additionalProperties": False,
            },
        }
    },
    "required": ["proposals"],
}

_PROPOSAL_KEYS = frozenset({"number", "value", "effort", "agent_fitness", "reasoning"})
_HUMAN_ONLY_FLAGS = ("interest", "pin")


def parse_proposals(structured: object, expected_numbers: set[int]) -> list[Proposal]:
    """Strict parse of the schema-validated object. Any violation rejects the
    WHOLE batch (spec §2 Guardrails: never write garbage). Issues the model
    omitted are fine — they're simply retried next run."""
    if not isinstance(structured, dict) or not isinstance(structured.get("proposals"), list):
        raise TriageParseError("output is not an object with a 'proposals' list")
    seen: set[int] = set()
    out: list[Proposal] = []
    for i, item in enumerate(structured["proposals"]):
        if not isinstance(item, dict):
            raise TriageParseError(f"proposals[{i}] is not an object")
        extra = sorted(set(item) - _PROPOSAL_KEYS)
        if extra:
            # A set flag is the one extra that matters (#267: interest/pin
            # are human-only); name it so the retry prompt says why.
            flags = [k for k in extra if k in _HUMAN_ONLY_FLAGS]
            what = "human-only flag" if flags else "unknown key"
            raise TriageParseError(f"proposals[{i}] sets {what}(s): {', '.join(extra)}")
        number = item.get("number")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number not in expected_numbers
        ):
            raise TriageParseError(
                f"proposals[{i}].number {number!r} is not one of the batch's issues"
            )
        if number in seen:
            raise TriageParseError(f"issue #{number} proposed twice")
        seen.add(number)
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise TriageParseError(f"#{number}: value {value!r} not in 1..5")
        try:
            effort = Effort(item.get("effort"))
            fitness = AgentFitness(item.get("agent_fitness"))
        except ValueError as exc:
            raise TriageParseError(f"#{number}: bad enum: {exc}") from exc
        reasoning = item.get("reasoning")
        if not isinstance(reasoning, dict):
            raise TriageParseError(f"#{number}: reasoning is not an object")
        clean: dict[str, str] = {}
        for dim in DIMENSIONS:
            text = reasoning.get(dim)
            if not isinstance(text, str) or not text.strip():
                raise TriageParseError(f"#{number}: missing reasoning for {dim}")
            if len(text) > MAX_REASONING_CHARS:
                raise TriageParseError(
                    f"#{number}: reasoning for {dim} exceeds {MAX_REASONING_CHARS} chars"
                )
            clean[dim] = text.strip()
        out.append(
            Proposal(
                number=number, value=value, effort=effort, agent_fitness=fitness, reasoning=clean
            )
        )
    return out


def readme_head(local_path: Path | None, *, max_lines: int = 60, max_chars: int = 4000) -> str:
    """First lines of <local_path>/README.md as light repo context; "" when
    the repo isn't checked out here or the file is unreadable — context is
    optional, never a reason to skip a batch."""
    if local_path is None:
        return ""
    try:
        text = (Path(local_path) / "README.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()[:max_lines]
    return "\n".join(lines)[:max_chars]


def build_prompt(
    repo: str,
    issues: list[Issue],
    records: dict[str, TriageRecord],
    *,
    description: str | None,
    readme: str,
) -> str:
    parts = [RUBRIC, "", f"Repository: {repo}"]
    if description:
        parts.append(f"Repository description: {description}")
    if readme:
        parts += ["", "README (head):", readme]
    parts += [
        "",
        f"Issues to triage ({len(issues)}). Return proposals for numbers: "
        + ", ".join(str(i.number) for i in issues),
        "",
    ]
    for issue in issues:
        parts.append(f"### #{issue.number}: {issue.title}")
        if issue.labels:
            parts.append(f"labels: {', '.join(issue.labels)}")
        rec = records.get(issue.key)
        hint = rec.priority_hint if rec is not None else None
        if hint in HINT_TO_VALUE:
            parts.append(f"previous priority label: {hint} (≈ value {HINT_TO_VALUE[hint]})")
        body = (issue.body or "").strip()
        if body:
            parts.append(
                body[:MAX_BODY_CHARS] + (" …[truncated]" if len(body) > MAX_BODY_CHARS else "")
            )
        else:
            parts.append("(no body)")
        parts.append("")
    return "\n".join(parts)


Runner = Callable[[list[str], str, int, Path], str]

# Pinned by the Task 1 probe: no tools, no hooks/MCP/settings, no session file.
CLAUDE_HEADLESS_FLAGS = [
    "--output-format",
    "json",
    "--no-session-persistence",
    "--strict-mcp-config",
    "--setting-sources",
    "",
    "--tools",
    "",
]


def default_runner(argv: list[str], prompt: str, timeout_s: int, cwd: Path) -> str:
    if shutil.which(argv[0]) is None:
        raise TriageInvokeError("claude binary not found on PATH")
    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired as exc:
        raise TriageInvokeError(f"claude -p timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise TriageInvokeError(f"claude -p could not start: {exc}") from exc
    if proc.returncode != 0:
        raise TriageInvokeError(f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def invoke_claude(
    prompt: str,
    *,
    model: str,
    timeout_s: int,
    cwd: Path,
    runner: Runner | None = None,
    schema: dict | None = None,
) -> object:
    """One headless call; returns the schema-validated object. Envelope
    problems (not JSON, is_error, no structured_output) are invoke errors —
    they mean the call itself failed, not that the model wrote bad JSON."""
    argv = [
        "claude",
        "-p",
        "--model",
        model,
        *CLAUDE_HEADLESS_FLAGS,
        "--json-schema",
        json.dumps(schema if schema is not None else TRIAGE_OUTPUT_SCHEMA),
    ]
    stdout = (runner or default_runner)(argv, prompt, timeout_s, cwd)
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise TriageInvokeError(f"claude -p stdout is not JSON: {stdout[:200]!r}") from exc
    if not isinstance(envelope, dict) or envelope.get("is_error"):
        raise TriageInvokeError(f"claude -p reported an error: {str(envelope)[:300]}")
    structured = envelope.get("structured_output")
    if structured is None:
        raise TriageInvokeError("claude -p envelope has no structured_output")
    return structured


def proposal_to_record(issue: Issue, proposal: Proposal, *, model: str, now: str) -> TriageRecord:
    return TriageRecord(
        issue_key=issue.key,
        value=proposal.value,
        effort=proposal.effort,
        agent_fitness=proposal.agent_fitness,
        reasoning=dict(proposal.reasoning),
        proposed_at=now,
        model=model,
        body_hash=body_hash(issue),
        updated_at=now,
    )


@dataclass
class RepoRunResult:
    repo: str
    targeted: int
    proposed: int = 0
    proposals: list[Proposal] = field(default_factory=list)
    error: str | None = None


@dataclass
class TriageRunReport:
    repos: list[RepoRunResult]
    dry_run: bool

    @property
    def targeted(self) -> int:
        return sum(r.targeted for r in self.repos)

    @property
    def proposed(self) -> int:
        return sum(r.proposed for r in self.repos)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.repos if r.error)


def run_triage(
    issues: list[Issue],
    store: TriageStore,
    triage_cfg: TriageConfig,
    repos_cfg: dict[str, RepoConfig],
    *,
    dry_run: bool = False,
    runner: Runner | None = None,
    now: str | None = None,
    only_repo: str | None = None,
    cap: int | None = None,
) -> TriageRunReport:
    """One worker run: per repo (sequentially — concurrency 1), select →
    prompt → invoke → parse (retry once with the parser error appended) →
    upsert actor="ai". Never raises; per-repo outcomes land in the report
    and in eventlog (error = batch rejected twice, warn = claude failed)."""
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    records = store.all_records(include_tombstoned=True)
    by_key = {r.issue_key: r for r in records}
    batch_cap = cap if cap is not None else triage_cfg.ai_batch_cap
    targets = select_targets(issues, records, cap=batch_cap)
    if only_repo is not None:
        targets = {k: v for k, v in targets.items() if k == only_repo}
    report = TriageRunReport(repos=[], dry_run=dry_run)
    for repo, batch in targets.items():
        result = RepoRunResult(repo=repo, targeted=len(batch))
        report.repos.append(result)
        rc = repos_cfg.get(repo)
        prompt = build_prompt(
            repo,
            batch,
            by_key,
            description=rc.description if rc else None,
            readme=readme_head(rc.local_path if rc else None),
        )
        expected = {i.number for i in batch}
        proposals: list[Proposal] | None = None
        attempt_prompt = prompt
        for attempt in (1, 2):
            try:
                structured = invoke_claude(
                    attempt_prompt,
                    model=triage_cfg.ai_model,
                    timeout_s=triage_cfg.ai_timeout_s,
                    cwd=store.data_dir,
                    runner=runner,
                )
                # Parse into a local first: an empty result must leave
                # ``proposals`` None so the failure path below still fires.
                parsed = parse_proposals(structured, expected)
                if not parsed:
                    # Zero progress: the same targets are selected next tick,
                    # so a caller that re-kicks on refresh would loop paid
                    # calls forever. Route it through the retry-then-fail path
                    # (which trips the web trigger's cooldown) instead.
                    raise TriageParseError(f"model returned no proposals for {len(batch)} issues")
                proposals = parsed
                break
            except TriageInvokeError as exc:
                result.error = f"invoke: {exc}"
                eventlog.emit(
                    "warn",
                    "triage worker invoke failed",
                    {"repo": repo, "error": str(exc)},
                    min_interval_s=900,
                )
                break
            except TriageParseError as exc:
                result.error = f"parse: {exc}"
                if attempt == 1:
                    attempt_prompt = (
                        prompt
                        + "\n\nYour previous output was rejected: "
                        + str(exc)
                        + "\nReturn ONLY JSON matching the schema, with every listed "
                        "issue number exactly once."
                    )
                else:
                    eventlog.emit(
                        "error",
                        "triage worker output rejected",
                        {"repo": repo, "error": str(exc), "targeted": len(batch)},
                        min_interval_s=900,
                    )
        if proposals is None:
            continue
        result.error = None
        result.proposals = proposals
        if dry_run:
            result.proposed = len(proposals)
            continue
        by_number = {i.number: i for i in batch}
        for p in proposals:
            try:
                stored = store.upsert(
                    proposal_to_record(by_number[p.number], p, model=triage_cfg.ai_model, now=now),
                    actor="ai",
                )
            except OSError as exc:
                stored, write_error = None, f"write: {exc}"
            else:
                write_error = "write: repo file unreadable"
            if stored is None:
                result.error = write_error
                eventlog.emit(
                    "warn",
                    "triage write failed",
                    {"op": "ai-propose", "repo": repo, "error": result.error},
                )
                break
            result.proposed += 1
    eventlog.emit(
        "info",
        "triage worker run",
        {
            "repos": len(report.repos),
            "targeted": report.targeted,
            "proposed": report.proposed,
            "failed": report.failed,
            "dry_run": dry_run,
        },
    )
    return report
