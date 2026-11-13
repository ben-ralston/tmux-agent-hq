"""Debounced git sync of the triage data repo (#246).

The daemon poll loop calls tick() every iteration (~2 s). Internally we only
run git every check_interval_s, and only commit when the working tree is
dirty AND quiescent (newest data-file mtime older than sync_debounce_s) so a
burst of triage writes lands as one commit. Push failures keep the commit —
sync is eventually consistent with origin. Nothing here may raise: the
daemon loop's health is worth more than a missed sync (it retries next tick).

Design: the issue-prioritization redesign spec (not part of this repository)
(Section 1 "Store"; auth per the ccc snapshot-lane pattern — the clone's
origin must already be pushable non-interactively).
"""

from __future__ import annotations

import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_hq import eventlog
from agent_hq.config import TriageConfig

log = logging.getLogger(__name__)

# Self-seeded into the data repo so store sidecars (flock files, atomic-write
# temps) are never committed — a fresh clone won't have a .gitignore otherwise.
_SIDECAR_PATTERNS = ("*.lock", "*.tmp")
_GITIGNORE_BODY = "# Runtime sidecars from agent_hq stores — never data\n" + "".join(
    f"{p}\n" for p in _SIDECAR_PATTERNS
)


class TriageSyncer:
    """Commit + push the triage data repo when it is dirty and quiescent."""

    def __init__(self, config: TriageConfig, *, check_interval_s: float = 15.0) -> None:
        self._config = config
        self._check_interval_s = check_interval_s
        self._last_check = 0.0

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self._config.data_dir), *args],
            capture_output=True,
            text=True,
            # Short timeout: these calls run synchronously on the daemon's
            # 2 s poll loop, so a hung origin must be bounded. Local ops
            # finish in ms; a push of small JSON diffs in a couple seconds.
            # (Thread offload is the follow-up if this ever bites.)
            timeout=15,
        )

    def _newest_dirty_mtime(self, porcelain: str) -> float:
        """Newest mtime among the paths ``git status --porcelain`` reports.

        Only dirty paths matter for quiescence — a committed file's fresh
        mtime must not hold the debounce open forever.
        """
        newest = 0.0
        for line in porcelain.splitlines():
            if len(line) < 4:
                continue
            rel = line[3:]
            if " -> " in rel:  # rename: "R  old -> new"
                rel = rel.split(" -> ", 1)[1]
            rel = rel.strip('"')
            if rel == ".gitignore":
                # Self-seeded infrastructure, not data — must not re-arm
                # the quiescence debounce on the tick that creates it.
                continue
            try:
                newest = max(newest, (Path(self._config.data_dir) / rel).stat().st_mtime)
            except OSError:
                pass
        return newest

    def tick(self, now: float | None = None) -> bool:
        """Maybe commit+push. Returns True when a commit was made. Never raises."""
        if not self._config.sync_enabled:
            return False
        now = now if now is not None else time.time()
        if now - self._last_check < self._check_interval_s:
            return False
        self._last_check = now
        try:
            return self._sync(now)
        except Exception as exc:  # daemon-loop constraint: swallow everything
            log.warning("triage sync failed: %s", exc)
            eventlog.emit("warn", "triage sync failed", {"error": repr(exc)}, min_interval_s=900)
            return False

    def _ensure_gitignore(self) -> bool:
        """Seed or extend the sidecar .gitignore. Returns True when it wrote.

        A hand-made data repo may already carry its own .gitignore — preserve
        it, but the sidecar patterns must be present or `git add -A` commits
        every flock file the store creates.
        """
        gi = Path(self._config.data_dir) / ".gitignore"
        if not gi.exists():
            gi.write_text(_GITIGNORE_BODY)
            return True
        existing = gi.read_text()
        lines = {line.strip() for line in existing.splitlines()}
        missing = [p for p in _SIDECAR_PATTERNS if p not in lines]
        if not missing:
            return False
        gi.write_text(existing.rstrip("\n") + "\n" + "".join(f"{p}\n" for p in missing))
        return True

    def _sync(self, now: float) -> bool:
        status = self._git("status", "--porcelain")
        if status.returncode != 0:
            # Keep this the first touch of data_dir: a missing clone or a
            # non-git directory must surface this diagnostic, not get files
            # written into it.
            log.warning("triage sync: not a usable git repo: %s", status.stderr.strip())
            return False
        if self._ensure_gitignore():
            status = self._git("status", "--porcelain")
        if not status.stdout.strip():
            self._push_if_ahead()
            return False
        if now - self._newest_dirty_mtime(status.stdout) < self._config.sync_debounce_s:
            return False  # still being written; wait for quiescence
        self._git("add", "-A")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commit = self._git("commit", "-m", f"triage sync {stamp}")
        if commit.returncode != 0:
            log.warning("triage sync commit failed: %s", commit.stderr.strip())
            return False
        self._push_if_ahead()
        return True

    def _push_if_ahead(self) -> None:
        # Local ahead-check first: a configured-but-idle repo must not fire a
        # network push every check interval (and a hung origin must not park
        # the daemon loop when there is nothing to push).
        ahead = self._git("rev-list", "--count", "@{u}..HEAD")
        if ahead.returncode != 0:
            log.warning("triage sync: cannot determine upstream: %s", ahead.stderr.strip())
            return
        if ahead.stdout.strip() in ("", "0"):
            return
        push = self._git("push")
        if push.returncode != 0:
            # Degraded, not broke (the commit stands and push retries next
            # tick) — "warn" per repo eventlog level semantics so the
            # sentinel doesn't auto-file issues for an offline window.
            log.warning("triage data push failed: %s", push.stderr.strip())
            eventlog.emit(
                "warn",
                "triage data push failed",
                {"stderr": push.stderr.strip()[:500]},
                min_interval_s=900,
            )
