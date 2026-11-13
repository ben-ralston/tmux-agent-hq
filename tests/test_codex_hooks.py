"""Tests for the ~/.codex/hooks.json installer (#206)."""

import json

from agent_hq.codex_hooks import (
    CODEX_HOOK_COMMAND,
    CODEX_HOOK_EVENTS,
    install_codex_hooks,
    merge_codex_hooks,
)

EXPECTED_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
}


class TestMerge:
    def test_event_set(self):
        assert set(CODEX_HOOK_EVENTS) == EXPECTED_EVENTS

    def test_fresh(self):
        merged = merge_codex_hooks(None)
        for event in CODEX_HOOK_EVENTS:
            entries = merged["hooks"][event]
            assert entries[-1]["hooks"][0] == {"type": "command", "command": CODEX_HOOK_COMMAND}
            assert entries[-1]["matcher"] == ""

    def test_command_carries_harness_flag(self):
        assert CODEX_HOOK_COMMAND == "agent-hq-hook --harness codex"

    def test_preserves_foreign_entries(self):
        existing = {
            "hooks": {
                "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "other-tool"}]}]
            },
            "topLevelExtra": 1,
        }
        merged = merge_codex_hooks(existing)
        assert merged["topLevelExtra"] == 1
        cmds = [h["command"] for e in merged["hooks"]["Stop"] for h in e["hooks"]]
        assert "other-tool" in cmds
        assert any("agent-hq-hook" in c for c in cmds)

    def test_idempotent(self):
        once = merge_codex_hooks(None)
        assert merge_codex_hooks(once) == once

    def test_does_not_mutate_input(self):
        existing = {"hooks": {"Stop": []}}
        merge_codex_hooks(existing)
        assert existing == {"hooks": {"Stop": []}}


class TestInstall:
    def test_fresh_install_writes_file(self, tmp_path, capsys):
        target = tmp_path / "hooks.json"
        rc = install_codex_hooks(target)
        assert rc == 0
        data = json.loads(target.read_text())
        assert set(data["hooks"]) == EXPECTED_EVENTS
        out = capsys.readouterr().out
        assert "/hooks" in out  # trust instructions printed

    def test_existing_file_backed_up(self, tmp_path, capsys):
        target = tmp_path / "hooks.json"
        target.write_text(json.dumps({"hooks": {"Stop": []}, "keep": True}))
        rc = install_codex_hooks(target)
        assert rc == 0
        backups = list(tmp_path.glob("hooks.json.bak-*"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text()) == {"hooks": {"Stop": []}, "keep": True}
        assert json.loads(target.read_text())["keep"] is True

    def test_idempotent_reinstall(self, tmp_path, capsys):
        target = tmp_path / "hooks.json"
        install_codex_hooks(target)
        first = target.read_text()
        install_codex_hooks(target)
        assert target.read_text() == first

    def test_print_only_touches_nothing(self, tmp_path, capsys):
        target = tmp_path / "hooks.json"
        rc = install_codex_hooks(target, print_only=True)
        assert rc == 0
        assert not target.exists()
        printed = json.loads(capsys.readouterr().out)
        assert set(printed["hooks"]) == EXPECTED_EVENTS

    def test_malformed_existing_is_never_replaced(self, tmp_path, capsys):
        # Superseded by TestMalformedAbortsInsteadOfDestroying: overwriting
        # an unreadable hooks.json would silently delete the user's own
        # codex hooks, so the installer now refuses.
        target = tmp_path / "hooks.json"
        target.write_text("{not json")
        assert install_codex_hooks(target) != 0
        assert target.read_text() == "{not json"

    def test_creates_parent_dir(self, tmp_path):
        target = tmp_path / "nested" / "hooks.json"
        assert install_codex_hooks(target) == 0
        assert target.exists()


class TestMergeCodexFlavorRequired:
    """Codex review P2: a plain (claude-flavor) agent-hq-hook entry must
    not satisfy the installed check — it runs the claude path (no identity
    write, wrong Stop parsing). The installer replaces it."""

    def test_plain_agent_hq_entry_replaced(self):
        existing = {
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [{"type": "command", "command": "agent-hq-hook"}]}
                ]
            }
        }
        merged = merge_codex_hooks(existing)
        cmds = [h["command"] for e in merged["hooks"]["Stop"] for h in e["hooks"]]
        assert cmds.count(CODEX_HOOK_COMMAND) == 1
        assert "agent-hq-hook" not in [c for c in cmds if c != CODEX_HOOK_COMMAND]

    def test_foreign_command_untouched_next_to_replacement(self):
        existing = {
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [{"type": "command", "command": "other-tool"}]},
                    {"matcher": "", "hooks": [{"type": "command", "command": "agent-hq-hook"}]},
                ]
            }
        }
        merged = merge_codex_hooks(existing)
        cmds = [h["command"] for e in merged["hooks"]["Stop"] for h in e["hooks"]]
        assert "other-tool" in cmds
        assert cmds.count(CODEX_HOOK_COMMAND) == 1
        assert "agent-hq-hook" not in [
            c for c in cmds if c not in (CODEX_HOOK_COMMAND, "other-tool")
        ]

    def test_codex_flavored_entry_still_idempotent(self):
        once = merge_codex_hooks(None)
        assert merge_codex_hooks(once) == once


