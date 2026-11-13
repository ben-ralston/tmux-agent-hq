"""The monitor must follow explicit TUI selection, never background events."""

import json
import tempfile

import pytest


def test_only_successful_frontend_selection_claims_thread():
    from agent_hq.codex_relay import ThreadSelection

    selected = []
    tracker = ThreadSelection(lambda thread: selected.append(thread))
    tracker.server(json.dumps({"method": "thread/started", "params": {"thread": {"id": "child"}}}))
    assert selected == []
    tracker.client(json.dumps({"id": 1, "method": "thread/resume", "params": {"threadId": "root"}}))
    tracker.server(json.dumps({"id": 1, "error": {"message": "not found"}}))
    assert selected == []
    tracker.client(json.dumps({"id": 2, "method": "thread/start", "params": {}}))
    tracker.server(
        json.dumps({"id": 2, "result": {"thread": {"id": "root", "path": "/tmp/root.jsonl"}}})
    )
    assert selected == [{"id": "root", "path": "/tmp/root.jsonl"}]


def test_out_of_order_selection_and_unsubscribe_do_not_restore_old_thread():
    from agent_hq.codex_relay import ThreadSelection

    selected = []
    tracker = ThreadSelection(selected.append)
    for request_id in (1, 2):
        tracker.client(json.dumps({"id": request_id, "method": "thread/resume", "params": {}}))
    tracker.server(json.dumps({"id": 2, "result": {"thread": {"id": "new"}}}))
    tracker.server(json.dumps({"id": 1, "result": {"thread": {"id": "old"}}}))
    tracker.client(
        json.dumps({"id": 3, "method": "thread/unsubscribe", "params": {"threadId": "old"}})
    )
    tracker.server(json.dumps({"id": 3, "result": {"status": "unsubscribed"}}))
    assert selected == [{"id": "new"}]
    tracker.client(
        json.dumps({"id": 4, "method": "thread/unsubscribe", "params": {"threadId": "new"}})
    )
    tracker.server(json.dumps({"id": 4, "result": {"status": "unsubscribed"}}))
    assert selected[-1] is None


@pytest.mark.asyncio
async def test_relay_forwards_approval_frames_without_answering(tmp_path):
    from websockets.asyncio.client import unix_connect
    from websockets.asyncio.server import unix_serve

    from agent_hq.codex_relay import relay

    approval = '{"id":99,"method":"item/commandExecution/requestApproval","params":{}}'
    answer = '{"id":99,"result":{"decision":"accept"}}'
    received = []

    async def upstream(ws):
        await ws.send(approval)
        received.append(await ws.recv())

    # macOS sockaddr_un paths are much shorter than pytest's default temp root.
    with tempfile.TemporaryDirectory(prefix="ahq276-", dir="/tmp") as short:
        server_path, proxy_path = f"{short}/up.sock", f"{short}/proxy.sock"

        async def proxy(ws):
            await relay(ws, server_path, lambda thread: None)

        async with unix_serve(upstream, server_path), unix_serve(proxy, proxy_path):
            async with unix_connect(proxy_path) as client:
                assert await client.recv() == approval
                await client.send(answer)
                # Closure arrives after upstream has read the response.
                await client.wait_closed()
    assert received == [answer]


def test_launch_arguments_preserve_resume_model_and_server_config():
    from agent_hq.codex_runtime import launch_commands

    original = ["-c", 'model_reasoning_effort="high"', "resume", "abc", "-m", "gpt-6-astra"]
    server, frontend = launch_commands(original, "/tmp/up.sock", "/tmp/client.sock")
    assert frontend == ["codex", "--remote", "unix:///tmp/client.sock", *original]
    assert server == [
        "codex",
        "app-server",
        "--listen",
        "unix:///tmp/up.sock",
        "-c",
        'model_reasoning_effort="high"',
    ]
    with pytest.raises(ValueError, match="remote"):
        launch_commands(["--remote", "ws://elsewhere"], "/tmp/up", "/tmp/client")


