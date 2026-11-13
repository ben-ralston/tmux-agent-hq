"""Tests for config loading and validation."""

import re
from pathlib import Path

import pytest

from agent_hq.config import (
    AutopilotConfig,
    ConfigError,
    HarnessDefaults,
    _parse_autopilot,
    load_config,
)


class TestLoadConfig:
    def test_missing_file_returns_none(self, tmp_path):
        result = load_config(tmp_path / "nonexistent.yaml")
        assert result is None

    def test_valid_config_returns_config(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  ben-ralston/agent-hq:
    local_path: ~/Programming/agent-hq

projects:
  - name: Agent HQ
    number: 12
    owner: "@me"
""")
        config = load_config(config_file)
        assert config is not None
        assert "ben-ralston/agent-hq" in config.repos
        assert len(config.projects) == 1
        assert config.projects[0].name == "Agent HQ"
        assert config.projects[0].number == 12
        assert config.projects[0].owner == "@me"

    def test_tilde_expanded_in_local_path(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: ~/Programming/repo
projects:
  - name: R
    number: 1
    owner: "@me"
""")
        config = load_config(config_file)
        assert "~" not in str(config.repos["o/r"].local_path)
        assert str(config.repos["o/r"].local_path).startswith("/")

    @pytest.mark.parametrize(
        ("line", "expected"),
        [("    base_branch: dev\n", "dev"), ('    base_branch: " dev "\n', "dev"), ("", None)],
    )
    def test_repo_base_branch_override(self, tmp_path, line, expected):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text(f"repos:\n  o/r:\n    local_path: /tmp/r\n{line}")
        assert load_config(config_file).repos["o/r"].base_branch == expected

    @pytest.mark.parametrize("value", ['""', '"  "', "3", "[dev]"])
    def test_repo_base_branch_must_be_nonblank_string(self, tmp_path, value):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text(
            f"repos:\n  o/r:\n    local_path: /tmp/r\n    base_branch: {value}\n"
        )
        with pytest.raises(ConfigError, match="base_branch"):
            load_config(config_file)

    def test_missing_repos_defaults_to_empty(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
projects:
  - name: R
    number: 1
    owner: "@me"
""")
        config = load_config(config_file)
        assert config.repos == {}
        assert len(config.projects) == 1

    def test_missing_projects_defaults_to_empty(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
""")
        config = load_config(config_file)
        assert config.projects == []
        assert "o/r" in config.repos

    def test_invalid_yaml_raises_config_error(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text(": [invalid yaml {{{")
        with pytest.raises(ConfigError, match="YAML"):
            load_config(config_file)

    def test_default_worktree_root(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
projects:
  - name: R
    number: 1
    owner: "@me"
""")
        config = load_config(config_file)
        assert config.worktree_root == Path.home() / ".claude-worktrees"

    def test_custom_worktree_root(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
projects:
  - name: R
    number: 1
    owner: "@me"
worktree_root: ~/my-worktrees
""")
        config = load_config(config_file)
        assert "~" not in str(config.worktree_root)

    def test_default_permission_mode_is_accept_edits(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
projects:
  - name: R
    number: 1
    owner: "@me"
""")
        config = load_config(config_file)
        assert config.dispatch.permission_mode == "acceptEdits"

    def test_permission_mode_override_from_yaml(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
projects:
  - name: R
    number: 1
    owner: "@me"
dispatch:
  permission_mode: default
""")
        config = load_config(config_file)
        assert config.dispatch.permission_mode == "default"

    def test_invalid_permission_mode_raises_config_error(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
projects:
  - name: R
    number: 1
    owner: "@me"
dispatch:
  permission_mode: acceptedits
""")
        with pytest.raises(ConfigError, match="permission_mode"):
            load_config(config_file)

    def test_dispatch_defaults_unchanged(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("dispatch:\n  permission_mode: acceptEdits\n")
        config = load_config(config_file)
        assert config.dispatch.default_harness == "claude"
        assert config.dispatch.harnesses == {}

    def test_harnesses_block_parsed(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text(
            "dispatch:\n  default_harness: codex\n  harnesses:\n    codex:\n      effort: high\n"
        )
        config = load_config(config_file)
        assert config.dispatch.default_harness == "codex"
        assert config.dispatch.harnesses["codex"] == HarnessDefaults(effort="high")

    def test_invalid_harness_name_fails_loud(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("dispatch:\n  harnesses:\n    aider: {}\n")
        with pytest.raises(ConfigError, match="harness"):
            load_config(config_file)

    def test_invalid_default_harness_fails_loud(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("dispatch:\n  default_harness: aider\n")
        with pytest.raises(ConfigError, match="default_harness"):
            load_config(config_file)

    def test_invalid_effort_fails_loud(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("dispatch:\n  harnesses:\n    codex:\n      effort: turbo\n")
        with pytest.raises(ConfigError, match="effort"):
            load_config(config_file)

    def test_invalid_harness_model_type_fails_loud(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("dispatch:\n  harnesses:\n    codex:\n      model: 5\n")
        with pytest.raises(ConfigError, match="model"):
            load_config(config_file)

    def test_empty_harness_model_fails_loud(self, tmp_path):
        """Fix 6 (multi-review wave): an empty ``model`` string must be
        rejected at config-load time -- mirrors resolve_dispatch_selection's
        rule -- rather than silently persisting and later blowing up (or
        worse, riding argv unvalidated) at dispatch time."""
        config_file = tmp_path / "projects.yaml"
        config_file.write_text('dispatch:\n  harnesses:\n    codex:\n      model: ""\n')
        with pytest.raises(ConfigError, match="model"):
            load_config(config_file)

    def test_whitespace_harness_model_fails_loud(self, tmp_path):
        """A model value containing whitespace rides /worktree-create's argv
        as a single token; splitting on whitespace there would splice
        phantom arguments in, so config-load must reject it up front."""
        config_file = tmp_path / "projects.yaml"
        config_file.write_text('dispatch:\n  harnesses:\n    codex:\n      model: "a b"\n')
        with pytest.raises(ConfigError, match="model"):
            load_config(config_file)


class TestAutomationsConfig:
    def test_automations_absent_is_empty_list(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("repos: {}\n")
        config = load_config(config_file)
        assert config.automations == []

    def test_automations_full_entry_parsed(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
automations:
  - name: nightly-backup
    label: com.example.backup
    log_file: /var/log/backup.log
""")
        config = load_config(config_file)
        assert len(config.automations) == 1
        auto = config.automations[0]
        assert auto.name == "nightly-backup"
        assert auto.label == "com.example.backup"
        assert auto.log_file == Path("/var/log/backup.log")

    def test_automations_defaults_derived_from_name(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
automations:
  - name: daily-digest
""")
        config = load_config(config_file)
        auto = config.automations[0]
        assert auto.label == "com.claude.daily-digest"
        assert auto.log_file == Path.home() / ".claude" / "logs" / "daily-digest.log"

    def test_automations_log_file_tilde_expanded(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
automations:
  - name: x
    log_file: ~/custom/x.log
""")
        config = load_config(config_file)
        assert config.automations[0].log_file == Path.home() / "custom" / "x.log"

    def test_automations_not_a_list_raises(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
automations:
  name: backup
""")
        with pytest.raises(ConfigError, match="automations"):
            load_config(config_file)

    def test_automations_entry_not_mapping_raises(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
automations:
  - backup
""")
        with pytest.raises(ConfigError, match="automations"):
            load_config(config_file)

    def test_automations_missing_name_raises(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
automations:
  - label: com.example.thing
""")
        with pytest.raises(ConfigError, match="name"):
            load_config(config_file)

    def test_automations_non_string_name_raises(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
automations:
  - name: 123
""")
        with pytest.raises(ConfigError, match="name"):
            load_config(config_file)

    def test_automations_patterns_parsed(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
automations:
  - name: nightly-backup
    failure_pattern: "FAILED"
    success_pattern: "report complete"
""")
        auto = load_config(config_file).automations[0]
        assert auto.failure_pattern == "FAILED"
        assert auto.success_pattern == "report complete"

    def test_automations_patterns_default_none(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
automations:
  - name: x
""")
        auto = load_config(config_file).automations[0]
        assert auto.failure_pattern is None
        assert auto.success_pattern is None

    def test_automations_non_string_pattern_raises(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
automations:
  - name: x
    failure_pattern: 123
""")
        with pytest.raises(ConfigError, match="failure_pattern"):
            load_config(config_file)

    def test_automations_invalid_regex_pattern_raises(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
automations:
  - name: x
    success_pattern: "(unclosed"
""")
        with pytest.raises(ConfigError, match="success_pattern"):
            load_config(config_file)

    def test_automations_stale_after_parsed(self, tmp_path):
        from datetime import timedelta

        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
automations:
  - name: a
    stale_after: 90s
  - name: b
    stale_after: 3m
  - name: c
    stale_after: 2h
  - name: d
""")
        autos = load_config(config_file).automations
        assert [a.stale_after for a in autos] == [
            timedelta(seconds=90),
            timedelta(minutes=3),
            timedelta(hours=2),
            None,
        ]

    def test_automations_invalid_stale_after_raises(self, tmp_path):
        for bad in ('"3 minutes"', "0m", "180", '""'):
            config_file = tmp_path / "projects.yaml"
            config_file.write_text(f"""
automations:
  - name: x
    stale_after: {bad}
""")
            with pytest.raises(ConfigError, match="stale_after"):
                load_config(config_file)


class TestMalformedEntriesRaiseConfigError:
    """#170 audit AHQ-011: malformed repos/projects entries raised raw
    KeyError/TypeError/AttributeError, bypassing every caller's ConfigError
    path (TUI crashed on mount; web service refused to start)."""

    def _load(self, tmp_path, text):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text(text)
        return load_config(config_file)

    def test_repo_missing_local_path(self, tmp_path):
        with pytest.raises(ConfigError):
            self._load(tmp_path, "repos:\n  o/r: {}\n")

    def test_repos_is_a_list(self, tmp_path):
        with pytest.raises(ConfigError):
            self._load(tmp_path, "repos:\n  - o/r\n")

    def test_repo_value_is_scalar(self, tmp_path):
        with pytest.raises(ConfigError):
            self._load(tmp_path, "repos:\n  o/r: just-a-string\n")

    def test_project_missing_number(self, tmp_path):
        with pytest.raises(ConfigError):
            self._load(tmp_path, "projects:\n  - name: Roadmap\n    owner: o\n")

    def test_project_entry_is_scalar(self, tmp_path):
        with pytest.raises(ConfigError):
            self._load(tmp_path, "projects:\n  - just-a-string\n")

    def test_projects_is_a_scalar(self, tmp_path):
        # Toolkit review of AHQ-011: the top-level container itself must
        # also be guarded, not just per-entry construction.
        with pytest.raises(ConfigError):
            self._load(tmp_path, "projects: 42\n")


class TestGithubConfig:
    """The `github:` section — GraphQL budget policy (#255)."""

    BASE = """
projects:
  - name: R
    number: 1
    owner: "@me"
"""

    def _load(self, tmp_path, github_yaml):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text(self.BASE + github_yaml)
        return load_config(config_file)

    def test_absent_section_uses_the_default_floor(self, tmp_path):
        assert self._load(tmp_path, "").github.graphql_floor == 500

    def test_floor_is_parsed(self, tmp_path):
        cfg = self._load(tmp_path, "\ngithub:\n  graphql_floor: 1200\n")
        assert cfg.github.graphql_floor == 1200

    def test_zero_floor_is_allowed(self, tmp_path):
        """Opting out of gating entirely is a legitimate choice."""
        cfg = self._load(tmp_path, "\ngithub:\n  graphql_floor: 0\n")
        assert cfg.github.graphql_floor == 0

    def test_non_integer_floor_raises(self, tmp_path):
        with pytest.raises(ConfigError):
            self._load(tmp_path, "\ngithub:\n  graphql_floor: lots\n")

    def test_boolean_floor_raises(self, tmp_path):
        """`True` is an int in Python; a YAML `true` here is a typo, not 1."""
        with pytest.raises(ConfigError):
            self._load(tmp_path, "\ngithub:\n  graphql_floor: true\n")

    def test_negative_floor_raises(self, tmp_path):
        with pytest.raises(ConfigError):
            self._load(tmp_path, "\ngithub:\n  graphql_floor: -1\n")

    def test_non_mapping_section_raises(self, tmp_path):
        with pytest.raises(ConfigError):
            self._load(tmp_path, "\ngithub: 42\n")


class TestTriageConfig:
    BASE = """
projects:
  - name: R
    number: 1
    owner: "@me"
"""

    def _load(self, tmp_path, triage_yaml):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text(self.BASE + triage_yaml)
        return load_config(config_file)

    def test_triage_config_parsed(self, tmp_path):
        cfg = self._load(
            tmp_path,
            """
triage:
  data_dir: /tmp/ahq-data
  sync_debounce_s: 120
""",
        )
        assert cfg.triage is not None
        assert cfg.triage.data_dir == Path("/tmp/ahq-data")
        assert cfg.triage.sync_debounce_s == 120
        assert cfg.triage.sync_enabled is True

    def test_triage_config_absent_is_none(self, tmp_path):
        cfg = self._load(tmp_path, "")
        assert cfg.triage is None

    def test_triage_config_expands_home(self, tmp_path):
        cfg = self._load(
            tmp_path,
            """
triage:
  data_dir: ~/triage-data
""",
        )
        assert cfg.triage.data_dir == Path.home() / "triage-data"

    def test_triage_config_missing_data_dir_raises(self, tmp_path):
        with pytest.raises(ConfigError):
            self._load(tmp_path, "triage: {}")

    def test_triage_config_sync_disabled(self, tmp_path):
        cfg = self._load(
            tmp_path,
            """
triage:
  data_dir: /tmp/x
  sync_enabled: false
""",
        )
        assert cfg.triage.sync_enabled is False

    def test_triage_config_bad_types_raise_config_error(self, tmp_path):
        # int()/bool() crashes must surface as ConfigError so the TUI/web/MCP
        # load_config call sites (which catch only ConfigError) degrade cleanly.
        with pytest.raises(ConfigError):
            self._load(tmp_path, 'triage:\n  data_dir: /tmp/x\n  sync_debounce_s: "5m"\n')
        with pytest.raises(ConfigError):
            self._load(tmp_path, "triage:\n  data_dir: /tmp/x\n  sync_debounce_s: [1]\n")
        with pytest.raises(ConfigError):
            self._load(tmp_path, 'triage:\n  data_dir: /tmp/x\n  sync_enabled: "yes please"\n')

    def test_triage_ai_defaults(self, tmp_path):
        cfg = self._load(tmp_path, "triage:\n  data_dir: /tmp/td\n")
        t = cfg.triage
        assert (t.ai_enabled, t.ai_model, t.ai_batch_cap, t.ai_timeout_s) == (
            True,
            "opus",
            15,
            300,
        )

    def test_triage_ai_overrides(self, tmp_path):
        cfg = self._load(
            tmp_path,
            "triage:\n  data_dir: /tmp/td\n  ai_enabled: false\n  ai_model: haiku\n"
            "  ai_batch_cap: 5\n  ai_timeout_s: 60\n",
        )
        t = cfg.triage
        assert (t.ai_enabled, t.ai_model, t.ai_batch_cap, t.ai_timeout_s) == (False, "haiku", 5, 60)

    @pytest.mark.parametrize(
        "line",
        [
            "  ai_enabled: yes_please",
            "  ai_model: ''",
            "  ai_model: 'son net'",
            "  ai_batch_cap: 0",
            "  ai_batch_cap: true",
            "  ai_timeout_s: -1",
        ],
    )
    def test_triage_ai_bad_values_raise(self, tmp_path, line):
        with pytest.raises(ConfigError, match="triage.ai_"):
            self._load(tmp_path, "triage:\n  data_dir: /tmp/td\n" + line + "\n")

    def test_triage_hygiene_defaults_and_overrides(self, tmp_path):
        t = self._load(tmp_path, "triage:\n  data_dir: /tmp/td\n").triage
        assert (t.hygiene_enabled, t.hygiene_model, t.hygiene_timeout_s) == (True, None, 600)
        t = self._load(
            tmp_path,
            "triage:\n  data_dir: /tmp/td\n  hygiene_enabled: false\n"
            "  hygiene_model: sonnet\n  hygiene_timeout_s: 30\n",
        ).triage
        assert (t.hygiene_enabled, t.hygiene_model, t.hygiene_timeout_s) == (False, "sonnet", 30)

    @pytest.mark.parametrize(
        "line",
        [
            "  hygiene_enabled: 1",
            "  hygiene_model: ''",
            "  hygiene_model: 'a b'",
            "  hygiene_timeout_s: 0",
            "  hygiene_timeout_s: true",
        ],
    )
    def test_triage_hygiene_bad_values_raise(self, tmp_path, line):
        with pytest.raises(ConfigError, match="triage.hygiene_"):
            self._load(tmp_path, "triage:\n  data_dir: /tmp/td\n" + line + "\n")


class TestAutopilotConfig:
    REPOS = {"ben-ralston/agent-hq"}

    def test_absent_is_none(self):
        assert _parse_autopilot(None, self.REPOS) is None

    def test_defaults(self):
        cfg = _parse_autopilot({}, self.REPOS)
        assert cfg == AutopilotConfig()
        assert cfg.enabled is False and cfg.per_night == 2 and cfg.max_open == 3
        assert cfg.turn_bound == 40 and cfg.model == "opus" and cfg.repos == ()

    def test_full(self):
        cfg = _parse_autopilot(
            {
                "enabled": True,
                "per_night": 1,
                "max_open": 2,
                "turn_bound": 20,
                "model": "sonnet",
                "repos": ["ben-ralston/agent-hq"],
            },
            self.REPOS,
        )
        assert cfg.enabled and cfg.per_night == 1 and cfg.repos == ("ben-ralston/agent-hq",)
        assert cfg.max_open == 2 and cfg.turn_bound == 20 and cfg.model == "sonnet"

    @pytest.mark.parametrize(
        "raw, msg",
        [
            ([], "autopilot: must be a mapping"),
            ({"enabled": "yes"}, "autopilot.enabled must be a boolean"),
            ({"per_night": 0}, "autopilot.per_night must be a positive integer"),
            ({"max_open": True}, "autopilot.max_open must be a positive integer"),
            ({"turn_bound": -1}, "autopilot.turn_bound must be a positive integer"),
            ({"model": "op us"}, "autopilot.model must be a non-empty, whitespace-free string"),
            ({"repos": "ben-ralston/agent-hq"}, "autopilot.repos must be a list"),
            ({"repos": ["nobody/else"]}, "autopilot.repos: 'nobody/else' is not a configured repo"),
        ],
    )
    def test_invalid(self, raw, msg):
        with pytest.raises(ConfigError, match=re.escape(msg)):
            _parse_autopilot(raw, self.REPOS)

    def test_load_config_wires_autopilot(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  ben-ralston/agent-hq:
    local_path: ~/Programming/agent-hq
autopilot:
  enabled: true
  repos: [ben-ralston/agent-hq]
""")
        config = load_config(config_file)
        assert config.autopilot is not None
        assert config.autopilot.enabled is True
        assert config.autopilot.repos == ("ben-ralston/agent-hq",)

    def test_load_config_absent_autopilot_is_none(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("repos: {}\n")
        assert load_config(config_file).autopilot is None

    def test_load_config_rejects_unconfigured_repo(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("autopilot:\n  repos: [nobody/else]\n")
        with pytest.raises(ConfigError, match="not a configured repo"):
            load_config(config_file)

