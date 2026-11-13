"""Own a private Codex backend and its ordinary terminal frontend."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import signal
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path

from websockets.asyncio.server import unix_serve

from .codex_relay import relay
from .runtime_dir import ensure_dir

RUNTIME_DIR = Path("/tmp/agent-hq/codex-runtimes")


def launch_commands(argv: list[str], server_socket: str, client_socket: str):
    """Pass frontend arguments intact; core config must also reach the backend."""
    server = ["codex", "app-server", "--listen", f"unix://{server_socket}"]
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            break
        if arg == "--profile" or arg.startswith(("--profile=", "-p")):
            raise ValueError(
                "named profiles are not supported by agent-hq codex: the native remote "
                "backend cannot load the complete profile; use explicit -c overrides "
                "or launch bare codex (status will be Unknown)"
            )
        if arg == "--oss":
            raise ValueError(
                "--oss is not supported by agent-hq codex: the native remote frontend "
                "does not forward its provider selection; configure model_provider "
                "with -c or launch bare codex (status will be Unknown)"
            )
        if arg == "--remote" or arg.startswith("--remote="):
            raise ValueError("agent-hq codex owns the remote endpoint")
        if arg in {"-c", "--config", "--enable", "--disable"}:
            if index + 1 == len(argv):
                raise ValueError(f"missing value for {arg}")
            server.extend(argv[index : index + 2])
            index += 2
            continue
        if arg.startswith(("-c", "--config=", "--enable=", "--disable=")):
            server.append(arg)
        index += 1
    return server, ["codex", "--remote", f"unix://{client_socket}", *argv]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def read_registrations(root: Path | None = None, *, alive=None) -> list[dict]:
    """Read launch receipts, ignoring exited owners and malformed records."""
    check_alive = alive or _alive
    from .process_identity import read_process_births

    births = read_process_births() if alive is None else {}
    result = []
    for path in (root or RUNTIME_DIR).glob("*/registration.json"):
        try:
            if path.is_symlink() or path.stat().st_uid != os.getuid():
                continue
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or data.get("version") != 1:
                continue
            if not all(
                type(data.get(k)) is int and data[k] > 0 and check_alive(data[k])
                for k in ("wrapper_pid", "frontend_pid", "server_pid")
            ):
                continue
            if not all(
                isinstance(data.get(k), str) and data[k]
                for k in ("generation", "pane_id", "socket_path")
            ):
                continue
            if data["socket_path"] != str(path.parent / "server.sock"):
                continue
            created = data.get("created_at")
            if (
                type(created) not in (int, float)
                or not math.isfinite(created)
                or created < 0
                or created > time.time()
            ):
                continue
            if alive is None:
                # A receipt cannot outlive its owner and then claim a reused PID.
                deadline = data.get("frontend_created_at", created)
                if type(deadline) not in (int, float) or not math.isfinite(deadline):
                    continue
                if any(
                    births.get(data[k], float("inf")) > deadline + 1
                    for k in ("wrapper_pid", "frontend_pid", "server_pid")
                ):
                    continue
            result.append(data)
        except (OSError, ValueError, TypeError, OverflowError):
            continue
    return result


async def _stop(child) -> None:
    if child.returncode is not None:
        return
    try:
        child.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(child.wait(), timeout=3)
    except TimeoutError:
        child.kill()
        await child.wait()


async def supervise(backend, frontend) -> int:
    """Neither half of this session may survive the other."""
    server_wait = asyncio.create_task(backend.wait())
    client_wait = asyncio.create_task(frontend.wait())
    try:
        done, _ = await asyncio.wait(
            (server_wait, client_wait), return_when=asyncio.FIRST_COMPLETED
        )
        if client_wait in done:
            return client_wait.result()
        return server_wait.result() or 1
    finally:
        await _stop(frontend)
        await _stop(backend)
        await asyncio.gather(server_wait, client_wait, return_exceptions=True)


def _write_receipt(path: Path, data: dict) -> None:
    # mkstemp: a fresh 0600 file opened O_EXCL, never a fixed name that a
    # planted symlink could redirect (#263 F2).
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def run_codex(argv: list[str]) -> int:
    pane_id = os.environ.get("TMUX_PANE")
    if not pane_id:
        raise ValueError("agent-hq codex must run in a tmux pane")
    ensure_dir(RUNTIME_DIR.parent)
    RUNTIME_DIR.mkdir(exist_ok=True, mode=0o700)
    info = RUNTIME_DIR.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Codex runtime directory must be a directory owned by this user")
    RUNTIME_DIR.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="r-", dir=RUNTIME_DIR) as temp:
        directory = Path(temp)
        upstream = str(directory / "server.sock")
        downstream = str(directory / "client.sock")
        server_argv, frontend_argv = launch_commands(argv, upstream, downstream)
        env = dict(os.environ, AGENT_HQ_CODEX_WRAPPED="1")
        backend = frontend = None
        tags = asyncio.Queue()

        async def tag_selections():
            from .hooks import _record_codex_session_tag

            while True:
                session, pid = await tags.get()
                await asyncio.to_thread(
                    _record_codex_session_tag,
                    {"session_id": session, "cwd": os.getcwd()},
                    frontend_pid=pid,
                )

        tag_task = asyncio.create_task(tag_selections())
        receipt = {
            "version": 1,
            "generation": uuid.uuid4().hex,
            "pane_id": pane_id,
            "wrapper_pid": os.getpid(),
            "created_at": time.time(),
            "socket_path": upstream,
            "thread_id": None,
            "transcript_path": None,
        }

        def selected(thread):
            receipt["thread_id"] = thread.get("id") if thread else None
            receipt["transcript_path"] = thread.get("path") if thread else None
            if "frontend_pid" in receipt:
                path = directory / "registration.json"
                try:
                    _write_receipt(path, receipt)
                except OSError:
                    path.unlink(missing_ok=True)
                    raise
                if thread:
                    tags.put_nowait((thread["id"], receipt["frontend_pid"]))

        connected = False

        async def proxy(ws):
            nonlocal connected
            if connected:
                await ws.close(code=1008, reason="one frontend per pane")
                return
            connected = True
            try:
                await relay(ws, upstream, selected)
            finally:
                connected = False

        with (directory / "server.log").open("wb") as log:
            try:
                backend = await asyncio.create_subprocess_exec(
                    *server_argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=env,
                    start_new_session=True,
                )
                receipt["server_pid"] = backend.pid
                deadline = time.monotonic() + 15
                while not Path(upstream).exists():
                    if backend.returncode is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Codex app server failed to become ready")
                    await asyncio.sleep(0.05)
                async with unix_serve(proxy, downstream, max_size=None):
                    frontend = await asyncio.create_subprocess_exec(*frontend_argv, env=env)
                    receipt["frontend_pid"] = frontend.pid
                    receipt["frontend_created_at"] = time.time()
                    _write_receipt(directory / "registration.json", receipt)
                    return await supervise(backend, frontend)
            finally:
                if frontend is not None:
                    await _stop(frontend)
                if backend is not None:
                    await _stop(backend)
                tag_task.cancel()
                await asyncio.gather(tag_task, return_exceptions=True)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--":
        argv = argv[1:]
    # The TUI receives terminal SIGINT too. Do not tear down its backend when
    # the user merely interrupts a turn. SIGTERM still ends the owned session.
    # A Python handler resets to SIG_DFL on exec; SIG_IGN would make the
    # frontend inherit ignored Ctrl-C and break ordinary turn interruption.
    previous = signal.signal(signal.SIGINT, lambda *_: None)
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_hup = signal.getsignal(signal.SIGHUP)

    async def owned_runtime():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        loop.add_signal_handler(signal.SIGHUP, task.cancel)
        try:
            return await run_codex(argv)
        except asyncio.CancelledError:
            return 143
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            loop.remove_signal_handler(signal.SIGHUP)

    try:
        return asyncio.run(owned_runtime())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"agent-hq codex: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGHUP, previous_hup)