def test_receipt_requires_live_owner_and_frontend(tmp_path):
    from agent_hq.codex_runtime import read_registrations

    launch = tmp_path / "launch"
    launch.mkdir()
    receipt = {
        "version": 1,
        "generation": "abc",
        "pane_id": "%7",
        "wrapper_pid": 12,
        "frontend_pid": 13,
        "server_pid": 14,
        "thread_id": "root",
        "socket_path": str(launch / "server.sock"),
        "created_at": 100.0,
    }
    (launch / "registration.json").write_text(json.dumps(receipt))
    assert read_registrations(tmp_path, alive=lambda pid: pid != 12) == []
    assert read_registrations(tmp_path, alive=lambda pid: pid != 13) == []
    assert read_registrations(tmp_path, alive=lambda pid: True) == [receipt]


@pytest.mark.asyncio
async def test_backend_failure_terminates_frontend_and_cleanup_is_scoped(tmp_path):
    from agent_hq.codex_runtime import supervise

    class Child:
        def __init__(self, code=None):
            self.returncode = code
            self.terminated = False

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        async def wait(self):
            import asyncio

            while self.returncode is None:
                await asyncio.sleep(0.01)
            return self.returncode

    backend, frontend = Child(42), Child()
    assert await supervise(backend, frontend) != 0
    assert frontend.terminated


def test_cli_passes_all_native_arguments(monkeypatch):
    from agent_hq import cli, codex_runtime

    received = []
    monkeypatch.setattr("sys.argv", ["agent-hq", "codex", "-m", "model", "resume", "id"])
    monkeypatch.setattr(codex_runtime, "main", lambda args: received.append(args) or 0)
    assert cli.main() == 0
    assert received == [["-m", "model", "resume", "id"]]


@pytest.mark.asyncio
async def test_cancel_supervision_stops_both_children():
    import asyncio

    from agent_hq.codex_runtime import supervise

    class Child:
        returncode = None

        def __init__(self):
            self.finished = asyncio.Event()

        async def wait(self):
            await self.finished.wait()
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.finished.set()

    backend, frontend = Child(), Child()
    task = asyncio.create_task(supervise(backend, frontend))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.returncode == frontend.returncode == -15


def test_launcher_sigterm_cancels_owned_runtime(monkeypatch):
    import asyncio
    import signal

    from agent_hq import codex_runtime

    cleaned = []

    async def run(argv):
        try:
            asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGTERM)
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    monkeypatch.setattr(codex_runtime, "run_codex", run)
    # Catch default SIGTERM so RED cannot kill the test runner.
    previous = signal.signal(
        signal.SIGTERM, lambda *_: (_ for _ in ()).throw(RuntimeError("unhandled SIGTERM"))
    )
    try:
        assert codex_runtime.main([]) == 143
        assert cleaned == [True]
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_receipt_rejects_nonfinite_start_time(tmp_path):
    from agent_hq.codex_runtime import read_registrations

    launch = tmp_path / "launch"
    launch.mkdir()
    data = {
        "version": 1,
        "generation": "g",
        "pane_id": "%1",
        "wrapper_pid": 1,
        "frontend_pid": 2,
        "server_pid": 3,
        "socket_path": str(launch / "server.sock"),
        "created_at": float("nan"),
    }
    (launch / "registration.json").write_text(json.dumps(data))
    assert read_registrations(tmp_path, alive=lambda _: True) == []


def test_discovery_prefers_registered_frontend_over_server(monkeypatch):
    from agent_hq import discovery

    monkeypatch.setattr(
        discovery,
        "get_all_panes",
        lambda: [
            {
                "pane_pid": 1,
                "pane_id": "%7",
                "session_name": "s",
                "window_index": 0,
                "pane_index": 0,
            }
        ],
    )
    monkeypatch.setattr(
        discovery,
        "build_process_tree",
        lambda: ({1: [2], 2: [3, 4]}, {1: "zsh", 2: "agent-hq", 3: "codex", 4: "codex"}),
    )
    monkeypatch.setattr(
        "agent_hq.codex_runtime.read_registrations",
        lambda **_: [{"pane_id": "%7", "frontend_pid": 3, "server_pid": 4}],
    )
    assert discovery.discover_agents()[0]["agent_pid"] == 3


