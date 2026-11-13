"""`agent-hq remote …`: serve, check-config, tokens, kill switch, LaunchAgents (#396)."""

import io
import plistlib

import pytest

from agent_hq import cli as hq_cli
from agent_hq import config as hq_config
from agent_hq import eventlog
from agent_hq.remote import cli as remote_cli
from agent_hq.remote import config as remote_config
from agent_hq.remote import service as remote_service
from agent_hq.remote.auth import TokenStore

REMOTE_YAML = """
remote:
  public_url: https://agents.example.com
  allowed_github_login: ben-ralston
  allowed_github_id: 1001
  github_client_id: gh-client-id
"""


@pytest.fixture
def configured(monkeypatch):
    path = hq_config.CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(REMOTE_YAML)
    monkeypatch.setattr(remote_config, "keychain_secret", lambda: "s3cret")
    return path


@pytest.fixture
def emitted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        eventlog,
        "emit",
        lambda level, message, extra=None, **kw: calls.append((level, message, extra)) or True,
    )
    return calls


@pytest.fixture
def ran(monkeypatch):
    """Record every process-control argv the remote service would run."""
    calls = []

    class Done:
        def __init__(self, rc=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = rc, stdout, stderr

    def fake(argv):
        calls.append(list(argv))
        return Done(rc=1 if argv[:2] == ["launchctl", "print"] else 0)

    monkeypatch.setattr(remote_service, "_run", fake)
    monkeypatch.setattr(remote_service, "_uid", lambda: 501)
    return calls


def _parse(*argv):
    return hq_cli.create_parser().parse_args(["remote", *argv])


# --- parser --------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["serve"],
        ["check-config"],
        ["revoke-all"],
        ["allow-client", "abc"],
        ["kill"],
        ["service", "install"],
        ["service", "install", "--tunnel"],
        ["service", "uninstall"],
        ["service", "status"],
    ],
)
def test_parser_has_remote_subcommands(argv):
    args = _parse(*argv)
    assert args.subcommand == "remote"
    assert args.remote_action == argv[0]


def test_bare_remote_prints_help_and_returns_2(capsys):
    assert remote_cli.run_remote(_parse()) == 2
    assert "serve" in capsys.readouterr().out


# --- config-driven commands ------------------------------------------------


def test_remote_without_config_block_exits_2(capsys):
    assert remote_cli.run_remote(_parse("check-config")) == 2
    assert "no remote: block" in capsys.readouterr().err


def test_check_config_prints_tools_identity_and_automations(configured, capsys):
    assert remote_cli.run_remote(_parse("check-config")) == 0
    out = capsys.readouterr().out
    for text in (
        "https://agents.example.com/mcp",
        "http://127.0.0.1:9894/mcp",
        "ben-ralston (id 1001)",
        "automations_health, list_agents, list_issues, recent_errors",
        "client secret:   found",
        "label: com.benralston.agent-hq.remote\n",
        "label: com.benralston.agent-hq.remote-tunnel\n",
        "stale_after: 20m",
    ):
        assert text in out, text
    assert "s3cret" not in out
    assert "writes:          off" in out
    assert "dispatch_issues" not in out


def test_check_config_lists_write_tools_when_enabled(configured, capsys):
    configured.write_text(REMOTE_YAML + "  writes: true\n")
    assert remote_cli.run_remote(_parse("check-config")) == 0
    out = capsys.readouterr().out
    assert "writes:          on" in out
    assert "dispatch_issues" in out and "set_triage" in out


def test_revoke_all_and_allow_client_need_no_secret(configured, monkeypatch, capsys):
    def boom():
        raise AssertionError("no secret needed")

    monkeypatch.setattr(remote_config, "keychain_secret", boom)
    assert remote_cli.run_remote(_parse("allow-client", "client-123")) == 0
    store = TokenStore(remote_config.STATE_DIR)
    assert not store.client_allowed("someone-else")
    assert remote_cli.run_remote(_parse("revoke-all")) == 0
    assert store.client_allowed("someone-else")  # pin cleared
    assert "Claude must sign in again" in capsys.readouterr().out


# --- serve -------------------------------------------------------------------


def test_serve_config_error_is_reported_and_exits_2(emitted, monkeypatch):
    monkeypatch.setattr(remote_cli.request_log, "configure_remote_logging", lambda: None)
    monkeypatch.setattr(
        remote_cli, "_run_server", lambda *a: pytest.fail("must not serve without config")
    )
    assert remote_cli.run_remote(_parse("serve")) == 2
    assert (
        "error",
        "remote config or secret failed to load",
        {"error_type": "RemoteConfigError"},
    ) in emitted


