"""Tests for Idea Inbox config additions: RepoConfig.project/description, IdeaInboxConfig."""

import pytest

from agent_hq.config import ConfigError, IdeaInboxConfig, load_config


class TestRepoProjectField:
    def test_repo_project_field_parsed(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  ben-ralston/agent-hq:
    local_path: /tmp/agent-hq
    project: Agent HQ
    description: "Agent monitoring CLI."
projects:
  - name: Agent HQ
    number: 12
    owner: "@me"
""")
        config = load_config(config_file)
        repo = config.repos["ben-ralston/agent-hq"]
        assert repo.project == "Agent HQ"
        assert repo.description == "Agent monitoring CLI."

    def test_repo_project_field_optional(self, tmp_path):
        """A repo without `project` is allowed — it just won't be an Idea Inbox candidate."""
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
        assert config.repos["o/r"].project is None
        assert config.repos["o/r"].description is None

    def test_unknown_project_name_raises(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
    project: Nonexistent
projects:
  - name: R
    number: 1
    owner: "@me"
""")
        with pytest.raises(ConfigError, match="Nonexistent"):
            load_config(config_file)


class TestIdeaInboxConfig:
    def test_defaults_when_block_missing(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
""")
        config = load_config(config_file)
        # Default block when missing — every field has a sensible default.
        assert config.idea_inbox is not None
        assert isinstance(config.idea_inbox, IdeaInboxConfig)
        assert config.idea_inbox.model == "claude-sonnet-4-6"
        assert config.idea_inbox.confidence_threshold == 0.75
        assert config.idea_inbox.anthropic_api_key_env == "ANTHROPIC_API_KEY"

    def test_overrides_parsed(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
idea_inbox:
  model: claude-haiku-4-5
  confidence_threshold: 0.6
  anthropic_api_key_env: MY_KEY
""")
        config = load_config(config_file)
        assert config.idea_inbox.model == "claude-haiku-4-5"
        assert config.idea_inbox.confidence_threshold == 0.6
        assert config.idea_inbox.anthropic_api_key_env == "MY_KEY"

    def test_api_key_command_defaults_none(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
""")
        config = load_config(config_file)
        assert config.idea_inbox.anthropic_api_key_command is None

    def test_api_key_command_parsed(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
idea_inbox:
  anthropic_api_key_command: [dcli, n, anthropic-agent-hq-key]
""")
        config = load_config(config_file)
        assert config.idea_inbox.anthropic_api_key_command == (
            "dcli",
            "n",
            "anthropic-agent-hq-key",
        )

    def test_api_key_command_must_be_list_of_strings(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
idea_inbox:
  anthropic_api_key_command: "dcli n key"
""")
        with pytest.raises(ConfigError, match="anthropic_api_key_command"):
            load_config(config_file)

    def test_api_key_command_rejects_empty_list(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
idea_inbox:
  anthropic_api_key_command: []
""")
        with pytest.raises(ConfigError, match="anthropic_api_key_command"):
            load_config(config_file)

    def test_invalid_threshold_raises(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
idea_inbox:
  confidence_threshold: 1.5
""")
        with pytest.raises(ConfigError, match="confidence_threshold"):
            load_config(config_file)

    def test_negative_threshold_raises(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
idea_inbox:
  confidence_threshold: -0.1
""")
        with pytest.raises(ConfigError, match="confidence_threshold"):
            load_config(config_file)

    def test_non_mapping_block_raises(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
idea_inbox: "not a mapping"
""")
        with pytest.raises(ConfigError, match="idea_inbox"):
            load_config(config_file)