def test_selection_observer_failure_invalidates_without_raising():
    from agent_hq.codex_relay import ThreadSelection

    calls = []

    def selected(thread):
        calls.append(thread)
        if thread is not None:
            raise OSError("receipt disk unavailable")

    tracker = ThreadSelection(selected)
    tracker.client('{"method":[],"id":1}')
    tracker.client('{"method":"thread/start","id":2}')
    tracker.server('{"id":2,"result":{"thread":{"id":"root"}}}')
    assert calls[-1] is None


def test_receipt_oversized_timestamp_is_ignored(tmp_path):
    from agent_hq.codex_runtime import read_registrations

    directory = tmp_path / "r"
    directory.mkdir()
    receipt = {
        "version": 1,
        "wrapper_pid": 1,
        "frontend_pid": 2,
        "server_pid": 3,
        "generation": "g",
        "pane_id": "%1",
        "socket_path": str(directory / "server.sock"),
        "created_at": 10**1000,
    }
    (directory / "registration.json").write_text(json.dumps(receipt))
    assert read_registrations(tmp_path, alive=lambda _: True) == []


def test_process_births_are_batched_and_bounded():
    from agent_hq.process_identity import read_process_births

    calls = []

    def run(argv, **kw):
        from types import SimpleNamespace

        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout="12 00:30\n13 00:00\nmalformed\n")

    assert read_process_births(runner=run, now=100) == {12: 70, 13: 100}
    assert len(calls) == 1 and calls[0][1]["timeout"] <= 0.2


def test_wrapped_hook_tags_frontend_not_server(monkeypatch, tmp_path):
    from agent_hq import hooks

    tags = tmp_path / "tags.jsonl"
    monkeypatch.setattr(hooks, "CODEX_SESSION_TAGS_FILE", tags)
    monkeypatch.setenv("AGENT_HQ_CODEX_WRAPPED", "1")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(hooks, "_find_codex_ancestor_pid", lambda _: 99)
    monkeypatch.setattr(
        "agent_hq.codex_runtime.read_registrations",
        lambda **_: [{"pane_id": "%7", "thread_id": "root", "frontend_pid": 42}],
    )
    hooks._record_codex_session_tag({"session_id": "root", "cwd": "/tmp"})
    assert json.loads(tags.read_text())["pid"] == 42


@pytest.mark.parametrize("provenance", [
    {"ephemeral": True}, {"threadSource": "system"},
])
def test_tui_auxiliary_title_thread_never_steals_visible_selection(provenance):
    from agent_hq.codex_relay import ThreadSelection

    selected = []
    tracker = ThreadSelection(selected.append)
    tracker.client('{"id":1,"method":"thread/start","params":{}}')
    tracker.server('{"id":1,"result":{"thread":{"id":"visible"}}}')
    tracker.client(
        json.dumps(
            {
                "id": 2,
                "method": "thread/start",
                "params": provenance,
            }
        )
    )
    tracker.server('{"id":2,"result":{"thread":{"id":"title"}}}')
    tracker.client('{"id":3,"method":"thread/unsubscribe","params":{"threadId":"title"}}')
    tracker.server('{"id":3,"result":{"status":"unsubscribed"}}')
    assert selected == [{"id": "visible"}]


