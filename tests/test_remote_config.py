"""The ``remote:`` block and the auth-facing RemoteConfig (#396)."""

import pytest

from agent_hq.config import ConfigError, RemoteSettings, load_config
from agent_hq.remote import config as remote_config
from agent_hq.remote.config import RemoteConfigError, load_remote_config

BASE = """
remote:
  public_url: https://agents.example.com/
  allowed_github_login: ben-ralston
  allowed_github_id: 1001
  github_client_id: gh-client-id
"""


def _load(tmp_path, text):
    path = tmp_path / "projects.yaml"
    path.write_text(text)
    return load_config(path)


def test_remote_block_absent_is_none(tmp_path):
    assert _load(tmp_path, "repos: {}\n").remote is None


def test_remote_block_parses(tmp_path):
    assert _load(tmp_path, BASE).remote == RemoteSettings(
        public_url="https://agents.example.com",
        allowed_github_login="ben-ralston",
        allowed_github_id=1001,
        github_client_id="gh-client-id",
        port=9894,
        writes=False,
    )


def test_remote_writes_opt_in(tmp_path):
    """#397: writes are off unless the block says so."""
    assert _load(tmp_path, BASE).remote.writes is False
    assert _load(tmp_path, BASE + "  writes: true\n").remote.writes is True


def test_remote_port_override(tmp_path):
    assert _load(tmp_path, BASE + "  port: 9895\n").remote.port == 9895


@pytest.mark.parametrize(
    "edit, match",
    [
        (("allowed_github_id: 1001", "allowed_github_id: true"), "allowed_github_id"),
        (("allowed_github_id: 1001", 'allowed_github_id: "1001"'), "allowed_github_id"),
        (("  github_client_id: gh-client-id\n", ""), "github_client_id"),
        (("  allowed_github_login: ben-ralston\n", ""), "allowed_github_login"),
        (("https://agents.example.com/", "http://agents.example.com"), "https"),
        (("https://agents.example.com/", "https://agents.example.com/mcp"), "scheme \\+ host"),
    ],
)
def test_remote_rejects_bad_fields(tmp_path, edit, match):
    with pytest.raises(ConfigError, match=match):
        _load(tmp_path, BASE.replace(*edit))


@pytest.mark.parametrize(
    "extra, match",
    [
        ("  host: 0.0.0.0\n", "host is fixed at 127.0.0.1"),
        ("  github_client_secret: s3cret\n", "Keychain"),
        ("  port: 0\n", "port"),
        ("  port: true\n", "port"),
        ("  colour: blue\n", "unknown key remote.colour"),
        ("  writes: yes please\n", "remote.writes must be true or false"),
        ("  writes: 1\n", "remote.writes must be true or false"),
    ],
)
def test_remote_rejects_extra_keys(tmp_path, extra, match):
    with pytest.raises(ConfigError, match=match):
        _load(tmp_path, BASE + extra)


def test_remote_must_be_a_mapping(tmp_path):
    with pytest.raises(ConfigError, match="remote: must be a mapping"):
        _load(tmp_path, "remote: yes\n")


def test_load_remote_config_reads_secret_from_lookup(tmp_path):
    cfg = load_remote_config(_load(tmp_path, BASE), keychain_lookup=lambda: "s3cret")
    assert cfg.github_client_secret == "s3cret"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 9894
    assert cfg.mcp_url == "https://agents.example.com/mcp"
    assert cfg.github_callback_url == "https://agents.example.com/oauth/github/callback"
    assert cfg.state_dir == remote_config.STATE_DIR
    assert cfg.log_path == remote_config.REQUEST_LOG
    assert "s3cret" not in repr(cfg)


def test_load_remote_config_without_secret_raises(tmp_path):
    with pytest.raises(RemoteConfigError, match="agent-hq-mcp-remote"):
        load_remote_config(_load(tmp_path, BASE), keychain_lookup=lambda: None)


def test_load_remote_config_require_secret_false_skips_lookup(tmp_path):
    def boom():
        raise AssertionError("lookup must not run")

    cfg = load_remote_config(_load(tmp_path, BASE), keychain_lookup=boom, require_secret=False)
    assert cfg.github_client_secret is None


@pytest.mark.parametrize("text", [None, "repos: {}\n"])
def test_load_remote_config_unconfigured_raises(tmp_path, text):
    cfg = None if text is None else _load(tmp_path, text)
    with pytest.raises(RemoteConfigError, match="no remote: block"):
        load_remote_config(cfg, keychain_lookup=lambda: "s3cret")


