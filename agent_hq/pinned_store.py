"""Disk-backed store of pinned agent identities. #141

A single JSON sidecar under ``~/.config/agent-hq/`` that the terminal TUI and the
launchd web service both read and write — the shared file IS the cross-surface
sync mechanism, exactly like ``ui-prefs.json``. Each mutating call takes
a flock (via ``agent_hq._locking.file_lock``) around a read-modify-write so the
two processes can't clobber each other, then writes atomically (temp file +
rename) so a concurrent reader never sees a half-written document. Reads are plain
and tolerate a missing or corrupt file by returning empty.

The stored keys are ``Agent.stable_key`` values (transcript session id, else
``tool:cwd:pane_id``) — the same identity used as the web card's
``data-card-key`` — so a pin survives a page refresh, a TUI restart, and a tmux
pane renumber for Claude agents (#132).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_hq._locking import file_lock

log = logging.getLogger(__name__)

_DEFAULT_PINNED_FILE = Path.home() / ".config" / "agent-hq" / "pinned-sessions.json"


def _atomic_write_json(path: Path, payload) -> None:
    """Write ``payload`` as JSON via a sibling temp file + atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


class PinnedSessionsStore:
    """Disk-backed set of ``Agent.stable_key`` values pinned to the top."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_PINNED_FILE
        self._lock = self._path.with_suffix(self._path.suffix + ".lock")

    def _read(self) -> set[str]:
        try:
            data = json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return set()
        if not isinstance(data, list):
            return set()
        return {k for k in data if isinstance(k, str)}

    def keys(self) -> set[str]:
        """All pinned keys. Fast disk read tolerating missing/corrupt files."""
        return self._read()

    def is_pinned(self, key: str) -> bool:
        return key in self._read()

    def _read_or_raise(self) -> set[str]:
        """Like ``_read`` but raises ``OSError`` instead of degrading to an
        empty set when the file exists yet can't be read/parsed. Only a
        genuinely missing file is legitimately empty. Used by ``add``/
        ``remove`` so a transient EIO/permissions blip or corrupt file can't
        clobber the existing pins (AHQ-014)."""
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return set()
        except OSError as exc:
            raise OSError(f"cannot read {self._path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OSError(f"{self._path} corrupt: {exc}") from exc
        if not isinstance(data, list):
            return set()
        return {k for k in data if isinstance(k, str)}

    def add(self, key: str) -> None:
        """Add ``key`` to the pinned set (flock-guarded, idempotent)."""
        with file_lock(self._lock):
            try:
                keys = self._read_or_raise()
            except OSError as exc:
                log.error("cannot read %s, skipping pin write: %s", self._path, exc)
                return
            if key in keys:
                return
            keys.add(key)
            _atomic_write_json(self._path, sorted(keys))

    def remove(self, key: str) -> bool:
        """Drop ``key`` from the pinned set — unpin the agent.

        Returns True if the key was present. The unpinned agent falls back to
        its natural position in the active sort order on the next render.
        """
        with file_lock(self._lock):
            try:
                keys = self._read_or_raise()
            except OSError as exc:
                log.error("cannot read %s, skipping pin write: %s", self._path, exc)
                return False
            if key not in keys:
                return False
            keys.discard(key)
            _atomic_write_json(self._path, sorted(keys))
            return True

    def replace(self, old_key: str, new_key: str) -> bool:
        """Atomically swap ``old_key`` for ``new_key`` (flock-guarded).

        No-op (returns False) when ``old_key`` is absent. When ``new_key``
        is already present the swap just drops ``old_key``. Used by
        ``migrate_legacy_pins`` to upgrade a legacy-form pin in one write.
        """
        with file_lock(self._lock):
            try:
                keys = self._read_or_raise()
            except OSError as exc:
                log.error("cannot read %s, skipping pin write: %s", self._path, exc)
                return False
            if old_key not in keys:
                return False
            keys.discard(old_key)
            keys.add(new_key)
            _atomic_write_json(self._path, sorted(keys))
            return True


def migrate_legacy_pins(store: PinnedSessionsStore, agents) -> None:
    """Rewrite pins held under an agent's legacy key to its stable key.

    #205 Codex review: the ``tool:cwd:pane_id`` legacy form is ambiguous —
    after the pinned session exits, an unrelated future session in the same
    pane and cwd collides with it and renders pinned. So the first time a
    live agent with a captured stable id (claude transcript sid / codex
    thread UUID) matches a legacy-form pin, the pin is rewritten under the
    stable key and the legacy entry dropped. This also carries a
    pre-capture codex pin (necessarily written in legacy form) across a
    later pane renumber: once the UUID is captured while the agent is still
    live, the pin is re-keyed on it.

    Cheap no-op when no legacy-form pin matches a live agent. Store-level
    I/O errors are swallowed by ``replace`` so callers' poll loops never
    wedge on this.
    """
    keys = store.keys()
    if not keys:
        return
    for agent in agents:
        stable, legacy = agent.stable_key, agent.legacy_stable_key
        if stable != legacy and legacy in keys:
            store.replace(legacy, stable)
            keys.discard(legacy)
            keys.add(stable)