def test_serve_starts_with_remote_role_and_event(configured, emitted, monkeypatch):
    served = {}
    monkeypatch.setattr(remote_cli.request_log, "configure_remote_logging", lambda: None)
    monkeypatch.setattr(remote_cli, "_run_server", lambda app, cfg: served.update(cfg=cfg))
    monkeypatch.setattr(eventlog, "_role", "cli")
    assert remote_cli.run_remote(_parse("serve")) == 0
    assert served["cfg"].host == "127.0.0.1" and served["cfg"].port == 9894
    assert eventlog._role == "remote"
    assert ("info", "remote mcp started", {"port": 9894}) in emitted


def test_serve_failure_is_reported(configured, emitted, monkeypatch):
    monkeypatch.setattr(remote_cli.request_log, "configure_remote_logging", lambda: None)

    def bind_fails(app, cfg):
        raise SystemExit(1)

    monkeypatch.setattr(remote_cli, "_run_server", bind_fails)
    assert remote_cli.run_remote(_parse("serve")) == 1
    assert any(m == "remote server exited with an error" for _, m, _ in emitted)


async def test_heartbeat_loop_writes_lines():
    import anyio

    stream = io.StringIO()
    with anyio.move_on_after(0.1):
        await remote_cli.heartbeat_loop(stream, interval=0.01)
    lines = [ln for ln in stream.getvalue().splitlines() if ln]
    assert len(lines) >= 2
    assert all(ln.endswith("agent-hq remote: heartbeat") for ln in lines)


# --- LaunchAgents and the kill switch --------------------------------------


def test_kill_order_and_scope(ran, capsys):
    assert remote_service.kill() == 0
    assert ran[:3] == [
        ["launchctl", "bootout", "gui/501/com.benralston.agent-hq.remote-tunnel"],
        ["launchctl", "bootout", "gui/501/com.benralston.agent-hq.remote"],
        ["pkill", "-f", "agent-hq remote serve"],
    ]
    assert all(argv[:2] == ["launchctl", "print"] for argv in ran[3:])
    flat = " ".join(" ".join(c) for c in ran)
    assert "notes-graph" not in flat and "cloudflared" not in flat


def test_server_plist_shape():
    plist = plistlib.loads(
        remote_service.build_server_plist("/usr/local/bin/agent-hq", path_env="/usr/bin").encode()
    )
    assert plist["Label"] == "com.benralston.agent-hq.remote"
    assert plist["ProgramArguments"] == ["/usr/local/bin/agent-hq", "remote", "serve"]
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["ThrottleInterval"] == 10 and plist["ExitTimeOut"] == 30
    assert plist["StandardErrorPath"] == str(remote_service.SERVER_LOG)
    assert plist["StandardOutPath"] == str(remote_service.SERVER_LOG)
    assert plist["EnvironmentVariables"] == {"PATH": "/usr/bin"}


def test_tunnel_plist_shape():
    plist = plistlib.loads(
        remote_service.build_tunnel_plist("/opt/homebrew/bin/cloudflared").encode()
    )
    assert plist["Label"] == "com.benralston.agent-hq.remote-tunnel"
    assert plist["ProgramArguments"] == [
        "/opt/homebrew/bin/cloudflared",
        "tunnel",
        "--config",
        str(remote_service.TUNNEL_CONFIG),
        "run",
    ]
    assert plist["StandardErrorPath"] == str(remote_service.TUNNEL_LOG)


def test_install_tunnel_refuses_without_config(ran, capsys):
    assert remote_service.install(tunnel=True) == 1
    assert "agent-hq-mcp.yml" in capsys.readouterr().err
    assert ran == []


def test_install_server_writes_plist_and_bootstraps(ran, monkeypatch):
    monkeypatch.setattr(remote_service.web_service, "_resolve_executable", lambda: "/bin/agent-hq")
    assert remote_service.install(tunnel=False) == 0
    plist_path = remote_service.LAUNCH_AGENTS_DIR / "com.benralston.agent-hq.remote.plist"
    assert plistlib.loads(plist_path.read_bytes())["ProgramArguments"][1:] == ["remote", "serve"]
    assert ["launchctl", "bootstrap", "gui/501", str(plist_path)] in ran


def test_uninstall_boots_out_both_and_removes_plists(ran):
    remote_service.LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for label in (remote_service.LABEL, remote_service.TUNNEL_LABEL):
        (remote_service.LAUNCH_AGENTS_DIR / f"{label}.plist").write_text("x")
    assert remote_service.uninstall() == 0
    assert not list(remote_service.LAUNCH_AGENTS_DIR.glob("*.plist"))
    assert ["launchctl", "bootout", "gui/501/com.benralston.agent-hq.remote"] in ran


