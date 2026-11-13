"""Enrich agent data with directory and git context."""

import hashlib
import json
import os
import re
import subprocess
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

import pygit2

from . import eventlog
from .adapters import get_adapter
from .hooks import (
    STALE_THRESHOLD_SECONDS,
    read_hook_identity,
    read_hook_summary,
)
from .models import Agent, AgentState, AgentType, optional_str
from .phase import _PYGIT2_ERRORS, _drop_cached_repo, _open_repo
from .transcript import transcript_path_for

if TYPE_CHECKING:
    from .models import FileStats

CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
# Local tracebacks for enrichment failures (#358). The eventlog row carries
# only the error type, repo and branch; the traceback — including the inner
# exception pygit2 hides behind its TypeError — stays on this machine.
ENRICH_ERRORS_FILE = Path.home() / ".cache" / "agent-hq" / "enrich-errors.log"
_ENRICH_ERRORS_MAX_BYTES = 512 * 1024
# One report per (message, repo, branch, error type) per window: the daemon
# ticks every ~2 s and a failure can repeat for minutes.
_FAILURE_REPORT_INTERVAL_S = 900.0
_failure_reported: dict[tuple, float] = {}
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# (path, mtime) -> (thread_id, cwd); parse each rollout head at most once.
# Bounded below in _read_rollout_meta so it can't grow unboundedly across
# daemon polls as old rollout files scroll out of the max_age_days window.
_rollout_meta_cache: dict[tuple[str, float], tuple[str | None, str | None]] = {}
_ROLLOUT_META_CACHE_MAX = 4000


