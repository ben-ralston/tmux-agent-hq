"""Private /tmp/agent-hq runtime root (#263 F2/F4/F6).

``/tmp/agent-hq`` holds the daemon PID file, hook state (with the last
assistant message), the discovery cache and the codex runtime sockets. Any
local user can create it first after a reboot, so every writer goes through
``runtime_dir.ensure_dir``, which makes the root a 0700 directory owned by
this user and refuses one that is not.
"""

from __future__ import annotations

import os
import stat

import pytest

from agent_hq import runtime_dir
from agent_hq.runtime_dir import UnsafeRuntimeDirError, ensure_dir


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "agent-hq"
    monkeypatch.setattr(runtime_dir, "RUNTIME_ROOT", path)
    return path


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_creates_root_private(root):
    ensure_dir(root / "states")
    assert _mode(root) == 0o700
    assert (root / "states").is_dir()


def test_tightens_existing_permissive_root(root):
    root.mkdir(mode=0o777)
    os.chmod(root, 0o777)
    ensure_dir(root)
    assert _mode(root) == 0o700


def test_refuses_symlinked_root(root, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    root.symlink_to(target)
    with pytest.raises(UnsafeRuntimeDirError):
        ensure_dir(root / "states")
    assert not (target / "states").exists()


def test_refuses_root_owned_by_someone_else(root, monkeypatch):
    root.mkdir()
    real_uid = os.getuid()
    monkeypatch.setattr(runtime_dir.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(UnsafeRuntimeDirError):
        ensure_dir(root / "states")


def test_refusal_names_a_remedy_the_user_can_carry_out(root, monkeypatch):
    """/tmp is sticky: another user's directory cannot be removed without
    root, so the message must not just say 'remove it' (#263 review)."""
    root.mkdir()
    real_uid = os.getuid()
    monkeypatch.setattr(runtime_dir.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(UnsafeRuntimeDirError) as exc:
        ensure_dir(root / "states")
    message = str(exc.value)
    assert "sudo rm -rf" in message
    assert "reboot" in message


def test_unsafe_root_is_an_oserror():
    """Callers that already degrade on OSError (best-effort locks, cache
    writers) must degrade on a hostile root too, not crash."""
    assert issubclass(UnsafeRuntimeDirError, OSError)


def test_paths_outside_root_are_plain_mkdir(root, tmp_path):
    other = tmp_path / "other" / "dir"
    ensure_dir(other)
    assert other.is_dir()
    assert not root.exists()


# --- wiring: every /tmp/agent-hq writer secures the root ---------------------


@pytest.fixture
def loose_root(monkeypatch):
    """The conftest-isolated runtime root, pre-created world-writable."""
    from agent_hq import cache

    path = cache.CACHE_FILE.parent
    monkeypatch.setattr(runtime_dir, "RUNTIME_ROOT", path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o777)
    return path


def test_cache_write_secures_root(loose_root):
    from agent_hq import cache

    cache.write_cache([])
    assert _mode(loose_root) == 0o700


def test_pid_write_secures_root(loose_root):
    from agent_hq import daemon

    daemon.write_pid(123)
    assert _mode(loose_root) == 0o700


def test_file_lock_secures_root(loose_root):
    from agent_hq._locking import file_lock

    with file_lock(loose_root / "x.lock"):
        pass
    assert _mode(loose_root) == 0o700


def test_overrides_write_secures_root(loose_root):
    from agent_hq import overrides

    overrides.write_override("%1", {"phase": "implement"}, session_id=None)
    assert _mode(loose_root) == 0o700


# --- readers: refuse data from a root that is not ours alone ------------------


def test_check_trusted_accepts_a_private_root(root):
    from agent_hq.runtime_dir import check_trusted

    ensure_dir(root)
    check_trusted(root / "cache.json")  # no raise


def test_check_trusted_rejects_group_or_world_writable_root(root):
    from agent_hq.runtime_dir import check_trusted

    root.mkdir()
    os.chmod(root, 0o777)
    with pytest.raises(UnsafeRuntimeDirError):
        check_trusted(root / "cache.json")


def test_untrusted_root_reads_as_missing():
    """Existing 'file missing' handlers must swallow it."""
    assert issubclass(UnsafeRuntimeDirError, FileNotFoundError)


@pytest.fixture
def planted_root(loose_root):
    """A world-writable root holding plausible files another user planted."""
    import json

    from agent_hq import cache

    cache.write_cache([])
    (loose_root / "daemon.pid").write_text(f"{os.getpid()}\n")
    (loose_root / "overrides.json").write_text(
        json.dumps({"revision": 3, "entries": {"%1": {"phase": "done"}}})
    )
    states = loose_root / "states"
    states.mkdir(exist_ok=True)
    (states / "5.json").write_text(json.dumps({"latest_summary": "planted"}))
    os.chmod(loose_root, 0o777)
    return loose_root


def test_read_pid_ignores_planted_pid(planted_root, monkeypatch):
    from agent_hq import daemon

    monkeypatch.setattr(daemon, "PID_FILE", planted_root / "daemon.pid")
    assert daemon.read_pid() is None


def test_read_cache_ignores_planted_cache(planted_root):
    from agent_hq import cache

    os.chmod(planted_root, 0o700)
    assert cache.read_cache() is not None  # sanity: readable when private
    os.chmod(planted_root, 0o777)
    assert cache.read_cache() is None


def test_watcher_cache_read_ignores_planted_cache(planted_root, monkeypatch):
    from agent_hq.web import watcher

    seen = []
    monkeypatch.setattr(watcher.json, "loads", lambda raw: seen.append(raw) or {})
    watcher._load_agents_defensively(max_age=None, cache_file=planted_root / "cache.json")
    assert seen == []


def test_read_overrides_ignores_planted_overrides(planted_root, monkeypatch):
    from agent_hq import overrides

    monkeypatch.setattr(overrides, "OVERRIDES_FILE", planted_root / "overrides.json")
    assert overrides.read_overrides() == ({}, 0)


def test_hook_state_read_ignores_planted_state(planted_root):
    from agent_hq.hooks import _read_state_file

    assert _read_state_file(planted_root / "states" / "5.json") == {}
