"""Disk-backed store of UI preferences. #176

A single JSON sidecar under ``~/.config/agent-hq/`` that the terminal TUI and
the launchd web service both read and write — the shared file IS the
cross-surface sync mechanism, exactly like ``pinned-sessions.json``. Each
mutating call takes a flock (via ``agent_hq._locking.file_lock``) around a
read-modify-write so the two processes can't clobber each other, then writes
atomically (temp file + rename) so a concurrent reader never sees a
half-written document. Reads are plain and tolerate a missing or corrupt file
by returning nothing; unknown keys are preserved on write so older builds
don't strip prefs added by newer ones.

Value spaces are validated per key — ``tui_sort_mode``, ``web_agents_view``
and ``web_theme`` are different enums stored side by side. Getters return
``None`` for a missing or invalid value; callers apply their own default.
``web_theme`` is written by ``POST /prefs/theme``
(``agent_hq/web/routes/prefs.py``) and read by ``base.html`` on every full
page render.

This module must stay importable by the web process — no textual imports.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_hq._locking import file_lock

log = logging.getLogger(__name__)

_DEFAULT_PREFS_FILE = Path.home() / ".config" / "agent-hq" / "ui-prefs.json"

TUI_SORT_MODES = ("priority", "phase", "repo")
WEB_AGENTS_VIEWS = ("phase", "list", "kanban")
# "default" is stored literally (rather than deleting the key) so a later
# build adding themes can distinguish "user chose the stock look" from
# "never touched the picker".
# This is the VALIDATION roster — every theme that may be persisted or
# deep-linked via ?theme=. The ⚙ picker shows only the THEME_PICKER_IDS
# subset defined in templates/partials/_theme_registry.html (parity with
# this tuple pinned by tests/test_web_theme.py::TestExpressiveThemes).
# midnight, warm, and brutalist are benched post-#220-bake-off: hidden
# from the picker but kept valid here so old prefs and direct links still
# work, and re-listing them in THEME_PICKER_IDS brings them back.
# Bake-off outcome (#220): the five Fable themes stayed; Codex's #221
# paper/terminal duplicates were dropped, and its bubblegum was adopted
# and reworked into a pink light theme.
WEB_THEMES = (
    "default",
    "midnight",
    "warm",
    "paper",
    "terminal",
    "neon",
    "brutalist",
    "console",
    "bubblegum",
)
# /issues hub segmented modes (#247). Queue is the default landing.
# Hygiene added in Phase 4 (#249)
WEB_ISSUES_MODES = ("queue", "triage", "browse", "hygiene")
# Sticky Live|History preview mode (#171 follow-up). One key per surface —
# same split as sort mode vs agents view — so toggling History on the phone
# doesn't flip the terminal dashboard.
PREVIEW_MODES = ("live", "history")


def _atomic_write_json(path: Path, payload) -> None:
    """Write ``payload`` as JSON via a sibling temp file + atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


class UiPrefsStore:
    """Disk-backed key/value store of validated UI preferences."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_PREFS_FILE
        self._lock = self._path.with_suffix(self._path.suffix + ".lock")

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _get(self, key: str, valid: tuple[str, ...]) -> str | None:
        value = self._read().get(key)
        return value if value in valid else None

    def _read_for_write(self) -> dict:
        """Like ``_read`` but raises ``OSError`` when the file exists yet
        can't be read, instead of degrading to ``{}``.

        ``_set`` is a read-modify-write with no early return, so a read that
        silently yields ``{}`` writes back a document containing only the key
        being set — dropping every other pref. An unreadable file (EIO,
        permissions) most likely still holds good values, so the caller skips
        the write rather than destroying them (the AHQ-014 class fixed for the
        other stores in #192).

        A *corrupt* file is deliberately still treated as empty: there is
        nothing preservable in unparseable JSON, and refusing to write would
        strand the user with a broken file and no in-app way to repair it. So
        the write proceeds and recovers it.
        """
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise OSError(f"cannot read {self._path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _set(self, key: str, value: str, valid: tuple[str, ...]) -> None:
        if value not in valid:
            raise ValueError(f"invalid {key}: {value!r} (expected one of {valid})")
        with file_lock(self._lock):
            try:
                prefs = self._read_for_write()
            except OSError as exc:
                log.error("cannot read %s, skipping %s write: %s", self._path, key, exc)
                return
            prefs[key] = value
            _atomic_write_json(self._path, prefs)

    def get_tui_sort_mode(self) -> str | None:
        return self._get("tui_sort_mode", TUI_SORT_MODES)

    def set_tui_sort_mode(self, mode: str) -> None:
        self._set("tui_sort_mode", mode, TUI_SORT_MODES)

    def get_web_agents_view(self) -> str | None:
        return self._get("web_agents_view", WEB_AGENTS_VIEWS)

    def set_web_agents_view(self, view: str) -> None:
        self._set("web_agents_view", view, WEB_AGENTS_VIEWS)

    def get_web_issues_mode(self) -> str | None:
        return self._get("web_issues_mode", WEB_ISSUES_MODES)

    def set_web_issues_mode(self, mode: str) -> None:
        self._set("web_issues_mode", mode, WEB_ISSUES_MODES)

    def get_web_theme(self) -> str | None:
        return self._get("web_theme", WEB_THEMES)

    def set_web_theme(self, theme: str) -> None:
        self._set("web_theme", theme, WEB_THEMES)

    def get_tui_preview_mode(self) -> str | None:
        return self._get("tui_preview_mode", PREVIEW_MODES)

    def set_tui_preview_mode(self, mode: str) -> None:
        self._set("tui_preview_mode", mode, PREVIEW_MODES)

    def get_web_preview_mode(self) -> str | None:
        return self._get("web_preview_mode", PREVIEW_MODES)

    def set_web_preview_mode(self, mode: str) -> None:
        self._set("web_preview_mode", mode, PREVIEW_MODES)
