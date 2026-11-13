"""Tests for web.errors wrappers around core action calls."""

from unittest.mock import patch

import pytest

from agent_hq.web.errors import ActionError, safe_kill, safe_mute, safe_rename


def test_safe_rename_raises_on_false():
    with patch("agent_hq.web.errors.rename_session", return_value=False):
        with pytest.raises(ActionError) as exc:
            safe_rename("%1", "new-name")
        assert "rename" in str(exc.value).lower()
        assert exc.value.pane_id == "%1"


def test_safe_rename_returns_on_true():
    with patch("agent_hq.web.errors.rename_session", return_value=True):
        safe_rename("%1", "new-name")  # no exception


def test_safe_kill_raises_on_false():
    with patch("agent_hq.web.errors.kill_agent", return_value=False):
        with pytest.raises(ActionError):
            safe_kill("%1")


def test_safe_kill_returns_on_true():
    with patch("agent_hq.web.errors.kill_agent", return_value=True):
        safe_kill("%1")  # no exception


def test_safe_mute_is_noop_wrapper():
    """Mute already returns a bool meaning 'now inactive' — don't raise."""
    with patch("agent_hq.web.errors.toggle_inactive", return_value=True):
        result = safe_mute("%1", "fp")
        assert result is True