class TestMalformedAbortsInsteadOfDestroying:
    """Workflow review [CONFIRMED]: a malformed hooks.json must not be
    replaced with an agent-hq-only config — that silently deletes every
    foreign hook the user configured."""

    def test_malformed_aborts_nonzero_and_leaves_file(self, tmp_path, capsys):
        target = tmp_path / "hooks.json"
        target.write_text("{not json")
        rc = install_codex_hooks(target)
        assert rc != 0
        assert target.read_text() == "{not json"  # untouched
        captured = capsys.readouterr()
        assert "refusing to overwrite" in (captured.out + captured.err)

    def test_non_object_json_aborts(self, tmp_path, capsys):
        target = tmp_path / "hooks.json"
        target.write_text('["a list"]')
        assert install_codex_hooks(target) != 0
        assert target.read_text() == '["a list"]'


class TestMixedEntryFlavorFilter:
    """Codex review P2: a matcher entry holding BOTH a foreign command and
    a bare agent-hq-hook must keep the foreign one and drop only the
    wrong-flavor agent-hq command — otherwise both flavors run and race to
    write the same pane state with incompatible schemas."""

    def test_mixed_entry_drops_only_wrong_flavor_command(self):
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": "other-tool"},
                            {"type": "command", "command": "agent-hq-hook"},
                        ],
                    }
                ]
            }
        }
        merged = merge_codex_hooks(existing)
        cmds = [h["command"] for e in merged["hooks"]["Stop"] for h in e["hooks"]]
        assert "other-tool" in cmds
        assert "agent-hq-hook" not in cmds
        assert cmds.count(CODEX_HOOK_COMMAND) == 1

    def test_mixed_entry_with_codex_flavor_is_left_alone(self):
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": "other-tool"},
                            {"type": "command", "command": CODEX_HOOK_COMMAND},
                        ],
                    }
                ]
            }
        }
        assert merge_codex_hooks(existing) == merge_codex_hooks(merge_codex_hooks(existing))
        merged = merge_codex_hooks(existing)
        cmds = [h["command"] for e in merged["hooks"]["Stop"] for h in e["hooks"]]
        assert cmds.count(CODEX_HOOK_COMMAND) == 1
        assert "other-tool" in cmds


class TestBackupNeverOverwritten:
    """Two content-changing installs inside one second must not collide on
    the timestamped backup name — the second would otherwise overwrite the
    only copy of the user's original config with the already-merged file."""

    def test_second_install_within_same_second_keeps_both(self, tmp_path):
        target = tmp_path / "hooks.json"
        original = json.dumps({"hooks": {"Stop": []}, "mine": True})
        target.write_text(original)
        install_codex_hooks(target)
        # Change the file between installs so the second is not a no-op
        # (an unchanged file no longer writes at all — see TestNoOpInstall).
        data = json.loads(target.read_text())
        data["hooks"]["Stop"].append(
            {"matcher": "", "hooks": [{"type": "command", "command": "agent-hq-hook"}]}
        )
        target.write_text(json.dumps(data))
        install_codex_hooks(target)
        backups = sorted(tmp_path.glob("hooks.json.bak-*"))
        assert len(backups) == 2
        # The original config survives in exactly one of them.
        assert any(b.read_text() == original for b in backups)


