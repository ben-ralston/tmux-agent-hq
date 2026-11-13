"""Data models for Agent HQ."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from .status_types import LiveStatus


class AgentType(Enum):
    """Supported AI coding agent types."""

    CLAUDE = "claude"
    CODEX = "codex"
    AIDER = "aider"
    OPENCODE = "opencode"
    GEMINI = "gemini"
    CURSOR = "cursor"
    ROO = "roo"
    UNKNOWN = "unknown"

    @classmethod
    def from_command(cls, command: str) -> "AgentType":
        """Determine agent type from command name."""
        cmd_lower = command.lower()
        for agent_type in cls:
            if agent_type.value in cmd_lower:
                return agent_type
        return cls.UNKNOWN


class AgentState(Enum):
    """Current state of an AI coding agent."""

    IDLE = "idle"  # Waiting for user input
    RUNNING = "running"  # Actively working (thinking, tools, streaming)
    WAITING = "waiting"  # Blocked on user permission approval
    UNKNOWN = "unknown"  # State cannot be determined

    @property
    def emoji(self) -> str:
        """Emoji indicator for this state."""
        return {
            "idle": "⚪",
            "running": "🟢",
            "waiting": "🟡",
            "unknown": "⚫",
        }[self.value]


class DevPhase(Enum):
    """Development phase of a branch/worktree. Non-monotonic (can regress).

    Declaration order is the order the kanban / phase views render (top /
    left-to-right). ``ADHOC`` sorts first so unscoped/exploring agents stay
    visible at the top rather than buried below the lifecycle phases, which
    then follow in lifecycle order. ``DESIGN`` (issue #108) is a single
    pre-implementation phase replacing the former ``BRAINSTORM`` + ``PLAN``
    pair.
    """

    ADHOC = "adhoc"
    DESIGN = "design"
    IMPLEMENT = "implement"
    QA = "qa"
    REVIEW = "review"
    READY = "ready"
    DONE = "done"

    @property
    def emoji(self) -> str:
        """Short glyph for compact TUI cells."""
        return {
            "design": "💭",
            "implement": "🔨",
            "qa": "🧪",
            "review": "👀",
            "ready": "🚀",
            "done": "✅",
            "adhoc": "·",
        }[self.value]

    @property
    def label(self) -> str:
        """Human-readable column label."""
        return {
            "design": "Design",
            "implement": "Implement",
            "qa": "QA",
            "review": "Review",
            "ready": "Ready",
            "done": "Done",
            "adhoc": "Ad-hoc",
        }[self.value]


# Aliases for renamed phase values; keeps state files / overrides written by
# older Agent HQ versions (and transcript ``<phase>`` tags from sessions that
# predate the rename) readable without a one-shot disk migration.
#   ``merged → done``        — issue #55 collapsed PR-merge and non-PR
#                              completion into a single semantic phase.
#   ``brainstorm/plan → design`` — issue #108 collapsed the two
#                              pre-implementation phases into one.
_LEGACY_PHASE_ALIASES = {
    "merged": "done",
    "brainstorm": "design",
    "plan": "design",
}


def normalize_phase_value(value: str) -> str:
    """Map a stored phase string to its current canonical value."""
    return _LEGACY_PHASE_ALIASES.get(value, value)


def _phase_from_value(value: str | None) -> "DevPhase | None":
    """Coerce a stored phase string (incl. legacy aliases) to the enum.

    Returns ``None`` for missing/empty/unknown values rather than raising —
    used inside ``PhaseSnapshot.from_dict`` and ``PhaseFeedback.from_dict``
    where a corrupt entry shouldn't blow up the whole cache read.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return DevPhase(normalize_phase_value(value))
    except ValueError:
        return None


# Where the phase value came from. ``tag`` is the fast path; ``tag_retry``
# means the Stop-hook retry loop fired before the transcript stabilized;
# ``tag_lookback`` means no tag in the latest assistant text and the
# transcript walk found one earlier; ``rule`` is the branch-state fallback;
# ``none`` means we have no signal.
PhaseSource = Literal["tag", "tag_retry", "tag_lookback", "rule", "none"]

# Which process generated this snapshot. ``daemon`` is canonical (poll
# loop tick); ``watcher`` is local re-resolve between cache ticks; ``live``
# is the cache-miss fallback in ``cache.get_agents`` when the daemon is
# down. The producer field is diagnostic — correctness is governed by
# ``inputs_observed`` (revisions) + ``phase_session_id`` (session reset).
PhaseProducer = Literal["daemon", "watcher", "live"]


@dataclass(frozen=True)
class FileStats:
    """Summary of changes on a branch vs its base."""

    files: int
    insertions: int
    deletions: int

    def display(self) -> str:
        """Human string like '3 files +42 -8', or '' when empty."""
        if self.files == 0 and self.insertions == 0 and self.deletions == 0:
            return ""
        file_word = "file" if self.files == 1 else "files"
        return f"{self.files} {file_word} +{self.insertions} -{self.deletions}"

    def to_dict(self) -> dict:
        return {"files": self.files, "insertions": self.insertions, "deletions": self.deletions}

    @classmethod
    def from_dict(cls, data: dict) -> "FileStats":
        return cls(
            files=int(data["files"]),
            insertions=int(data["insertions"]),
            deletions=int(data["deletions"]),
        )


@dataclass(frozen=True)
class PhaseFeedback:
    """User action on a pane's phase. Persisted in overrides.json.

    Either an ``override`` (changes ``effective_phase``) or a ``confirm``
    (acknowledges the auto-detected value; ``effective_phase`` stays the
    detected value). Both surface as a badge in the UI; the resolver
    decides which one matters for the effective phase.
    """

    action: Literal["override", "confirm"]
    phase: DevPhase | None  # user's chosen phase if override; None if confirm
    auto_phase: DevPhase | None  # what was auto-detected at the time of action
    set_at: str  # ISO 8601 UTC string, matching routes/phase.py format
    note: str | None
    session_id: str | None  # writing agent's claude_session_id (bridge id)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "phase": self.phase.value if self.phase else None,
            "auto_phase": self.auto_phase.value if self.auto_phase else None,
            "set_at": self.set_at,
            "note": self.note,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PhaseFeedback":
        action = data.get("action")
        if action not in ("override", "confirm"):
            raise ValueError(f"unknown feedback action: {action!r}")
        return cls(
            action=action,
            phase=_phase_from_value(data.get("phase")),
            auto_phase=_phase_from_value(data.get("auto_phase")),
            set_at=str(data.get("set_at", "")),
            note=data.get("note"),
            session_id=data.get("session_id"),
        )


@dataclass(frozen=True)
class InputsObserved:
    """Inputs the resolver observed at snapshot time.

    ``overrides_revision`` / ``hook_state_revision`` are integer
    revisions read from inside ``overrides.json`` / the hook state file
    (content + version observed atomically, no separate ``os.stat``).
    ``transcript_mtime_ns`` / ``transcript_size`` come from the single
    ``os.stat`` of the Claude transcript the resolver scanned for the
    ``<phase>`` tag — they let ``replaces()`` order transcript-only
    phase changes (which touch neither other file) and let the resolver
    skip a re-scan when the transcript is byte-for-byte unchanged.

    Zero means "not observed": no file, pre-v5 schema with no revision
    key, or the transcript sessionId was unknown (path None). A zero on
    either side makes the transcript axis non-comparable in ``replaces``
    (an unknown must never reject a real update).
    """

    overrides_revision: int
    hook_state_revision: int
    transcript_mtime_ns: int = 0
    transcript_size: int = 0

    def to_dict(self) -> dict:
        return {
            "overrides_revision": self.overrides_revision,
            "hook_state_revision": self.hook_state_revision,
            "transcript_mtime_ns": self.transcript_mtime_ns,
            "transcript_size": self.transcript_size,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InputsObserved":
        return cls(
            overrides_revision=int(data.get("overrides_revision", 0)),
            hook_state_revision=int(data.get("hook_state_revision", 0)),
            transcript_mtime_ns=int(data.get("transcript_mtime_ns", 0)),
            transcript_size=int(data.get("transcript_size", 0)),
        )


@dataclass(frozen=True)
class PhaseSnapshot:
    """Resolver output for one pane at one moment.

    The correctness key is ``inputs_observed`` (integer revisions on
    overrides + hook state). Wall clock is display-only; ``monotonic_ns``
    is the final tiebreaker when two snapshots share the same inputs.
    """

    detected_phase: DevPhase | None
    effective_phase: DevPhase | None
    phase_source: PhaseSource
    phase_session_id: str | None  # transcript sessionId at capture time
    feedback: PhaseFeedback | None  # override or confirm — both surface in UI

    inputs_observed: InputsObserved  # primary correctness key

    # Diagnostic / tiebreaker fields.
    producer: PhaseProducer
    producer_epoch: float  # producer instance start time (wall clock; display)
    sequence: int  # local seq within producer instance
    monotonic_ns: int  # time.monotonic_ns() at resolver-call (final tiebreaker)
    timestamp: float  # time.time() at resolver-call (display/logs)

    def to_dict(self) -> dict:
        return {
            "detected_phase": self.detected_phase.value if self.detected_phase else None,
            "effective_phase": self.effective_phase.value if self.effective_phase else None,
            "phase_source": self.phase_source,
            "phase_session_id": self.phase_session_id,
            "feedback": self.feedback.to_dict() if self.feedback else None,
            "inputs_observed": self.inputs_observed.to_dict(),
            "producer": self.producer,
            "producer_epoch": self.producer_epoch,
            "sequence": self.sequence,
            "monotonic_ns": self.monotonic_ns,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PhaseSnapshot":
        if not isinstance(data, dict):
            raise ValueError(f"PhaseSnapshot.from_dict expects dict, got {type(data).__name__}")
        feedback_raw = data.get("feedback")
        inputs_raw = data.get("inputs_observed") or {}
        return cls(
            detected_phase=_phase_from_value(data.get("detected_phase")),
            effective_phase=_phase_from_value(data.get("effective_phase")),
            phase_source=data.get("phase_source") or "none",
            phase_session_id=data.get("phase_session_id"),
            feedback=PhaseFeedback.from_dict(feedback_raw) if feedback_raw else None,
            inputs_observed=InputsObserved.from_dict(inputs_raw),
            producer=data.get("producer") or "daemon",
            producer_epoch=float(data.get("producer_epoch", 0.0)),
            sequence=int(data.get("sequence", 0)),
            monotonic_ns=int(data.get("monotonic_ns", 0)),
            timestamp=float(data.get("timestamp", 0.0)),
        )


def replaces(new: PhaseSnapshot, old: PhaseSnapshot | None) -> bool:
    """Per-pane acceptance rule used by the watcher's three update paths.

    Rules (in order):

    1. No prior snapshot → accept.
    2. **Positive session mismatch** (both sides non-None and differ) →
       accept unconditionally. A new Claude session reused the pane number;
       the old snapshot's inputs are no longer comparable. Mirrors the
       3-state matrix used elsewhere — exactly-one-None falls through to
       revision compare so a transient session-file read failure can't
       accept a lower-revision snapshot.
    3. Any input observed strictly older on the new snapshot → reject.
       The new snapshot can't represent reality if it didn't see at least
       the same inputs the old one did. The transcript axis is compared
       as ``(mtime_ns, size)`` — JSONL is append-only so both rise
       monotonically within a session — but ONLY when both snapshots
       have a non-zero ``transcript_mtime_ns``. A zero means the
       transcript wasn't observed (unknown sessionId / legacy snapshot);
       an unknown must never reject a real update.
    4. At least one input strictly newer → accept. A transcript-only
       phase change touches neither ``overrides.json`` nor the hook
       state file, so without this axis it would fall through to the
       ``monotonic_ns`` tiebreak with no reject-if-older guarantee.
    5. All inputs identical → tiebreak by ``monotonic_ns``. Handles two
       equal-input fallback resolves racing — last-writer-wins on the
       monotonic clock (the only ordering primitive that tolerates clock
       steps).
    """
    if old is None:
        return True
    if (
        new.phase_session_id is not None
        and old.phase_session_id is not None
        and new.phase_session_id != old.phase_session_id
    ):
        return True
    n, o = new.inputs_observed, old.inputs_observed
    transcript_comparable = n.transcript_mtime_ns > 0 and o.transcript_mtime_ns > 0
    new_t = (n.transcript_mtime_ns, n.transcript_size)
    old_t = (o.transcript_mtime_ns, o.transcript_size)
    if n.overrides_revision < o.overrides_revision:
        return False
    if n.hook_state_revision < o.hook_state_revision:
        return False
    if transcript_comparable and new_t < old_t:
        return False
    if n.overrides_revision > o.overrides_revision or n.hook_state_revision > o.hook_state_revision:
        return True
    if transcript_comparable and new_t > old_t:
        return True
    return new.monotonic_ns >= old.monotonic_ns


def session_match_state(
    stored: str | None, current: str | None, *, lenient_none: bool = False
) -> str:
    """Classify a stored override's ``session_id`` against an agent's live
    session id per the shared 5-state matrix. Single source of truth for
    both the resolver's apply decision (``resolver._feedback_from_entry``)
    and the daemon's prune pass (``overrides.prune_stale_overrides``) --
    they must never fork into two separately-maintained matrices.

    Returns one of:

    * ``"match"`` -- identical (including both ``None``), or
      ``lenient_none`` and exactly one side is ``None``. Feedback applies;
      entry is kept.
    * ``"ambiguous"`` -- exactly one side is ``None`` and not
      ``lenient_none`` -- a transient bridge-id read failure. Feedback
      does NOT apply, but the entry is still kept (the next successful
      read resolves it).
    * ``"mismatch"`` -- both sides non-``None`` and differ -- a genuine
      session rollover (new session reused the pane). Feedback does not
      apply and the entry is pruned.

    ``lenient_none`` exists because codex's ``codex_thread_id`` flaps
    between a UUID and ``None`` across ordinary operation (capture-after-
    write, dedupe clearing, the 3-day rollout cutoff) in a way a claude
    bridge id does not -- so codex callers pass ``lenient_none=True`` to
    treat a one-sided ``None`` as compatible rather than merely ambiguous.
    """
    if stored == current:
        return "match"
    if stored is None or current is None:
        return "match" if lenient_none else "ambiguous"
    return "mismatch"


def optional_str(value: object) -> str | None:
    """A non-empty string, else ``None`` — for untrusted cache values."""
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class SessionMetadata:
    """Harness-generic session identity (#205, plan-of-record §2 decision 4).

    ``transcript_path`` is reserved for Stage 2 (rollout/transcript readers);
    Stage 1B never populates it.
    """

    harness: AgentType
    stable_id: str | None = None
    transcript_path: str | None = None
    native_uri: str | None = None
    display_name: str | None = None


@dataclass
class Agent:
    """Represents a running AI coding agent in a tmux pane.

    Phase has migrated from a stored ``DevPhase`` field to a
    ``PhaseSnapshot`` produced by ``agent_hq.resolver`` — ``Agent.phase``
    is now a read-only ``@property`` that returns the snapshot's
    ``effective_phase`` for display-only consumers. Mutating phase
    in-place is no longer supported; build a new snapshot and assign
    ``phase_snapshot`` instead.
    """

    pane_id: str
    session_name: str
    window_index: int
    pane_index: int
    agent_type: AgentType
    cwd: str
    repo_root: str | None = None
    repo_name: str | None = None  # From git remote origin, or directory basename
    branch: str | None = None
    worktree_name: str | None = None  # Basename of worktree path if not main
    raw_command: str = ""
    state: AgentState = AgentState.UNKNOWN
    session_slug: str | None = None  # Claude Code session name (e.g., "integrate-session-slugs")
    phase_snapshot: PhaseSnapshot | None = None
    file_stats: "FileStats | None" = None
    latest_summary: str | None = None
    # bridgeSessionId for Claude Code remote-control deep-link
    # (https://claude.ai/code/{id}). Only populated for Claude agents on
    # versions that write the field; older versions get None.
    claude_session_id: str | None = None
    # sessionId from the same Claude Code session JSON — used as the
    # transcript identity for hook-state session scoping (distinct from
    # the bridge id used for override session matching).
    claude_transcript_session_id: str | None = None
    # Codex root thread UUID (rollout session_meta.payload.id). Serves as
    # stable_id, resume id, and codex:// deep-link id. None when uncaptured.
    codex_thread_id: str | None = None
    # Path to the codex rollout JSONL for this thread (#206 Stage 2): from
    # the hook state file's transcript_path when the codex hook is
    # installed, else the cwd/mtime heuristic. Feeds the resolver's phase
    # read and SessionMetadata.transcript_path. None when uncaptured.
    codex_rollout_path: str | None = None
    # Foreground harness process, never the Codex app-server child.
    agent_pid: int | None = None
    status_snapshot: LiveStatus | None = None
    # Claude session-registry ``name`` / ``nameSource`` (#287): the handle a
    # native ``@name`` / ``SendMessage`` addresses. Registry-only (no
    # transcript fallback); ``derived`` names change on /rename.
    native_name: str | None = None
    native_name_source: str | None = None

    @property
    def status_label(self) -> str:
        return self.status_snapshot.label if self.status_snapshot else self.state.value.title()

    @property
    def status_reason(self) -> str:
        return self.status_snapshot.reason if self.status_snapshot else ""

    @property
    def phase(self) -> DevPhase | None:
        """Effective phase for UI display. Read-only shim over ``phase_snapshot``."""
        return self.phase_snapshot.effective_phase if self.phase_snapshot else None

    @property
    def stable_key(self) -> str:
        """Identity that survives a tmux pane renumber (#132).

        ``pane_id`` (e.g. ``%54``) is *not* stable — tmux renumbers panes on
        a server restart / ``tmux-resurrect`` restore — so keying an agent's
        live identity on it makes a still-running agent's card vanish when it
        comes back under a new number. Claude agents instead key on the
        transcript session id (stable across a resume); everything else falls
        back to ``tool:cwd:pane_id``.

        Mirrors the ``recover.identity`` rule, with one deliberate divergence:
        ``recover`` can multiset-count its diff so it keys the fallback on a
        bare ``tool:cwd``; here the key indexes a live ``dict`` where a
        collision would silently drop a card, so the fallback keeps the
        ``pane_id`` suffix to stay unique. The trade-off is that session-less
        agents (non-Claude, or older Claude without a transcript id) don't gain
        renumber-stability — acceptable, since the reported failure is a Claude
        session, and this is no worse than the prior pane-keyed behavior.

        Now generalized (#205): every harness prefers its ``session_metadata``
        ``stable_id`` before falling back to the ``tool:cwd:pane_id`` form.
        """
        stable_id = self.session_metadata.stable_id
        if stable_id:
            return stable_id
        return self.legacy_stable_key

    @property
    def session_metadata(self) -> SessionMetadata:
        if self.agent_type is AgentType.CLAUDE:
            return SessionMetadata(
                harness=self.agent_type,
                stable_id=self.claude_transcript_session_id,
                native_uri=(
                    f"https://claude.ai/code/{self.claude_session_id}"
                    if self.claude_session_id
                    else None
                ),
                display_name=self.session_label,
            )
        if self.agent_type is AgentType.CODEX:
            return SessionMetadata(
                harness=self.agent_type,
                stable_id=self.codex_thread_id,
                transcript_path=self.codex_rollout_path,
                native_uri=(
                    f"codex://threads/{self.codex_thread_id}" if self.codex_thread_id else None
                ),
                display_name=self.session_label,
            )
        return SessionMetadata(harness=self.agent_type, display_name=self.session_label)

    @property
    def legacy_stable_key(self) -> str:
        """The pre-#205 fallback key form; kept for pinned-store compat."""
        return f"{self.agent_type.value}:{self.cwd}:{self.pane_id}"

    def is_pinned_in(self, pinned: set[str]) -> bool:
        """True if either key form is in ``pinned`` (#205 Task 7).

        A codex agent's ``stable_key`` upgrades from ``legacy_stable_key`` to
        its thread UUID once ``codex_thread_id`` is captured (Tasks 1-3). A
        pin written before that upgrade is keyed on the old form, so
        membership must accept either key or the pin silently orphans.
        """
        return self.stable_key in pinned or self.legacy_stable_key in pinned

    @property
    def override_session_id(self) -> str | None:
        """Session id used for override entry matching (claude: bridge id)."""
        if self.agent_type is AgentType.CODEX:
            return self.codex_thread_id
        return self.claude_session_id

    @property
    def session_label(self) -> str:
        """Display label: session slug if available, otherwise agent type."""
        if self.session_slug:
            return self.session_slug
        return self.agent_type.value

    @property
    def display_name(self) -> str:
        """Human-readable name for this agent."""
        return self.agent_type.value

    @property
    def location(self) -> str:
        """Display string for agent's location (repo/worktree or cwd)."""
        if self.repo_root:
            repo_name = self.repo_root.rsplit("/", 1)[-1]
            if self.worktree_name:
                return f"{repo_name}/{self.worktree_name}"
            return repo_name
        return self.cwd

    def to_dict(self) -> dict:
        """Serialize Agent to a JSON-compatible dict."""
        return {
            "pane_id": self.pane_id,
            "session_name": self.session_name,
            "window_index": self.window_index,
            "pane_index": self.pane_index,
            "agent_type": self.agent_type.value,
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "repo_name": self.repo_name,
            "branch": self.branch,
            "worktree_name": self.worktree_name,
            "raw_command": self.raw_command,
            "state": self.state.value,
            "session_slug": self.session_slug,
            "phase_snapshot": self.phase_snapshot.to_dict() if self.phase_snapshot else None,
            "file_stats": self.file_stats.to_dict() if self.file_stats else None,
            "latest_summary": self.latest_summary,
            "claude_session_id": self.claude_session_id,
            "claude_transcript_session_id": self.claude_transcript_session_id,
            "harness": self.agent_type.value,
            "stable_id": self.session_metadata.stable_id,
            "codex_thread_id": self.codex_thread_id,
            "codex_rollout_path": self.codex_rollout_path,
            "agent_pid": self.agent_pid,
            "status_snapshot": self.status_snapshot.to_dict() if self.status_snapshot else None,
            "native_name": self.native_name,
            "native_name_source": self.native_name_source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Agent":
        """Deserialize an Agent from a dict (cache schema v2 shape).

        Carries a compat window (#205 Stage 1B) for the identity fields: old
        names (``agent_type``, ``claude_transcript_session_id``) and new
        names (``harness``, ``stable_id``, ``codex_thread_id``) are both
        read, with the new names winning when both are present and disagree.
        """
        snap_raw = data.get("phase_snapshot")
        file_stats_val = data.get("file_stats")
        try:
            agent_type = AgentType(data.get("harness") or data["agent_type"])
        except ValueError:
            agent_type = AgentType(data["agent_type"])
        stable_id = data.get("stable_id")
        transcript_sid = data.get("claude_transcript_session_id")
        if stable_id and agent_type is AgentType.CLAUDE:
            transcript_sid = stable_id  # new wins on disagreement
        codex_thread_id = data.get("codex_thread_id")
        if codex_thread_id is None and agent_type is AgentType.CODEX:
            codex_thread_id = stable_id
        return cls(
            pane_id=data["pane_id"],
            session_name=data["session_name"],
            window_index=data["window_index"],
            pane_index=data["pane_index"],
            agent_type=agent_type,
            cwd=data["cwd"],
            repo_root=data.get("repo_root"),
            repo_name=data.get("repo_name"),
            branch=data.get("branch"),
            worktree_name=data.get("worktree_name"),
            raw_command=data["raw_command"],
            state=AgentState(data["state"]),
            session_slug=data.get("session_slug"),
            phase_snapshot=PhaseSnapshot.from_dict(snap_raw) if snap_raw else None,
            file_stats=FileStats.from_dict(file_stats_val) if file_stats_val else None,
            latest_summary=data.get("latest_summary"),
            claude_session_id=data.get("claude_session_id"),
            claude_transcript_session_id=transcript_sid,
            codex_thread_id=codex_thread_id,
            codex_rollout_path=data.get("codex_rollout_path"),
            agent_pid=(data["agent_pid"] if type(data.get("agent_pid")) is int
                       and data["agent_pid"] > 0 else None),
            status_snapshot=(LiveStatus.from_dict(data["status_snapshot"])
                             if data.get("status_snapshot") is not None else None),
            native_name=optional_str(data.get("native_name")),
            native_name_source=optional_str(data.get("native_name_source")),
        )


@dataclass
class WorktreeGroup:
    """A group of agents in the same git worktree."""

    repo_name: str  # Basename of repo root
    repo_root: str
    worktree_path: str
    worktree_name: str  # Branch name or "main"
    branch: str
    agents: list[Agent] = field(default_factory=list)


@dataclass
class RepoGroup:
    """A group of worktrees in the same repository."""

    repo_name: str
    repo_root: str
    worktrees: list[WorktreeGroup] = field(default_factory=list)

    @property
    def agent_count(self) -> int:
        """Total number of agents in this repo."""
        return sum(len(wt.agents) for wt in self.worktrees)


class Priority(Enum):
    """Issue priority from GitHub Project board."""

    NEED = "Need"
    WANT = "Want"
    IDEA = "Idea"
    NO_PRI = ""


class Complexity(Enum):
    """Complexity tier for dispatch classification."""

    TRIVIAL = "trivial"
    STRAIGHTFORWARD = "straightforward"
    COMPLEX = "complex"
    EXPLORATORY = "exploratory"


@dataclass
class Issue:
    """A GitHub issue from a project board."""

    number: int
    title: str
    repo: str  # "owner/name" format
    status: str | None
    priority: Priority
    labels: list[str] = field(default_factory=list)
    body: str = ""
    item_id: str = ""  # GitHub Project item ID (for status updates)
    project_number: int = 0  # Which project this came from
    # Owner the project was fetched under (#81). Project numbers are
    # owner-scoped, so the number alone is ambiguous across orgs/users.
    # "" for issues from pre-#81 disk caches.
    project_owner: str = ""
    updated_at: str | None = None  # ISO-8601 (gh ``updatedAt``); None when unknown
    created_at: str | None = None  # ISO-8601 (gh ``createdAt``); None when unknown

    @property
    def key(self) -> str:
        """Stable identity key: 'owner/repo#number'."""
        return f"{self.repo}#{self.number}"

    @property
    def repo_short(self) -> str:
        """Short repo name (without owner)."""
        return self.repo.split("/")[-1] if "/" in self.repo else self.repo

    @property
    def is_sentinel(self) -> bool:
        """Machine-filed by the log sentinel (#235) — carries its label."""
        return "sentinel" in self.labels

    def timestamp_for(self, field: str) -> str | None:
        """ISO timestamp matching a date sort field.

        ``"created"`` → ``created_at``; anything else → ``updated_at``. The
        single accessor every sort surface (``filters.sort_issues``, the TUI's
        ``build_issue_rows``, and the ``issue_card`` age badge) shares, so the
        list can never sort by one field while a card shows another.
        """
        return self.created_at if field == "created" else self.updated_at
