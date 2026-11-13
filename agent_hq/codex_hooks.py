"""Install the agent-hq hook into Codex's ``~/.codex/hooks.json`` (#206).

Schema verified live on codex-cli 0.144.5 (Claude-compatible):
``{"hooks": {"<Event>": [{"matcher": "", "hooks": [{"type": "command",
"command": "agent-hq-hook --harness codex"}]}]}}`` — command strings
carry argv, so the harness flag rides in the command itself.

Codex trusts hooks by content hash: after ANY change to this file the
user must re-trust via ``/hooks`` inside Codex, or the hooks silently
stop running (Stage 0 V9). Until trusted, Agent HQ degrades to exactly
the pre-#206 behavior for codex panes (regex state, heuristic thread
capture, branch-rule phase) — the hook IS the feature gate.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path

CODEX_HOOKS_FILE = Path.home() / ".codex" / "hooks.json"
CODEX_HOOK_COMMAND = "agent-hq-hook --harness codex"
# Full Claude-parity state set: Pre/PostToolUse keep RUNNING fresh during
# long turns (the 5-min staleness cap would otherwise drop an active agent
# back to regex detection mid-turn). No SessionEnd — Codex never fires one.
CODEX_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
)

TRUST_INSTRUCTIONS = """\
Codex trusts hooks by content hash. To activate (or re-activate) them:
  1. Open codex in any terminal
  2. Run /hooks
  3. Approve/trust the agent-hq-hook entries
Until trusted, the hooks silently do not run and Agent HQ falls back to
regex state detection for codex panes."""


def _hook_commands(entry: object) -> list[str]:
    if not isinstance(entry, dict):
        return []
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return []
    return [str(h.get("command", "")) for h in hooks if isinstance(h, dict)]


def _command_argv(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _is_agent_hq_command(command: str) -> bool:
    """True only when the command's executable IS ``agent-hq-hook``.

    Anchored on the argv[0] basename, not a substring: a foreign entry
    like ``/Users/me/bin/my-agent-hq-hook-wrapper.sh --notify`` merely
    *contains* the substring, and classifying it as ours would silently
    delete a user's own hook during merge (controller review, verified by
    execution). Wrappers we can't positively identify are treated as
    foreign — the safe direction is to preserve them.
    """
    argv = _command_argv(command)
    return bool(argv) and Path(argv[0]).name == "agent-hq-hook"


def _selects_codex_harness(command: str) -> bool:
    """An agent-hq-hook invocation that actually selects the codex flavor.

    A bare ``agent-hq-hook`` here would run the *claude* path — no identity
    write on SessionStart, and Stop parsing the rollout as a claude
    transcript — so matching the executable alone is not enough. The flag
    check walks argv token pairs so ``--harness codexfoo`` can't match.
    """
    if not _is_agent_hq_command(command):
        return False
    argv = _command_argv(command)
    return any(a == "--harness" and b == "codex" for a, b in zip(argv, argv[1:]))


def _entry_is_codex_agent_hq(entry: object) -> bool:
    return any(_selects_codex_harness(c) for c in _hook_commands(entry))


def _strip_wrong_flavor(entry: object) -> object | None:
    """Drop wrong-flavor agent-hq commands from ``entry``, keeping foreign
    ones. Returns ``None`` when nothing is left.

    Filtering per COMMAND rather than per entry matters: an entry holding
    both a foreign command and a bare ``agent-hq-hook`` would otherwise be
    kept whole, leaving both flavors registered so they race to write the
    same pane state file with incompatible schemas.
    """
    if not isinstance(entry, dict):
        return entry
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return entry
    kept = [
        h
        for h in hooks
        if not (
            isinstance(h, dict)
            and _is_agent_hq_command(str(h.get("command", "")))
            and not _selects_codex_harness(str(h.get("command", "")))
        )
    ]
    if not kept:
        return None
    if len(kept) == len(hooks):
        return entry
    return {**entry, "hooks": kept}


def merge_codex_hooks(existing: dict | None) -> dict:
    """Return ``existing`` with the codex-flavored agent-hq entries ensured.

    Idempotent; preserves foreign entries and unknown top-level keys; never
    mutates its input. An existing agent-hq entry that does NOT select the
    codex harness is replaced rather than counted as installed — leaving it
    would silently run the claude path for every codex session.
    """
    data = dict(existing) if isinstance(existing, dict) else {}
    hooks = data.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}
    for event in CODEX_HOOK_EVENTS:
        entries = hooks.get(event)
        entries = list(entries) if isinstance(entries, list) else []
        entries = [stripped for e in entries if (stripped := _strip_wrong_flavor(e)) is not None]
        if not any(_entry_is_codex_agent_hq(e) for e in entries):
            entries.append(
                {"matcher": "", "hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND}]}
            )
        hooks[event] = entries
    data["hooks"] = hooks
    return data


def install_codex_hooks(hooks_file: Path | None = None, *, print_only: bool = False) -> int:
    """Merge the agent-hq entries into ``hooks_file`` (default: the real
    ``~/.codex/hooks.json``), backing up any existing file first and
    printing the trust-by-hash instructions. ``print_only`` renders the
    merged JSON without touching disk."""
    target = hooks_file if hooks_file is not None else CODEX_HOOKS_FILE
    existing: dict | None = None
    try:
        loaded = json.loads(target.read_text())
        if isinstance(loaded, dict):
            existing = loaded
        else:
            print(
                f"Error: {target} is not valid JSON (expected an object). "
                "Fix or remove it, then re-run — refusing to overwrite it, "
                "because merging from scratch would delete any hooks it defines.",
                file=sys.stderr,
            )
            return 1
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        # Never "merge" an unreadable file into an agent-hq-only config:
        # that silently deletes every foreign hook the user configured,
        # and they'd re-trust it via /hooks without noticing.
        print(
            f"Error: could not read {target} as valid JSON ({exc}). "
            "Fix or remove it, then re-run — refusing to overwrite it, "
            "because merging from scratch would delete any hooks it defines.",
            file=sys.stderr,
        )
        return 1

    merged = merge_codex_hooks(existing)
    rendered = json.dumps(merged, indent=2) + "\n"

    if print_only:
        print(rendered, end="")
        return 0

    if existing is not None and merged == existing:
        # No-op: don't rewrite (a byte-identical rewrite is harmless, but a
        # formatting-only one would change the content hash and silently
        # un-trust already-trusted hooks) and don't pile up backups.
        print(f"{target} already up to date; nothing written.")
        print(TRUST_INSTRUCTIONS)
        return 0

    target_mode: int | None = None
    if target.exists():
        try:
            target_mode = target.stat().st_mode & 0o777
        except OSError:
            pass
        # Never clobber an existing backup: two installs inside one second
        # would otherwise share a name, and the second would replace the
        # only copy of the user's original config with the merged file.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.bak-{stamp}")
        suffix = 1
        while backup.exists():
            backup = target.with_name(f"{target.name}.bak-{stamp}-{suffix}")
            suffix += 1
        backup.write_bytes(target.read_bytes())
        if target_mode is not None:
            # A 0600 hooks.json must not leak to umask-default 0644 via
            # its backup or its replacement.
            os.chmod(backup, target_mode)
        print(f"Backed up existing {target.name} to {backup}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(rendered)
    if target_mode is not None:
        os.chmod(tmp, target_mode)
    tmp.replace(target)
    print(f"Wrote {target}")
    print(TRUST_INSTRUCTIONS)
    return 0