def get_pane_cwd(pane_id: str) -> str:
    """Get the current working directory of a tmux pane."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_current_path}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""
    except FileNotFoundError:
        return ""


def get_git_repo_root(cwd: str) -> str | None:
    """Get the root directory of the git repository containing cwd."""
    repo = _open_repo(cwd)
    if repo is None or repo.workdir is None:
        return None
    return repo.workdir.rstrip("/")


def get_git_branch(cwd: str) -> str | None:
    """Get the current git branch for the directory.

    Returns "HEAD" on detached HEAD (matching ``git rev-parse --abbrev-ref
    HEAD``), None when there's no HEAD at all (e.g. unborn / non-git).
    """
    repo = _open_repo(cwd)
    if repo is None:
        return None
    try:
        return repo.head.shorthand
    except OSError:
        # Drop the cached handle too: an OS-level read failure (#324) is
        # usually transient, and a fresh open next tick is the cheapest retry.
        _drop_cached_repo(cwd)
        return None
    except pygit2.GitError:
        # Unborn HEAD (fresh `git init`) is permanent — keep the cached handle
        # or every tick would re-discover and re-open the repo (review finding).
        return None


def get_repo_name(repo_root: str) -> str:
    """
    Get the repository name, preferring the remote origin name over directory name.

    Tries to extract repo name from git remote URL (e.g., 'my-repo' from
    'git@github.com:user/my-repo.git'). Falls back to directory basename.
    """
    repo = _open_repo(repo_root)
    if repo is None:
        return Path(repo_root).name
    try:
        url = repo.remotes["origin"].url
    except (KeyError, *_PYGIT2_ERRORS):
        return Path(repo_root).name

    # Parse repo name from various URL formats:
    # - git@github.com:user/repo.git
    # - https://github.com/user/repo.git
    # - https://github.com/user/repo
    if url.endswith(".git"):
        url = url[:-4]

    repo_name = url.rsplit("/", 1)[-1]

    # SSH form (git@host:user/repo) leaves "host:user/repo" if no slash split
    if ":" in repo_name and "/" not in repo_name:
        repo_name = repo_name.rsplit(":", 1)[-1]

    return repo_name or Path(repo_root).name


def get_worktree_info(cwd: str) -> tuple[str | None, str | None, bool]:
    """
    Determine if cwd is in a worktree and get worktree details.

    Returns:
        (main_repo_root, worktree_name, is_linked)
        - main_repo_root: Path to the main repository (not the worktree)
        - worktree_name: Name of the worktree if linked, None if main
        - is_linked: True if this is a linked worktree
    """
    repo = _open_repo(cwd)
    if repo is None:
        return None, None, False

    # repo.path is the per-worktree .git dir.
    #   Main:    /main/.git/
    #   Linked:  /main/.git/worktrees/<name>/
    git_path = Path(repo.path.rstrip("/")).resolve()

    if git_path.parent.name == "worktrees":
        # Linked worktree — resolve main .git via the commondir file
        # (matches what `git rev-parse --git-common-dir` reads).
        commondir_file = git_path / "commondir"
        try:
            common_raw = commondir_file.read_text().strip()
        except OSError:
            # Missing, or the OS refused the read (#324 class — with the
            # Repository handle cached this is the only read on the path,
            # so it must degrade, never abort the daemon tick). The
            # standard layout puts the main .git two levels up.
            common_path = git_path.parent.parent
        else:
            common_path = (
                Path(common_raw)
                if Path(common_raw).is_absolute()
                else (git_path / common_raw).resolve()
            )

        main_repo_root = str(common_path.parent)

        workdir = repo.workdir
        if workdir:
            worktree_name = Path(workdir.rstrip("/")).name
            return main_repo_root, worktree_name, True
        return main_repo_root, None, True

    # Main worktree
    return str(git_path.parent), None, False


def get_git_context(cwd: str) -> tuple[str | None, str | None, str | None]:
    """
    Get git context for a directory.

    Returns:
        (repo_root, branch, worktree_name)
        - repo_root: Path to the MAIN git repository (not the worktree directory)
        - branch: Current branch name
        - worktree_name: Name of the worktree if in a linked worktree, else None
    """
    if not cwd:
        return None, None, None

    # First check if it's a git repo at all
    toplevel = get_git_repo_root(cwd)
    if not toplevel:
        return None, None, None

    # Get worktree info - this gives us the MAIN repo root
    main_repo_root, worktree_name, _ = get_worktree_info(cwd)

    # Use main repo root if available, otherwise fall back to toplevel
    repo_root = main_repo_root or toplevel

    branch = get_git_branch(cwd)

    return repo_root, branch, worktree_name


def _report_enrichment_failure(
    message: str, exc: BaseException, *, repo: str | None, branch: str | None
) -> None:
    """Warn (stable literal, no exception text) and keep the traceback locally.

    Never raises: diagnostics must not become the next thing that aborts a
    tick. Throttled here per (message, repo, branch, error type, cause type),
    so eventlog's own per-message dedupe is switched off — it would hide a
    second repo's failure behind the first for its whole window.
    """
    cause = exc.__cause__ or exc.__context__
    cause_type = type(cause).__name__ if cause is not None else None
    key = (message, repo, branch, type(exc).__name__, cause_type)
    now = time.monotonic()
    for stale in [k for k, t in _failure_reported.items() if now - t >= _FAILURE_REPORT_INTERVAL_S]:
        del _failure_reported[stale]
    if key in _failure_reported:
        return
    _failure_reported[key] = now
    extra = {"error_type": type(exc).__name__, "repo": repo, "branch": branch}
    if cause_type is not None:
        extra["cause_type"] = cause_type
    try:
        eventlog.emit("warn", message, extra, min_interval_s=0)
    except Exception:
        pass
    try:
        ENRICH_ERRORS_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            if ENRICH_ERRORS_FILE.stat().st_size > _ENRICH_ERRORS_MAX_BYTES:
                # Keep one previous window: rename, never truncate in place.
                os.replace(ENRICH_ERRORS_FILE, ENRICH_ERRORS_FILE.with_name(
                    ENRICH_ERRORS_FILE.name + ".1"))
        except FileNotFoundError:
            pass
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with ENRICH_ERRORS_FILE.open("a") as fh:
            fh.write(f"--- {stamp} {message} repo={repo} branch={branch} pid={os.getpid()}\n")
            fh.write("".join(traceback.format_exception(exc)))
    except OSError:
        pass


def git_diff_shortstat(
    repo_root: str, branch: str, *, base_ref: str = "main"
) -> "FileStats | None":
    """Return FileStats for ``base_ref..branch`` diff, or None when empty/error.

    Uses ``revparse_single`` so any rev spec works (branch, tag, remote ref,
    raw SHA, "HEAD" on detached HEAD).
    """
    from .models import FileStats

    repo = _open_repo(repo_root)
    if repo is None:
        return None
    try:
        base_oid = repo.revparse_single(base_ref).peel(pygit2.Commit).id
        branch_oid = repo.revparse_single(branch).peel(pygit2.Commit).id
        try:
            diff = repo.diff(base_oid, branch_oid)
        except TypeError as exc:
            # #358: pygit2's ``Repository.diff`` turns a failed ``peel(Tree)``
            # (the commit's tree can't be read) into ``TypeError('unexpected
            # "<class '_pygit2.Commit'>"')``, hiding the real KeyError in
            # ``__context__``. Uncaught, it aborted the daemon tick for every
            # agent. Degrade this agent's stats like any git error (#283),
            # drop the handle, and keep the inner error for diagnosis. Only
            # the diff call is covered, so a TypeError bug elsewhere still
            # surfaces.
            _drop_cached_repo(repo_root)
            _report_enrichment_failure(
                "git diff stats failed", exc, repo=repo_root, branch=branch
            )
            return None
        stats = diff.stats
        files = stats.files_changed
        insertions = stats.insertions
        deletions = stats.deletions
    except (KeyError, *_PYGIT2_ERRORS):
        _drop_cached_repo(repo_root)
        return None

    if files == 0 and insertions == 0 and deletions == 0:
        return None
    return FileStats(files=files, insertions=insertions, deletions=deletions)


# ANSI escape code pattern for stripping terminal formatting
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07")

# Unicode spinner characters used by various CLI tools
SPINNER_CHARS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒⣾⣽⣻⢿⡿⣟⣯⣷")

# Claude Code status line spinner characters (may need extending as CC adds more)
CLAUDE_STATUS_CHARS = "·✢✳✶✻✽"

# Status line pattern: "✶ Meandering…" or "✻ Fixing serializer NS.data index…"
CLAUDE_STATUS_LINE_PATTERN = re.compile(r"[" + CLAUDE_STATUS_CHARS + r"]\s+.+?…")

# Patterns indicating Claude Code is waiting for permission or user input
PERMISSION_PATTERNS = [
    # Permission prompts
    r"allow\s+(once|always)",
    r"\[y/n\]",
    r"allow this",
    r"do you want to",
    r"would you like to",
    # Sandbox permission prompts
    r"outside of sandbox",
    r"do you want to allow this",
    # Question UI patterns (AskUserQuestion)
    r"enter to select",  # Footer navigation hint
    r"esc to cancel",  # Footer cancel hint
    r"(?<![-*+] )\[ \]",  # Multi-select unchecked checkbox (not markdown list)
    r"(?<![-*+] )\[x\]",  # Multi-select checked checkbox (not markdown list)
    # Selection cursor with numbered options (e.g., "❯ 1. Yes")
    r"❯\s*\d+\.",
]

# Subagent execution indicators (status line may not be visible during subagent runs)
CLAUDE_SUBAGENT_PATTERN = re.compile(
    r"running \d+ agents?…|ctrl\+b ctrl\+b \(twice\) to run in background"
)

# Codex CLI spinner characters used in multi-line ASCII art spinners
CODEX_SPINNER_CHARS = "●○◉"

# Codex CLI patterns indicating WAITING state (plan confirmation, command approval, questions)
CODEX_WAITING_PATTERNS = [
    r"press enter to confirm",
    r"esc to go back",
    r"approve and run the command",
    r"allow command\?",
    r"yes\s+always\s+no,\s*provide feedback",
    # Question UI patterns (brainstorming multi-choice prompts)
    r"enter to submit answer",
    r"question \d+/\d+",
    r"\u2190/\u2192 to navigate questions",  # ←/→ to navigate questions
]

# Codex CLI patterns indicating RUNNING state
CODEX_RUNNING_PATTERNS = [
    r"esc to interrupt\)",  # "(51s • esc to interrupt)"
    r"\d+s\s*[•·.]\s*esc to interrupt",  # "51s • esc to interrupt" with timer prefix
]


def get_pane_content(pane_id: str, lines: int = 25, ansi: bool = False) -> str:
    """
    Capture the last N lines of content from a tmux pane.

    Args:
        pane_id: The tmux pane identifier (e.g., "%5")
        lines: Number of lines to capture from the end
        ansi: If True, preserve ANSI escape sequences via -e flag

    Returns:
        The captured pane content, or empty string on error
    """
    try:
        cmd = ["tmux", "capture-pane", "-t", pane_id, "-p"]
        if ansi:
            # -S captures scrollback history, -e preserves ANSI escape sequences
            cmd.insert(2, "-S")
            cmd.insert(3, f"-{lines}")
            cmd.insert(4, "-e")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        content = result.stdout
        content_lines = content.split("\n")
        tail = content_lines[-lines:]
        if ansi:
            # Strip trailing whitespace (tmux pads lines to full pane width)
            tail = [line.rstrip() for line in tail]
        return "\n".join(tail)
    except subprocess.CalledProcessError:
        return ""
    except FileNotFoundError:
        return ""


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return ANSI_ESCAPE_PATTERN.sub("", text)


def detect_claude_state(content: str) -> AgentState:
    """
    Analyze pane content to determine Claude Code's current state.

    Detection priority:
    1. Check for waiting/permission patterns in RECENT content only
       (avoids false positives from scrollback)
    2. Check for running indicators (esc to interrupt, spinners)
    3. Check for idle indicators (INSERT mode, prompt)
    4. Default to idle if nothing matches

    Args:
        content: Raw pane content (last N lines)

    Returns:
        The detected AgentState
    """
    if not content.strip():
        return AgentState.UNKNOWN

    # Strip ANSI codes for pattern matching
    clean_content = strip_ansi(content)
    clean_lower = clean_content.lower()

    # Get recent content (last 10 non-empty lines) for waiting detection
    # Permission prompts appear at bottom, not in scrollback
    # Filter empty lines so trailing whitespace doesn't push prompts out of window
    # Use 10 lines to handle narrow screens (phones) where prompts wrap more
    non_empty_lines = [line for line in clean_lower.split("\n") if line.strip()]
    recent_content = "\n".join(non_empty_lines[-10:]) if len(non_empty_lines) > 10 else clean_lower

    # Check for WAITING - only in recent content to avoid scrollback false positives
    # Even with "(esc to interrupt" visible, a permission dialog at the bottom
    # means we're blocked (e.g., sandbox network request prompts)
    for pattern in PERMISSION_PATTERNS:
        if re.search(pattern, recent_content):
            return AgentState.WAITING

    # Check for RUNNING - Claude Code status line pattern
    # New format: "✶ Meandering…" or "✶ Meandering… (43s · ↓ 543 tokens)"
    # Old format: "✶ Wandering… (esc to interrupt · 50s · ↑ 938 tokens)"
    if CLAUDE_STATUS_LINE_PATTERN.search(clean_content):
        return AgentState.RUNNING

    # Check for RUNNING - legacy interrupt hints
    if (
        "(esc to interrupt" in clean_lower
        or "esc to interrupt ·" in clean_lower
        or "ctrl+c to interrupt" in clean_lower
    ):
        return AgentState.RUNNING

    # Check for spinner characters (indicates running)
    if any(char in content for char in SPINNER_CHARS):
        return AgentState.RUNNING

    # Check for RUNNING - subagent execution indicators
    # When Claude Code runs subagents, the status line may not be visible
    # but the subagent progress tree indicates active work
    if CLAUDE_SUBAGENT_PATTERN.search(recent_content):
        return AgentState.RUNNING

    # Check for idle indicators
    # 1. "-- INSERT --" mode indicator (Claude Code shows this when waiting for input)
    if "-- insert --" in clean_lower:
        return AgentState.IDLE

    # 2. Look for "> " prompt anywhere in the content (not just last line)
    #    The prompt line starts with "> " at the beginning
    for line in clean_content.split("\n"):
        stripped = line.strip()
        # Match lines that are just ">" or start with "> "
        if stripped == ">" or stripped.startswith("> "):
            # Make sure there's no spinner on the same line
            if not any(char in line for char in SPINNER_CHARS):
                return AgentState.IDLE

    # Default to idle if we can't determine state
    return AgentState.IDLE


def detect_codex_state(content: str) -> AgentState:
    """
    Analyze pane content to determine Codex CLI's current state.

    Detection priority:
    1. Check for waiting/approval patterns in RECENT content
       (avoids false positives from scrollback)
    2. Check for running indicators (esc to interrupt)
    3. Check for idle indicators (› prompt, ? for shortcuts, % context left)
    4. Default to idle if nothing matches

    Args:
        content: Raw pane content (last N lines)

    Returns:
        The detected AgentState
    """
    if not content.strip():
        return AgentState.UNKNOWN

    # Strip ANSI codes for pattern matching
    clean_content = strip_ansi(content)
    clean_lower = clean_content.lower()

    # Get recent content (last 10 non-empty lines) for waiting detection
    # Same approach as Claude: filter empty lines, use last 10
    non_empty_lines = [line for line in clean_lower.split("\n") if line.strip()]
    recent_content = "\n".join(non_empty_lines[-10:]) if len(non_empty_lines) > 10 else clean_lower

    # Check for WAITING - only in recent content to avoid scrollback false positives
    for pattern in CODEX_WAITING_PATTERNS:
        if re.search(pattern, recent_content):
            return AgentState.WAITING

    # Check for RUNNING - "esc to interrupt" in the pane content
    # Safe to check full content because Codex uses a Ratatui TUI that redraws
    # the frame (old status lines don't persist in scrollback)
    for pattern in CODEX_RUNNING_PATTERNS:
        if re.search(pattern, clean_lower, re.MULTILINE):
            return AgentState.RUNNING

    # Check for IDLE - Codex-specific footer indicators
    # The › character appears in both idle prompts and selection UIs,
    # but WAITING is already checked above so › here means idle
    if "% context left" in clean_lower or "? for shortcuts" in clean_lower:
        return AgentState.IDLE

    # Check for the › prompt character on a line by itself or with text after it
    for line in clean_content.split("\n"):
        stripped = line.strip()
        if stripped == "\u203a" or stripped.startswith("\u203a "):
            return AgentState.IDLE

    # Default to idle if we can't determine state
    return AgentState.IDLE


def get_agent_state(
    pane_id: str,
    agent_type: AgentType,
    *,
    current_session_id: str | None = None,
    agent_pid: int | None = None,
) -> AgentState:
    """Read the shared native snapshot; hooks and terminal text are not live state."""
    from .status_collector import apply_live_status

    if agent_type is AgentType.CLAUDE and current_session_id is None and agent_pid is not None:
        _, current_session_id = get_claude_session_ids(agent_pid)

    agent = Agent(
        pane_id,
        "",
        0,
        0,
        agent_type,
        "",
        agent_pid=agent_pid,
        claude_transcript_session_id=(
            current_session_id if agent_type is AgentType.CLAUDE else None
        ),
        codex_thread_id=(current_session_id if agent_type is AgentType.CODEX else None),
    )
    apply_live_status([agent])
    return agent.state


def compute_content_fingerprint(pane_id: str) -> str:
    """
    Compute a fingerprint of pane content.

    Stored on a mute record when an agent is muted. Nothing compares it any
    more: mute is sticky (#303), so a content change never unmutes.

    The fingerprint is stable across window resizes (zoom, width changes) because it:
    1. Strips ANSI codes and spinner characters
    2. Splits into words (ignoring whitespace layout)
    3. Takes only the LAST N words (fixed count, not line-dependent)
    4. Hashes those words

    By using a fixed word count from the end, we're immune to resize changes
    that affect how many lines are visible. New content at the end will
    change the fingerprint, but reflow won't.

    Args:
        pane_id: The tmux pane identifier

    Returns:
        A 16-character hex string fingerprint, or empty string on error
    """
    # Capture more lines to ensure we have enough content even after resize
    content = get_pane_content(pane_id, lines=100)
    if not content.strip():
        return ""

    # Strip ANSI codes
    clean = strip_ansi(content)

    # Remove spinner characters
    for char in SPINNER_CHARS:
        clean = clean.replace(char, "")

    # Remove Claude Code status line spinner characters
    for char in CLAUDE_STATUS_CHARS:
        clean = clean.replace(char, "")

    # Remove Codex CLI spinner characters
    for char in CODEX_SPINNER_CHARS:
        clean = clean.replace(char, "")

    # Split into words (ignoring all whitespace)
    words = clean.split()

    # Keep words 3+ characters to filter noise
    meaningful = [w for w in words if len(w) >= 3]

    # Take only the LAST 150 words - this makes us immune to resize
    # because we're not dependent on line count, just word count from the end
    TAIL_WORDS = 150
    tail_words = meaningful[-TAIL_WORDS:] if len(meaningful) > TAIL_WORDS else meaningful

    # Hash the word sequence
    word_string = " ".join(tail_words)
    return hashlib.md5(word_string.encode()).hexdigest()[:16]


def get_claude_session_slug(agent_pid: int | None, cwd: str) -> str | None:
    """
    Extract the session name for a running Claude Code instance.

    Reads the session file (~/.claude/sessions/{pid}.json) to get the sessionId,
    then finds the JSONL conversation file and extracts the session name.

    Prefers agentName (the user-visible session name shown in Claude Code's UI)
    over slug (an internal identifier). Returns None if no agentName is found.

    Args:
        agent_pid: The PID of the Claude Code process
        cwd: The current working directory (unused, kept for future flexibility)

    Returns:
        The session name (e.g., "integrate-session-slugs-display") or None
    """
    if agent_pid is None:
        return None

    session_file = CLAUDE_SESSIONS_DIR / f"{agent_pid}.json"
    if not session_file.exists():
        return None

    try:
        with open(session_file) as f:
            session_data = json.load(f)

        # Check session file first — "name" field is set immediately on rename
        name = session_data.get("name")
        if name:
            return name

        # Fall back to JSONL conversation file — "agentName" in records
        session_id = session_data.get("sessionId")
        session_cwd = session_data.get("cwd")
        if not session_id or not session_cwd:
            return None

        # Single source of the transcript-path encoding (see transcript.py).
        # Candidates are current-scheme first, legacy (pre-underscore-
        # replacement) fallback second — use the first that exists.
        jsonl_path = None
        for candidate in transcript_path_for(session_cwd, session_id):
            p = Path(candidate)
            if p.exists():
                jsonl_path = p
                break
        if jsonl_path is None:
            return None

        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    agent_name = record.get("agentName")
                    if agent_name:
                        return agent_name
                except json.JSONDecodeError:
                    continue

        return None

    except (OSError, json.JSONDecodeError, KeyError):
        return None


def get_claude_session_ids(agent_pid: int | None) -> tuple[str | None, str | None]:
    """Read ``(bridgeSessionId, sessionId)`` from the Claude Code session file.

    Returns a tuple of (bridge_id, transcript_id):

    * **bridge_id** (``bridgeSessionId``) drives the Claude Code remote-
      control deep link (``https://claude.ai/code/{id}``) and is the
      session-scoping key for overrides (issue #65).
    * **transcript_id** (``sessionId``) names the JSONL transcript file
      and is the session-scoping key for hook state reads (issue #73 —
      stale state from a prior session in the same pane number must
      not surface as live state).

    Older Claude Code versions may write only one of these. Missing
    keys yield ``None`` for the missing slot; ``""`` normalizes to
    ``None`` to match ``hooks._normalize_session``.
    """
    if agent_pid is None:
        return None, None

    session_file = CLAUDE_SESSIONS_DIR / f"{agent_pid}.json"
    if not session_file.exists():
        return None, None

    try:
        with open(session_file) as f:
            session_data = json.load(f)
        bridge = session_data.get("bridgeSessionId") or None
        transcript = session_data.get("sessionId") or None
        return bridge, transcript
    except (OSError, json.JSONDecodeError):
        return None, None


def get_claude_native_name(
    agent_pid: int | None,
) -> tuple[str | None, str | None, str | None]:
    """Read ``(name, nameSource, sessionId)`` from the Claude session registry (#287).

    ``name`` is what native ``@name`` / ``SendMessage`` addressing uses, so
    unlike ``get_claude_session_slug`` there is no transcript ``agentName``
    fallback — that value is not addressable. ``nameSource`` is ``derived``
    for auto-generated names (they change on ``/rename``), ``user`` for
    ``--name`` / ``/rename``. ``sessionId`` comes from the same read so the
    caller can check the name belongs to the transcript it already holds —
    the registry is rewritten in place on ``/clear``. Any missing or
    malformed value yields ``None``; no name means ``(None, None, None)``.
    """
    if agent_pid is None:
        return None, None, None
    try:
        with open(CLAUDE_SESSIONS_DIR / f"{agent_pid}.json") as f:
            session_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None, None
    if not isinstance(session_data, dict):
        return None, None, None
    name = optional_str(session_data.get("name"))
    if name is None:
        return None, None, None
    return (
        name,
        optional_str(session_data.get("nameSource")),
        optional_str(session_data.get("sessionId")),
    )


_CODEX_THREAD_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _read_rollout_meta(path: Path, mtime: float) -> tuple[str | None, str | None]:
    """Parse a rollout file's first line (``session_meta``) into (thread_id, cwd).

    Cached by (path, mtime) so a live codex agent's still-growing rollout is
    only parsed once per distinct mtime across a daemon's poll loop.

    Only binds rollouts written by an interactive codex TUI: the payload's
    ``originator`` field must equal exactly ``"codex-tui"``. A missing
    ``originator`` (very old codex versions) or any other value (e.g.
    ``"codex-exec"`` / ``"codex_exec"`` for exec/subagent/IDE runs) is
    rejected -- otherwise a non-interactive run could steal a pane's
    identity out from under the interactive session actually running in
    it. Old codex versions without the field simply lose capture, which
    is an acceptable trade-off.

    ``thread_id`` is accepted only when it fullmatches the UUID shape
    (``_CODEX_THREAD_ID_RE``) -- this captured id eventually reaches
    ``("codex", "resume", thread_id)`` argv (``adapters.CodexAdapter.resume_spec``),
    so an unvalidated value starting with ``-`` would be interpreted as a flag
    rather than a positional argument (an argv-injection class).
    """
    key = (str(path), mtime)
    if key in _rollout_meta_cache:
        return _rollout_meta_cache[key]
    if len(_rollout_meta_cache) >= _ROLLOUT_META_CACHE_MAX:
        _rollout_meta_cache.clear()
    thread_id = cwd = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first = f.readline()
        obj = json.loads(first)
        if obj.get("type") == "session_meta":
            payload = obj.get("payload") or {}
            if payload.get("originator") == "codex-tui":
                tid, pcwd = payload.get("id"), payload.get("cwd")
                thread_id = (
                    tid if isinstance(tid, str) and _CODEX_THREAD_ID_RE.fullmatch(tid) else None
                )
                cwd = pcwd if isinstance(pcwd, str) and pcwd else None
    except (OSError, ValueError):
        pass
    _rollout_meta_cache[key] = (thread_id, cwd)
    return thread_id, cwd


def get_codex_rollout(
    cwd: str, *, sessions_dir: Path | None = None, max_age_days: int = 3
) -> tuple[str, str] | None:
    """Newest recent rollout ``(thread_id, path)`` whose session_meta cwd
    matches ``cwd``.

    Heuristic association fallback (#206: the codex hook's identity write
    is preferred when installed): a live codex process appends to its
    rollout every turn, so the newest cwd-matching rollout is the pane's
    thread. Ambiguity between two live agents in one cwd is handled by
    ``_clear_duplicate_codex_thread_ids``.
    """
    base = sessions_dir if sessions_dir is not None else CODEX_SESSIONS_DIR
    cutoff = time.time() - max_age_days * 86400
    best: tuple[float, str, str] | None = None
    try:
        candidates = base.glob("*/*/*/rollout-*.jsonl")
    except OSError:
        return None
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        thread_id, meta_cwd = _read_rollout_meta(path, mtime)
        if thread_id and meta_cwd == cwd and (best is None or mtime > best[0]):
            best = (mtime, thread_id, str(path))
    return (best[1], best[2]) if best else None


def get_codex_thread_id(
    cwd: str, *, sessions_dir: Path | None = None, max_age_days: int = 3
) -> str | None:
    """Thread-id-only view of ``get_codex_rollout`` (pre-#206 shape)."""
    rollout = get_codex_rollout(cwd, sessions_dir=sessions_dir, max_age_days=max_age_days)
    return rollout[0] if rollout else None


# (path, mtime) -> summary; the rollout-tail fallback only re-reads a
# rollout that actually grew since the last poll (2s daemon tick).
_codex_summary_cache: dict[tuple[str, float], str | None] = {}
_CODEX_SUMMARY_CACHE_MAX = 256


def _codex_summary_from_rollout(path: str, thread_id: str) -> str | None:
    """Rollout-tail ``latest_summary`` fallback for codex (#206), used when
    the hook wrote none (not installed / not yet a Stop event). Cached by
    (path, mtime). An adapter/reader failure costs only the summary —
    degradation rule."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    key = (path, mtime)
    if key in _codex_summary_cache:
        return _codex_summary_cache[key]
    if len(_codex_summary_cache) >= _CODEX_SUMMARY_CACHE_MAX:
        _codex_summary_cache.clear()
    adapter = get_adapter(AgentType.CODEX)
    try:
        summary = adapter.transcript_summary(path, thread_id) if adapter else None
    except Exception:
        summary = None
    _codex_summary_cache[key] = summary
    return summary


def _clear_duplicate_codex_thread_ids(
    agents: list[Agent], pids: dict[str, int | None] | None = None
) -> None:
    """A shared stable_key silently drops a card — never let two live codex
    agents (e.g. two panes in one cwd) carry the same thread id.

    Refinement (#206): when exactly one claimant's id is hook-sourced
    (its pane state file names the same thread + cwd), that claimant is
    the pane actually running the session — keep it and clear only the
    heuristic strays. Any other shape (all heuristic, or several
    hook-sourced files somehow naming one thread) clears every claimant,
    matching the pre-#206 safety behavior.
    """
    counts = Counter(a.codex_thread_id for a in agents if a.codex_thread_id)
    for dup_id, n in counts.items():
        if n <= 1:
            continue
        claimants = [a for a in agents if a.codex_thread_id == dup_id]
        hook_sourced = []
        for a in claimants:
            # Same pid-validated read ``enrich_agent`` used. Without the
            # pid a stale-but-live owner reads as expired, no claimant
            # looks hook-sourced, and BOTH agents get cleared.
            identity = read_hook_identity(a.pane_id, agent_pid=(pids or {}).get(a.pane_id))
            if (
                identity is not None
                and identity.harness == "codex"
                and identity.session_id == dup_id
                and identity.cwd == a.cwd
            ):
                hook_sourced.append(a)
        keep = hook_sourced[0] if len(hook_sourced) == 1 else None
        for a in claimants:
            if a is not keep:
                a.codex_thread_id = None
                a.codex_rollout_path = None
                # The rollout-tail summary was read from the very rollout
                # this agent has just been deemed not to own — dropping the
                # id while keeping the text would leave another session's
                # reply on the card. A hook-written summary is per-pane and
                # therefore still correct, so it survives.
                if a.latest_summary is not None and a.latest_summary != read_hook_summary(
                    a.pane_id
                ):
                    a.latest_summary = None


def enrich_agent(raw: dict) -> Agent:
    """
    Enrich raw agent data with cwd, git context, state, and Claude session ids.

    Phase resolution is NOT done here. The resolver
    (``agent_hq.resolver.resolve_phase_snapshot``) is the single place
    that produces a ``PhaseSnapshot``; this function leaves
    ``phase_snapshot=None`` so the daemon's poll loop (or the watcher /
    live-fallback) can fill it in.

    Reorder rationale: session ids must be read BEFORE
    ``get_agent_state`` so the state read can session-scope its hook
    file lookup. Without this order, a hook state file from a prior
    Claude session could surface as live state in the brief window
    between session boundary and the next daemon prune.

    Args:
        raw: Dict with keys: session_name, window_index, pane_index, pane_id, pane_pid, command

    Returns:
        Agent with phase_snapshot=None — populated by the resolver
    """
    pane_id = raw["pane_id"]
    cwd = get_pane_cwd(pane_id)
    repo_root, branch, worktree_name = get_git_context(cwd)

    # Get repo name from remote origin URL, or fallback to directory name
    repo_name = get_repo_name(repo_root) if repo_root else None

    # Determine agent type for state detection
    agent_type = AgentType.from_command(raw["command"])

    # Read session ids first so the state read can session-scope its
    # hook file lookup.
    session_slug = None
    claude_session_id = None
    claude_transcript_session_id = None
    native_name = native_name_source = None
    if agent_type == AgentType.CLAUDE:
        session_slug = get_claude_session_slug(raw.get("agent_pid"), cwd)
        claude_session_id, claude_transcript_session_id = get_claude_session_ids(
            raw.get("agent_pid")
        )
        native_name, native_name_source, name_session = get_claude_native_name(
            raw.get("agent_pid")
        )
        if name_session != claude_transcript_session_id:
            # Torn read across a registry rewrite (/clear): never pair a
            # name with a different session's id.
            native_name = native_name_source = None

    # Codex thread association (#206): the hook's identity write is
    # pane-authoritative when present — accept it only for a matching cwd
    # and a UUID-shaped id (the id reaches `codex resume` argv, so an
    # unvalidated leading '-' would parse as a flag). Otherwise fall back
    # to the cwd/mtime rollout heuristic, exactly the pre-hook behavior.
    codex_thread_id = None
    codex_rollout_path = None
    agent_pid = raw.get("agent_pid")
    if agent_type == AgentType.CODEX:
        identity = read_hook_identity(pane_id, agent_pid=agent_pid)
        if (
            identity is not None
            and identity.harness == "codex"
            and identity.cwd == cwd
            and identity.session_id
            and _CODEX_THREAD_ID_RE.fullmatch(identity.session_id)
        ):
            codex_thread_id = identity.session_id
            codex_rollout_path = identity.transcript_path
            if not codex_rollout_path or not os.path.exists(codex_rollout_path):
                # Keep the hook's (authoritative) thread id but recover a
                # usable rollout path from the heuristic — without this,
                # a payload missing transcript_path leaves phase with no
                # transcript axis at all and it silently degrades to the
                # branch rule for the whole session. Only accept a path
                # whose rollout is the SAME thread.
                rollout = get_codex_rollout(cwd)
                codex_rollout_path = (
                    rollout[1] if rollout and rollout[0] == codex_thread_id else None
                )
        else:
            rollout = get_codex_rollout(cwd)
            if rollout:
                codex_thread_id, codex_rollout_path = rollout

    # Detect agent state (session-scoped via current_session_id: claude's
    # transcript sid, codex's thread id)
    state = get_agent_state(
        pane_id,
        agent_type,
        current_session_id=(
            claude_transcript_session_id if agent_type == AgentType.CLAUDE else codex_thread_id
        ),
        agent_pid=agent_pid,
    )

    file_stats = None
    if repo_root and branch:
        file_stats = git_diff_shortstat(repo_root, branch)

    if agent_type == AgentType.CODEX:
        # Bound the hook summary's age for codex: once hooks stop firing
        # (trust revoked, hooks.json edited) an unbounded read would keep
        # returning the last pushed reply forever and suppress the
        # rollout-tail fallback, freezing the card while the rollout grows.
        latest_summary = read_hook_summary(pane_id, max_age=STALE_THRESHOLD_SECONDS)
        if latest_summary is None and codex_thread_id and codex_rollout_path:
            latest_summary = _codex_summary_from_rollout(codex_rollout_path, codex_thread_id)
    else:
        latest_summary = read_hook_summary(pane_id)

    agent = Agent(
        pane_id=pane_id,
        session_name=raw["session_name"],
        window_index=raw["window_index"],
        pane_index=raw["pane_index"],
        agent_type=agent_type,
        cwd=cwd,
        repo_root=repo_root,
        repo_name=repo_name,
        branch=branch,
        worktree_name=worktree_name,
        raw_command=raw["command"],
        agent_pid=raw.get("agent_pid"),
        state=state,
        session_slug=session_slug,
        phase_snapshot=None,
        file_stats=file_stats,
        latest_summary=latest_summary,
        claude_session_id=claude_session_id,
        claude_transcript_session_id=claude_transcript_session_id,
        codex_thread_id=codex_thread_id,
        codex_rollout_path=codex_rollout_path,
        native_name=native_name,
        native_name_source=native_name_source,
    )

    from .status_collector import apply_live_status

    apply_live_status([agent])
    return agent


def _fallback_agent(raw: dict, prior: Agent | None) -> Agent:
    """The record for an agent whose enrichment raised (#358).

    Pane fields come fresh from discovery and the state is UNKNOWN (readers
    overlay live status). ``prior`` — last tick's record for the same process —
    supplies the git context and session identity, so the card keeps its
    repo/branch and its ``stable_key`` (no remove-and-re-add). With no prior
    the git context is simply absent.
    """
    try:
        cwd = get_pane_cwd(raw["pane_id"])
    except Exception:
        cwd = prior.cwd if prior is not None else ""
    carried = {}
    if prior is not None:
        carried = {
            name: getattr(prior, name)
            for name in (
                "repo_root", "repo_name", "branch", "worktree_name", "file_stats",
                "session_slug", "latest_summary", "claude_session_id",
                "claude_transcript_session_id", "codex_thread_id", "codex_rollout_path",
                "native_name", "native_name_source",
            )
        }
    agent = Agent(
        pane_id=raw["pane_id"],
        session_name=raw["session_name"],
        window_index=raw["window_index"],
        pane_index=raw["pane_index"],
        agent_type=AgentType.from_command(raw["command"]),
        cwd=cwd or (prior.cwd if prior is not None else ""),
        raw_command=raw["command"],
        agent_pid=raw.get("agent_pid"),
        state=AgentState.UNKNOWN,
        phase_snapshot=None,
        **carried,
    )
    try:
        from .status_collector import apply_live_status

        apply_live_status([agent])
    except Exception:
        pass
    return agent


def _same_process_prior(raw: dict, previous: Mapping[str, Agent] | None) -> Agent | None:
    """Last tick's record for this pane, only when it is the same process.

    Same pane and the same, known agent pid: a different (or unknown) process
    in the pane must never lend its git context or session identity.
    """
    prior = (previous or {}).get(raw.get("pane_id"))
    pid = raw.get("agent_pid")
    if prior is None or pid is None or prior.agent_pid != pid:
        return None
    return prior


def _keep_exiting_claude_ids(agent: Agent, prior: Agent | None) -> None:
    """Keep a shutting-down Claude session's ids for its last ticks (#433).

    Claude Code deletes ``~/.claude/sessions/<pid>.json`` as soon as it starts
    shutting down, about a second before the process exits (measured
    2026-11-10: gone 0.17 s after ``/exit``, process alive to 1.14 s). A tick
    in that window reads no ids for a live pane, and the snapshot store then
    recorded ``session_id: null`` — on a reboot, the very record ``recover``
    picks as its baseline (#410). The same process's previous record still
    knows them. Only an ABSENT registry file means "exiting": a file that is
    there but unreadable (a torn read while ``/clear`` rewrites it in place,
    malformed JSON) keeps the null, so an old session never outlives a
    rewrite. A registry that does answer always wins. Startup has nothing to
    carry: until the registry is written (~0.5 s) no tick has seen an id.
    Live status was overlaid while the id was None, so it is re-applied
    against the carried identity.
    """
    if (
        prior is None
        or agent.agent_type is not AgentType.CLAUDE
        or prior.agent_type is not AgentType.CLAUDE
        or agent.claude_transcript_session_id is not None
        or agent.claude_session_id is not None
        or prior.claude_transcript_session_id is None
        or agent.agent_pid is None
        or not CLAUDE_SESSIONS_DIR.is_dir()  # registry outage, not an exit
        or (CLAUDE_SESSIONS_DIR / f"{agent.agent_pid}.json").exists()
    ):
        return
    agent.claude_transcript_session_id = prior.claude_transcript_session_id
    agent.claude_session_id = prior.claude_session_id
    try:
        from .status_collector import apply_live_status

        apply_live_status([agent])
    except Exception:
        pass  # outside enrich_all_agents' per-agent guard: never abort the tick


def enrich_all_agents(
    raw_agents: list[dict], *, previous: Mapping[str, Agent] | None = None
) -> list[Agent]:
    """Enrich all raw agent data with cwd and git context.

    One agent's failure never costs the others (#358): it is reported and
    replaced by ``_fallback_agent``. ``previous`` (pane id → last tick's Agent)
    lends that record its git context when it is the same process — same pane
    and the same, known agent pid.
    """
    agents: list[Agent] = []
    for raw in raw_agents:
        try:
            agent = enrich_agent(raw)
        except Exception as exc:
            prior = _same_process_prior(raw, previous)
            repo = prior.repo_root if prior is not None else None
            if repo is None:
                try:
                    repo = get_pane_cwd(raw["pane_id"]) or None
                except Exception:
                    repo = None
            _report_enrichment_failure(
                "agent enrichment failed",
                exc,
                repo=repo,
                branch=prior.branch if prior is not None else None,
            )
            try:
                agents.append(_fallback_agent(raw, prior))
            except Exception:
                pass  # malformed discovery row: nothing safe to show
            continue
        _keep_exiting_claude_ids(agent, _same_process_prior(raw, previous))
        agents.append(agent)
    pids = {raw.get("pane_id"): raw.get("agent_pid") for raw in raw_agents}
    _clear_duplicate_codex_thread_ids(agents, pids)
    return agents