async def test_slow_process_check_preserves_frontend_but_not_verified_status(monkeypatch, tmp_path):
    import time
    from unittest.mock import AsyncMock

    from agent_hq import codex_runtime, discovery, process_identity
    from agent_hq.status_collector import RuntimeStatusCollector
    from agent_hq.status_types import LiveStatus

    directory = tmp_path / 'r'
    directory.mkdir()
    now = time.time()
    receipt = dict(version=1, generation='g', pane_id='%7', wrapper_pid=2,
                   frontend_pid=3, server_pid=4, thread_id='root', created_at=now-10,
                   frontend_created_at=now-9, socket_path=str(directory / 'server.sock'))
    (directory / 'registration.json').write_text(json.dumps(receipt))
    monkeypatch.setattr(codex_runtime, 'RUNTIME_DIR', tmp_path)
    monkeypatch.setattr(codex_runtime, '_alive', lambda pid: True)
    births = {}
    monkeypatch.setattr(process_identity, 'read_process_births', lambda: births)
    monkeypatch.setattr(discovery, 'get_all_panes', lambda: [dict(
        pane_pid=1, pane_id='%7', session_name='s', window_index=0, pane_index=0)])
    monkeypatch.setattr(discovery, 'build_process_tree', lambda: (
        {1: [2], 2: [3, 4]}, {1: 'zsh', 2: 'agent-hq', 3: 'codex', 4: 'codex'}))
    native = AsyncMock(return_value=LiveStatus('idle', 'idle', 'Idle', '', 'codex',
                                              'root', 3, now, time.monotonic_ns()))
    path = tmp_path / 'status.json'
    collector = RuntimeStatusCollector(path, codex_reader=native)
    assert discovery.discover_agents()[0]['agent_pid'] == 3
    await collector._codex()
    native.assert_not_called()
    assert json.loads(path.read_text())['observations'] == []
    births.update({2: now-10, 3: now-9, 4: now-10})
    await collector._codex()
    assert json.loads(path.read_text())['observations'][0]['status']['state'] == 'idle'
    # A process table that proves PID reuse still invalidates the live status.
    births[3] = now
    await collector._codex()
    assert json.loads(path.read_text())['observations'] == []


@pytest.mark.parametrize('args', [
    ['--profile', 'work'], ['--profile=work'], ['-p', 'work'], ['-pwork'],
    ['resume', 'thread-id', '--profile', 'work'],
])
def test_profiles_rejected_instead_of_silently_dropping_backend_settings(args):
    from agent_hq.codex_runtime import launch_commands

    with pytest.raises(ValueError, match='profile.*not supported'):
        launch_commands(args, '/tmp/up.sock', '/tmp/client.sock')


def test_profile_like_prompt_and_config_values_are_not_options():
    from agent_hq.codex_runtime import launch_commands

    for args in [['--', '--profile=work'], ['-c', 'developer_instructions="-pwork"']]:
        _, frontend = launch_commands(args, '/tmp/up.sock', '/tmp/client.sock')
        assert frontend[3:] == args


@pytest.mark.parametrize('args', [['--oss'], ['--oss', '--local-provider', 'ollama']])
def test_oss_rejected_instead_of_using_backend_default_provider(args):
    from agent_hq.codex_runtime import launch_commands

    with pytest.raises(ValueError, match='--oss is not supported'):
        launch_commands(args, '/tmp/up.sock', '/tmp/client.sock')


def test_compact_config_override_reaches_backend():
    from agent_hq.codex_runtime import launch_commands

    arg = '-cmodel_provider="ollama"'
    server, frontend = launch_commands([arg], '/tmp/up.sock', '/tmp/client.sock')
    assert server[-1] == arg
    assert frontend[-1] == arg


def test_write_receipt_does_not_follow_a_planted_temp_symlink(tmp_path):
    """The receipt's temp file must be created fresh (O_EXCL), never through
    a pre-existing ``registration.tmp`` symlink (#263 F2)."""
    import json

    from agent_hq.codex_runtime import _write_receipt

    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (tmp_path / "registration.tmp").symlink_to(victim)
    target = tmp_path / "registration.json"
    _write_receipt(target, {"version": 1})
    assert victim.read_text() == "keep me"
    assert json.loads(target.read_text()) == {"version": 1}
    assert target.stat().st_mode & 0o777 == 0o600