def test_real_keychain_is_blocked_in_tests():
    with pytest.raises(RuntimeError, match="real Keychain"):
        remote_config.keychain_secret()


# --- #429: the start_session allowlist ---------------------------------------

SESSIONS = (
    BASE
    + """  sessions:
    daily-review:
      prompt: /daily-review
      cwd: ~/scratch
    weekly:
      prompt: "/weekly-summary\\nthen stop"
      model: opus
      effort: high
"""
)


def test_remote_sessions_default_to_none(tmp_path):
    assert _load(tmp_path, BASE).remote.sessions == {}


def test_remote_sessions_parse(tmp_path):
    from pathlib import Path

    from agent_hq.config import RemoteSession

    sessions = _load(tmp_path, SESSIONS).remote.sessions
    assert sessions == {
        "daily-review": RemoteSession(
            name="daily-review", prompt="/daily-review", cwd=Path("~/scratch").expanduser()
        ),
        "weekly": RemoteSession(
            name="weekly", prompt="/weekly-summary\nthen stop", model="opus", effort="high"
        ),
    }


@pytest.mark.parametrize(
    "sessions, match",
    [
        ("  sessions: [daily-review]\n", "remote.sessions must be a mapping"),
        ("  sessions:\n    Plan Day:\n      prompt: x\n", "remote.sessions name"),
        ("  sessions:\n    -x:\n      prompt: x\n", "remote.sessions name"),
        ("  sessions:\n    a: /daily-review\n", "remote.sessions.a must be a mapping"),
        ("  sessions:\n    a: {}\n", "remote.sessions.a.prompt"),
        ("  sessions:\n    a:\n      prompt: '  '\n", "remote.sessions.a.prompt"),
        ('  sessions:\n    a:\n      prompt: "x\\ty"\n', "control characters"),
        ('  sessions:\n    a:\n      prompt: "x\\u0003"\n', "control characters"),
        ("  sessions:\n    a:\n      prompt: x\n      cwd: 3\n", "remote.sessions.a.cwd"),
        ("  sessions:\n    a:\n      prompt: x\n      model: 'a b'\n", "remote.sessions.a.model"),
        ("  sessions:\n    a:\n      prompt: x\n      model: --x\n", "remote.sessions.a.model"),
        ("  sessions:\n    a:\n      prompt: x\n      effort: huge\n", "remote.sessions.a.effort"),
        (
            "  sessions:\n    a:\n      prompt: x\n      shell: rm\n",
            "unknown key remote.sessions.a.shell",
        ),
        # Review round 1: a ConfigError, never a TypeError.
        ("  sessions:\n    a:\n      prompt: x\n      effort: [high]\n", "sessions.a.effort"),
        ("  sessions:\n    a:\n      prompt: x\n      1: y\n      zz: y\n", "unknown key"),
        # A relative folder would resolve against the server's own cwd.
        ("  sessions:\n    a:\n      prompt: x\n      cwd: scratch\n", "absolute path"),
        # The launch line is typed into a tty, which keeps ~1024 bytes of a line.
        ("  sessions:\n    a:\n      prompt: " + "x" * 801 + "\n", "at most 800 bytes"),
        ("  sessions:\n    a:\n      prompt: " + "é" * 401 + "\n", "at most 800 bytes"),
    ],
)
def test_remote_sessions_reject_bad_entries(tmp_path, sessions, match):
    with pytest.raises(ConfigError, match=match):
        _load(tmp_path, BASE + sessions)


def test_prompt_cap_is_on_the_shell_quoted_text(tmp_path):
    """Review round 2: each apostrophe is five bytes once quoted, and the
    whole typed line must stay under the tty's ~1024 bytes."""
    from agent_hq.dispatch import chat_launch_line

    body = "  sessions:\n    a:\n      model: " + "m" * 64 + "\n      effort: xhigh\n"
    with pytest.raises(ConfigError, match="at most 800 bytes"):
        _load(tmp_path, BASE + body + '      prompt: "' + "don't " * 100 + '"\n')
    longest = "x'" * 133  # 266 chars -> 800 bytes quoted
    text = BASE + body + '      prompt: "' + longest + '"\n'
    session = _load(tmp_path, text).remote.sessions["a"]
    line = chat_launch_line(
        "claude", model=session.model, effort=session.effort, prompt=session.prompt, name="n" * 40
    )
    assert len(line.encode()) < 1024


def test_remote_session_efforts_are_what_claude_accepts():
    from agent_hq.adapters import get_adapter
    from agent_hq.config import REMOTE_SESSION_EFFORTS

    assert REMOTE_SESSION_EFFORTS == get_adapter("claude").chat_efforts
