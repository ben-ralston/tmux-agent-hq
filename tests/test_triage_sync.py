"""Tests for agent_hq.triage_sync — uses real local git repos, no network."""

import os
import subprocess
import time

import pytest

from agent_hq.config import TriageConfig
from agent_hq.triage_sync import TriageSyncer


@pytest.fixture
def data_repo(tmp_path):
    """A data repo with a local bare 'origin' so push works offline."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    repo = tmp_path / "data"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    for args in (
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["config", "commit.gpgsign", "false"],
        ["config", "core.hooksPath", "/dev/null"],
    ):
        subprocess.run(["git", "-C", str(repo)] + args, check=True, capture_output=True)
    (repo / "triage").mkdir()
    (repo / "triage" / "seed.json").write_text("{}")
    # Steady state: the syncer self-seeds this on first contact; pre-seed it
    # so clean-repo tests are actually clean. Reseeding has its own test.
    (repo / ".gitignore").write_text(
        "# Runtime sidecars from agent_hq stores — never data\n*.lock\n*.tmp\n"
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(repo), "push"], check=True, capture_output=True)
    return repo


def _syncer(repo, debounce=0):
    cfg = TriageConfig(data_dir=repo, sync_debounce_s=debounce)
    return TriageSyncer(cfg, check_interval_s=0)


def _age(path):
    old = time.time() - 120
    os.utime(path, (old, old))


def _log_count(repo):
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


def test_clean_repo_no_commit(data_repo):
    assert _syncer(data_repo).tick() is False
    assert _log_count(data_repo) == 1


def test_dirty_quiescent_repo_commits_and_pushes(data_repo):
    (data_repo / "triage" / "agent-hq.json").write_text("{}")
    _age(data_repo / "triage" / "agent-hq.json")
    assert _syncer(data_repo, debounce=60).tick() is True
    assert _log_count(data_repo) == 2
    status = subprocess.run(
        ["git", "-C", str(data_repo), "status", "--porcelain", "-b"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "ahead" not in status  # pushed


def test_recent_write_debounces(data_repo):
    (data_repo / "triage" / "agent-hq.json").write_text("{}")  # fresh mtime
    assert _syncer(data_repo, debounce=60).tick() is False
    assert _log_count(data_repo) == 1


def test_check_interval_throttles(data_repo):
    cfg = TriageConfig(data_dir=data_repo, sync_debounce_s=0)
    syncer = TriageSyncer(cfg, check_interval_s=1000)
    (data_repo / "triage" / "agent-hq.json").write_text("{}")
    _age(data_repo / "triage" / "agent-hq.json")
    assert syncer.tick() is True  # first tick checks
    (data_repo / "triage" / "b.json").write_text("{}")
    _age(data_repo / "triage" / "b.json")
    assert syncer.tick() is False  # throttled, no second check yet


def test_sync_disabled_does_nothing(data_repo):
    cfg = TriageConfig(data_dir=data_repo, sync_debounce_s=0, sync_enabled=False)
    (data_repo / "triage" / "agent-hq.json").write_text("{}")
    _age(data_repo / "triage" / "agent-hq.json")
    assert TriageSyncer(cfg, check_interval_s=0).tick() is False
    assert _log_count(data_repo) == 1


def test_missing_repo_never_raises(tmp_path):
    cfg = TriageConfig(data_dir=tmp_path / "nope")
    assert TriageSyncer(cfg, check_interval_s=0).tick() is False


def test_push_failure_keeps_commit_and_returns_true(data_repo):
    subprocess.run(
        ["git", "-C", str(data_repo), "remote", "set-url", "origin", "/nonexistent"],
        check=True,
        capture_output=True,
    )
    (data_repo / "triage" / "agent-hq.json").write_text("{}")
    _age(data_repo / "triage" / "agent-hq.json")
    assert _syncer(data_repo, debounce=60).tick() is True
    assert _log_count(data_repo) == 2  # commit stands; push retried on later ticks


def test_build_syncer_from_config(tmp_path):
    from agent_hq.config import Config
    from agent_hq.daemon import _build_triage_syncer

    assert _build_triage_syncer(None) is None  # no config file
    bare = Config(repos={}, projects=[], worktree_root=tmp_path)
    assert _build_triage_syncer(bare) is None  # no triage section
    cfg = Config(
        repos={},
        projects=[],
        worktree_root=tmp_path,
        triage=TriageConfig(data_dir=tmp_path / "data"),
    )
    assert _build_triage_syncer(cfg) is not None


def test_clean_tree_never_pushes(data_repo, monkeypatch):
    # A configured-but-idle repo must not fire network pushes every check.
    calls = []
    orig = TriageSyncer._git

    def spy(self, *args):
        calls.append(args)
        return orig(self, *args)

    monkeypatch.setattr(TriageSyncer, "_git", spy)
    assert _syncer(data_repo).tick() is False
    assert not any(a[0] == "push" for a in calls), calls


def test_push_skipped_when_not_ahead_even_with_broken_remote(data_repo):
    subprocess.run(
        ["git", "-C", str(data_repo), "remote", "set-url", "origin", "/nonexistent"],
        check=True,
        capture_output=True,
    )
    # clean tree, not ahead -> no push attempt -> no error path exercised
    assert _syncer(data_repo).tick() is False


def test_lock_and_tmp_sidecars_never_committed(data_repo):
    # The syncer must self-seed a .gitignore so store sidecars stay untracked.
    (data_repo / "triage" / "agent-hq.json").write_text("{}")
    (data_repo / "triage" / "agent-hq.json.lock").write_text("")
    (data_repo / "triage" / "orphan.json.tmp").write_text("{")
    for f in (data_repo / "triage").iterdir():
        _age(f)
    assert _syncer(data_repo, debounce=60).tick() is True
    tracked = subprocess.run(
        ["git", "-C", str(data_repo), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "agent-hq.json" in tracked
    assert ".lock" not in tracked
    assert ".tmp" not in tracked
    assert ".gitignore" in tracked


def test_gitignore_reseeded_when_missing(data_repo):
    subprocess.run(
        ["git", "-C", str(data_repo), "rm", "-q", ".gitignore"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(data_repo), "commit", "-q", "-m", "drop gitignore"],
        check=True,
        capture_output=True,
    )
    assert _syncer(data_repo).tick() is True  # reseeds + commits it
    assert (data_repo / ".gitignore").exists()
    tracked = subprocess.run(
        ["git", "-C", str(data_repo), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert ".gitignore" in tracked


def test_eventlog_failures_emit_warn_not_error(data_repo, monkeypatch):
    # Push failures are degraded-not-broke (self-healing retry): repo
    # convention says level "warn" so the sentinel doesn't auto-file issues.
    from agent_hq import triage_sync

    emits = []
    monkeypatch.setattr(
        triage_sync.eventlog, "emit", lambda level, msg, *a, **k: emits.append((level, msg))
    )
    subprocess.run(
        ["git", "-C", str(data_repo), "remote", "set-url", "origin", "/nonexistent"],
        check=True,
        capture_output=True,
    )
    (data_repo / "triage" / "agent-hq.json").write_text("{}")
    _age(data_repo / "triage" / "agent-hq.json")
    assert _syncer(data_repo, debounce=60).tick() is True
    assert emits, "push failure should emit"
    assert all(level == "warn" for level, _ in emits), emits


def test_existing_gitignore_gains_sidecar_patterns(data_repo):
    # A hand-made data repo with its own .gitignore must still get the
    # sidecar exclusions appended (preserving what's already there).
    (data_repo / ".gitignore").write_text("custom-junk/\n")
    subprocess.run(
        ["git", "-C", str(data_repo), "commit", "-aqm", "custom gitignore"],
        check=True,
        capture_output=True,
    )
    (data_repo / "triage" / "agent-hq.json").write_text("{}")
    (data_repo / "triage" / "agent-hq.json.lock").write_text("")
    for f in (data_repo / "triage").iterdir():
        _age(f)
    assert _syncer(data_repo, debounce=60).tick() is True
    gi = (data_repo / ".gitignore").read_text()
    assert "custom-junk/" in gi  # existing content preserved
    assert "*.lock" in gi and "*.tmp" in gi
    tracked = subprocess.run(
        ["git", "-C", str(data_repo), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert ".lock" not in tracked


def test_missing_data_dir_writes_nothing_and_no_stray_gitignore(tmp_path):
    target = tmp_path / "not-a-repo"
    target.mkdir()  # exists but is NOT a git repo
    cfg = TriageConfig(data_dir=target)
    assert TriageSyncer(cfg, check_interval_s=0).tick() is False
    assert not (target / ".gitignore").exists()  # no writes before the repo check