# --- review round 1 ----------------------------------------------------------


@pytest.mark.parametrize("yaml_text", ["repos: {}\n", "remote: [broken\n"])
def test_revoke_all_works_without_a_valid_remote_block(yaml_text, monkeypatch, capsys):
    """The emergency revoke must not depend on projects.yaml parsing."""
    hq_config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    hq_config.CONFIG_PATH.write_text(yaml_text)
    store = TokenStore(remote_config.STATE_DIR)
    store.allow_client("pinned-client")
    assert remote_cli.run_remote(_parse("revoke-all")) == 0
    assert store.client_allowed("anyone")  # pin cleared


def test_serve_failure_lines_match_the_automations_failure_pattern(
    configured, emitted, monkeypatch, capsys
):
    monkeypatch.setattr(remote_cli.request_log, "configure_remote_logging", lambda: None)

    def bind_fails(app, cfg):
        raise SystemExit(1)

    monkeypatch.setattr(remote_cli, "_run_server", bind_fails)
    remote_cli.run_remote(_parse("serve"))
    assert capsys.readouterr().err.strip().splitlines()[-1].startswith("ERROR ")


def test_serve_config_failure_line_matches_the_failure_pattern(emitted, monkeypatch, capsys):
    monkeypatch.setattr(remote_cli.request_log, "configure_remote_logging", lambda: None)
    remote_cli.run_remote(_parse("serve"))
    assert capsys.readouterr().err.strip().splitlines()[-1].startswith("ERROR ")


def test_service_error_is_one_line_not_a_traceback(monkeypatch, capsys):
    from agent_hq.service import ServiceError

    def missing():
        raise ServiceError("agent-hq not found on PATH")

    monkeypatch.setattr(remote_service.web_service, "_resolve_executable", missing)
    assert remote_cli.run_remote(_parse("service", "install")) == 2
    assert "agent-hq not found on PATH" in capsys.readouterr().err


def test_kill_disables_plists_so_reboot_does_not_restart(ran):
    remote_service.LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for label in (remote_service.LABEL, remote_service.TUNNEL_LABEL):
        (remote_service.LAUNCH_AGENTS_DIR / f"{label}.plist").write_text("x")
    assert remote_service.kill() == 0
    names = sorted(p.name for p in remote_service.LAUNCH_AGENTS_DIR.iterdir())
    assert names == [
        "com.benralston.agent-hq.remote-tunnel.plist.disabled",
        "com.benralston.agent-hq.remote.plist.disabled",
    ]


def test_kill_fails_loudly_when_a_job_is_still_loaded(monkeypatch, capsys):
    class Done:
        def __init__(self, rc):
            self.returncode, self.stdout, self.stderr = rc, "", "Input/output error"

    # bootout fails AND print still finds the tunnel: the kill did not take.
    monkeypatch.setattr(
        remote_service,
        "_run",
        lambda argv: Done(0 if argv[:2] == ["launchctl", "print"] and "tunnel" in argv[2] else 1),
    )
    monkeypatch.setattr(remote_service, "_uid", lambda: 501)
    assert remote_service.kill() == 1
    assert "STILL LOADED" in capsys.readouterr().err


def test_install_replaces_a_disabled_plist(ran, monkeypatch):
    monkeypatch.setattr(remote_service.web_service, "_resolve_executable", lambda: "/bin/agent-hq")
    remote_service.LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    disabled = remote_service.LAUNCH_AGENTS_DIR / "com.benralston.agent-hq.remote.plist.disabled"
    disabled.write_text("old")
    assert remote_service.install(tunnel=False) == 0
    assert not disabled.exists()


def test_check_config_lists_the_allowed_sessions(configured, capsys):
    """#429: the start_session allowlist is visible without reading the yaml."""
    configured.write_text(REMOTE_YAML + "  writes: true\n")
    assert remote_cli.run_remote(_parse("check-config")) == 0
    assert "sessions:        none (remote.sessions)" in capsys.readouterr().out
    configured.write_text(
        REMOTE_YAML
        + "  writes: true\n  sessions:\n    daily-review:\n      prompt: /daily-review\n"
        + "    weekly:\n      prompt: /weekly-summary\n"
    )
    assert remote_cli.run_remote(_parse("check-config")) == 0
    out = capsys.readouterr().out
    assert "sessions:        daily-review, weekly (remote.sessions)" in out
    assert "start_session" in out
