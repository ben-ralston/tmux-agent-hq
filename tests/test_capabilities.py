"""Tests for the launch.sh --capabilities probe (#205 Stage 1B Task 10)."""

from __future__ import annotations

from unittest import mock

import pytest

from agent_hq.capabilities import (
    CLAUDE_ONLY,
    clear_capabilities_cache,
    probe_launcher_capabilities,
    supported_flags,
    supports_harness,
)

CONTRACT = (
    '{"contract_version":1,"agents":["claude","codex"],'
    '"flags":{"claude":["--model","--effort","--accept-edits","--auto"],'
    '"codex":["--model","--effort"]}}'
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Never let a cached result leak between tests."""
    clear_capabilities_cache()
    yield
    clear_capabilities_cache()


def test_probe_parses_contract(tmp_path):
    clear_capabilities_cache()
    with mock.patch("agent_hq.capabilities.subprocess.run") as m:
        m.return_value = mock.Mock(returncode=0, stdout=CONTRACT)
        caps = probe_launcher_capabilities(tmp_path / "launch.sh")
    assert supports_harness(caps, "codex")
    assert "--effort" in supported_flags(caps, "codex")


def test_probe_uses_a_short_timeout(tmp_path):
    """Fix 7 (multi-review wave): the launcher prints its JSON instantly, so
    a hung/misbehaving launch.sh must not be able to block a request-serving
    thread for as long as 10s -- 3s is plenty of slack over "instant"."""
    clear_capabilities_cache()
    with mock.patch("agent_hq.capabilities.subprocess.run") as m:
        m.return_value = mock.Mock(returncode=0, stdout=CONTRACT)
        probe_launcher_capabilities(tmp_path / "launch.sh")
    assert m.call_args.kwargs["timeout"] == 3


def test_probe_failure_returns_claude_only(tmp_path):
    clear_capabilities_cache()
    with mock.patch(
        "agent_hq.capabilities.subprocess.run",
        side_effect=FileNotFoundError,
    ):
        caps = probe_launcher_capabilities(tmp_path / "launch.sh")
    assert caps == CLAUDE_ONLY and not supports_harness(caps, "codex")


def test_wrong_contract_version_claude_only(tmp_path):
    clear_capabilities_cache()
    bad_contract = (
        '{"contract_version":2,"agents":["claude","codex"],'
        '"flags":{"claude":["--model"],"codex":["--model"]}}'
    )
    with mock.patch("agent_hq.capabilities.subprocess.run") as m:
        m.return_value = mock.Mock(returncode=0, stdout=bad_contract)
        caps = probe_launcher_capabilities(tmp_path / "launch.sh")
    assert caps == CLAUDE_ONLY
    assert not supports_harness(caps, "codex")


def test_probe_result_cached(tmp_path):
    clear_capabilities_cache()
    launcher = tmp_path / "launch.sh"
    with mock.patch("agent_hq.capabilities.subprocess.run") as m:
        m.return_value = mock.Mock(returncode=0, stdout=CONTRACT)
        first = probe_launcher_capabilities(launcher)
        second = probe_launcher_capabilities(launcher)
    assert first == second
    m.assert_called_once()


def test_probe_result_cache_expires_after_ttl(tmp_path):
    clear_capabilities_cache()
    launcher = tmp_path / "launch.sh"
    with (
        mock.patch("agent_hq.capabilities.subprocess.run") as m,
        mock.patch("agent_hq.capabilities.time.monotonic") as mono,
    ):
        m.return_value = mock.Mock(returncode=0, stdout=CONTRACT)
        mono.return_value = 1000.0
        probe_launcher_capabilities(launcher, ttl=5.0)
        mono.return_value = 1006.0
        probe_launcher_capabilities(launcher, ttl=5.0)
    assert m.call_count == 2


def test_nonzero_returncode_claude_only(tmp_path):
    clear_capabilities_cache()
    with mock.patch("agent_hq.capabilities.subprocess.run") as m:
        m.return_value = mock.Mock(returncode=1, stdout="")
        caps = probe_launcher_capabilities(tmp_path / "launch.sh")
    assert caps == CLAUDE_ONLY


def test_timeout_claude_only(tmp_path):
    import subprocess

    clear_capabilities_cache()
    with mock.patch(
        "agent_hq.capabilities.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="launch.sh", timeout=10),
    ):
        caps = probe_launcher_capabilities(tmp_path / "launch.sh")
    assert caps == CLAUDE_ONLY


def test_unicode_decode_error_claude_only(tmp_path):
    """subprocess.run(text=True) decodes stdout internally and can raise
    UnicodeDecodeError on non-UTF-8 launcher output -- the probe must
    degrade to CLAUDE_ONLY like every other failure mode, not propagate."""
    clear_capabilities_cache()
    with mock.patch(
        "agent_hq.capabilities.subprocess.run",
        side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ):
        caps = probe_launcher_capabilities(tmp_path / "launch.sh")
    assert caps == CLAUDE_ONLY


def test_invalid_json_claude_only(tmp_path):
    clear_capabilities_cache()
    with mock.patch("agent_hq.capabilities.subprocess.run") as m:
        m.return_value = mock.Mock(returncode=0, stdout="not json")
        caps = probe_launcher_capabilities(tmp_path / "launch.sh")
    assert caps == CLAUDE_ONLY


def test_malformed_agents_type_claude_only(tmp_path):
    clear_capabilities_cache()
    bad_contract = '{"contract_version":1,"agents":"claude","flags":{}}'
    with mock.patch("agent_hq.capabilities.subprocess.run") as m:
        m.return_value = mock.Mock(returncode=0, stdout=bad_contract)
        caps = probe_launcher_capabilities(tmp_path / "launch.sh")
    assert caps == CLAUDE_ONLY


def test_missing_flags_defaults_to_empty_tuple(tmp_path):
    clear_capabilities_cache()
    contract = '{"contract_version":1,"agents":["claude","codex"]}'
    with mock.patch("agent_hq.capabilities.subprocess.run") as m:
        m.return_value = mock.Mock(returncode=0, stdout=contract)
        caps = probe_launcher_capabilities(tmp_path / "launch.sh")
    assert supports_harness(caps, "codex")
    assert supported_flags(caps, "codex") == ()


def test_supports_harness_false_for_unknown():
    assert not supports_harness(CLAUDE_ONLY, "codex")
    assert supports_harness(CLAUDE_ONLY, "claude")


def test_supported_flags_returns_tuple():
    assert isinstance(supported_flags(CLAUDE_ONLY, "claude"), tuple)


def test_claude_only_constant_shape():
    assert CLAUDE_ONLY.agents == frozenset({"claude"})
    assert CLAUDE_ONLY.flags == {}


def test_default_launcher_used_when_none_passed():
    clear_capabilities_cache()
    with mock.patch("agent_hq.capabilities.subprocess.run") as m:
        m.return_value = mock.Mock(returncode=0, stdout=CONTRACT)
        probe_launcher_capabilities(None)
    args = m.call_args[0][0]
    from agent_hq.capabilities import DEFAULT_LAUNCHER_PATH

    assert args[0] == str(DEFAULT_LAUNCHER_PATH)
    assert args[1] == "--capabilities"