class TestForeignSubstringCommandPreserved:
    """Controller review (Medium, verified by execution): a foreign command
    merely CONTAINING the substring ``agent-hq-hook`` — a user's wrapper
    script — was silently deleted by the wrong-flavor filter. The match
    must anchor on the executable basename being exactly ``agent-hq-hook``."""

    WRAPPER = "/Users/me/bin/my-agent-hq-hook-wrapper.sh --notify"

    def test_wrapper_script_survives_merge(self):
        existing = {
            "hooks": {
                "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": self.WRAPPER}]}]
            }
        }
        merged = merge_codex_hooks(existing)
        cmds = [h["command"] for e in merged["hooks"]["Stop"] for h in e["hooks"]]
        assert self.WRAPPER in cmds
        assert cmds.count(CODEX_HOOK_COMMAND) == 1

    def test_wrapper_next_to_bare_agent_hq_in_one_entry(self):
        # The per-command filter must drop ONLY the bare agent-hq-hook.
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": self.WRAPPER},
                            {"type": "command", "command": "agent-hq-hook"},
                        ],
                    }
                ]
            }
        }
        merged = merge_codex_hooks(existing)
        cmds = [h["command"] for e in merged["hooks"]["Stop"] for h in e["hooks"]]
        assert self.WRAPPER in cmds
        assert "agent-hq-hook" not in cmds
        assert cmds.count(CODEX_HOOK_COMMAND) == 1

    def test_absolute_path_agent_hq_hook_is_still_ours(self):
        # Anchoring on basename must not stop matching a path-qualified
        # install of our own binary.
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/Users/me/.local/bin/agent-hq-hook --harness codex",
                            }
                        ],
                    }
                ]
            }
        }
        merged = merge_codex_hooks(existing)
        cmds = [h["command"] for e in merged["hooks"]["Stop"] for h in e["hooks"]]
        # Recognized as already-installed codex flavor: no duplicate appended.
        assert cmds.count("/Users/me/.local/bin/agent-hq-hook --harness codex") == 1
        assert CODEX_HOOK_COMMAND not in cmds

    def test_harness_flag_needs_exact_token(self):
        # "--harness codexfoo" must not read as the codex flavor.
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": "agent-hq-hook --harness codexfoo"}
                        ],
                    }
                ]
            }
        }
        merged = merge_codex_hooks(existing)
        cmds = [h["command"] for e in merged["hooks"]["Stop"] for h in e["hooks"]]
        assert "agent-hq-hook --harness codexfoo" not in cmds  # wrong flavor, replaced
        assert cmds.count(CODEX_HOOK_COMMAND) == 1


class TestNoOpInstall:
    """Re-running install over an already-correct file must not rewrite it
    (a formatting-only rewrite would change the trust hash and silently
    un-trust the hooks) and must not pile up backups."""

    def test_reinstall_writes_nothing_and_no_backup(self, tmp_path, capsys):
        target = tmp_path / "hooks.json"
        install_codex_hooks(target)
        before_mtime = target.stat().st_mtime_ns
        capsys.readouterr()
        rc = install_codex_hooks(target)
        assert rc == 0
        assert target.stat().st_mtime_ns == before_mtime
        assert list(tmp_path.glob("hooks.json.bak-*")) == []
        out = capsys.readouterr().out
        assert "already up to date" in out
        assert "/hooks" in out  # trust instructions still shown


class TestInstallPreservesTargetIdentity:
    def test_backup_name_follows_custom_basename(self, tmp_path):
        target = tmp_path / "custom-hooks.json"
        target.write_text(json.dumps({"hooks": {}}))
        install_codex_hooks(target)
        assert list(tmp_path.glob("custom-hooks.json.bak-*"))
        assert list(tmp_path.glob("hooks.json.bak-*")) == []

    def test_mode_preserved_on_target_and_backup(self, tmp_path):
        target = tmp_path / "hooks.json"
        target.write_text(json.dumps({"hooks": {}}))
        target.chmod(0o600)
        install_codex_hooks(target)
        assert target.stat().st_mode & 0o777 == 0o600
        backup = next(iter(tmp_path.glob("hooks.json.bak-*")))
        assert backup.stat().st_mode & 0o777 == 0o600
