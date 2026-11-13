"""Tests for the `web:` config block (WebConfig). #137"""

import pytest

from agent_hq.config import ConfigError, WebConfig, load_config


class TestWebConfig:
    def test_defaults_when_block_missing(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
repos:
  o/r:
    local_path: /tmp/r
""")
        config = load_config(config_file)
        assert isinstance(config.web, WebConfig)
        assert config.web.ios_jump_shortcut == "Agent HQ Terminal"

    def test_custom_shortcut_name(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
web:
  ios_jump_shortcut: "Open Blink"
""")
        config = load_config(config_file)
        assert config.web.ios_jump_shortcut == "Open Blink"

    def test_null_disables(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
web:
  ios_jump_shortcut: null
""")
        config = load_config(config_file)
        assert config.web.ios_jump_shortcut is None

    def test_empty_string_disables(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
web:
  ios_jump_shortcut: "   "
""")
        config = load_config(config_file)
        assert config.web.ios_jump_shortcut is None

    def test_name_is_stripped(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
web:
  ios_jump_shortcut: "  My Term  "
""")
        config = load_config(config_file)
        assert config.web.ios_jump_shortcut == "My Term"

    def test_empty_block_keeps_default(self, tmp_path):
        """`web: {}` (block present, key absent) keeps the default name."""
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
web: {}
""")
        config = load_config(config_file)
        assert config.web.ios_jump_shortcut == "Agent HQ Terminal"

    def test_non_mapping_block_raises(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
web: nope
""")
        with pytest.raises(ConfigError, match="web section must be a mapping"):
            load_config(config_file)

    def test_non_string_name_raises(self, tmp_path):
        config_file = tmp_path / "projects.yaml"
        config_file.write_text("""
web:
  ios_jump_shortcut: 42
""")
        with pytest.raises(ConfigError, match="ios_jump_shortcut"):
            load_config(config_file)
