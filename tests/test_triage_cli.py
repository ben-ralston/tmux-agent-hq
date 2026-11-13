"""agent-hq triage run (#248)."""

import json
from pathlib import Path

import pytest

from agent_hq import triage_cli
from agent_hq.cli import create_parser
from agent_hq.config import Config, ConfigError, ProjectConfig, RepoConfig, TriageConfig
from agent_hq.models import Issue, Priority
from agent_hq.triage_store import TriageState, TriageStore

FIX = Path(__file__).parent / "fixtures" / "claude_p"
REPO = "ben-ralston/agent-hq"


def _cfg(tmp_path):
    # The data dir must exist: the CLI refuses to run against a missing triage
    # data repo rather than letting the store silently create one.
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return Config(
        repos={REPO: RepoConfig(REPO, tmp_path / "co")},
        projects=[ProjectConfig(name="HQ", number=1, owner="ben-ralston")],
        worktree_root=tmp_path / "wt",
        triage=TriageConfig(data_dir=tmp_path / "data"),
    )


def _issues():
    return [
        Issue(number=67, title="dispatch view", repo=REPO, status="Todo", priority=Priority.NEED),
        Issue(number=40, title="filter bar", repo=REPO, status="Todo", priority=Priority.WANT),
    ]


def test_parser_accepts_triage_run_flags():
    args = create_parser().parse_args(["triage", "run", "--dry-run", "--repo", REPO, "--cap", "3"])
    assert (args.subcommand, args.triage_action, args.dry_run, args.repo, args.cap) == (
        "triage",
        "run",
        True,
        REPO,
        3,
    )


def test_bare_triage_requires_action():
    with pytest.raises(SystemExit) as exc:
        create_parser().parse_args(["triage"])
    assert exc.value.code == 2


def test_run_dry_run_prints_proposals_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(triage_cli, "load_config", lambda path=None: _cfg(tmp_path))
    monkeypatch.setattr(triage_cli, "_load_issues", lambda cfg: _issues())
    rc = triage_cli.run_triage_cli(
        dry_run=True,
        repo=None,
        cap=None,
        runner=lambda *a: (FIX / "triage_ok.json").read_text(),
    )
    out = capsys.readouterr().out
    assert rc == 0 and "#67" in out and "V4" in out and "pair" in out and "dry run" in out.lower()
    assert TriageStore(tmp_path / "data").all_records() == []


def test_run_writes_and_seeds(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(triage_cli, "load_config", lambda path=None: _cfg(tmp_path))
    monkeypatch.setattr(triage_cli, "_load_issues", lambda cfg: _issues())
    rc = triage_cli.run_triage_cli(
        dry_run=False,
        repo=None,
        cap=None,
        runner=lambda *a: (FIX / "triage_ok.json").read_text(),
    )
    assert rc == 0
    store = TriageStore(tmp_path / "data")
    assert store.get(f"{REPO}#67").state is TriageState.PROPOSED
    assert store.get(f"{REPO}#67").priority_hint == "Need"  # seeding ran first
    assert "proposed 2" in capsys.readouterr().out


def test_run_returns_1_on_failure_and_when_unconfigured(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(triage_cli, "load_config", lambda path=None: None)
    assert triage_cli.run_triage_cli(dry_run=True, repo=None, cap=None) == 1
    assert "triage" in capsys.readouterr().err
    monkeypatch.setattr(triage_cli, "load_config", lambda path=None: _cfg(tmp_path))
    monkeypatch.setattr(triage_cli, "_load_issues", lambda cfg: _issues())
    rc = triage_cli.run_triage_cli(
        dry_run=False,
        repo=None,
        cap=None,
        runner=lambda *a: (FIX / "triage_malformed.json").read_text(),
    )
    assert rc == 1


class _StubCache:
    """Two project boards that share an issue and include a Done one."""

    def __init__(self, boards):
        self.boards = boards

    def get_issues(self, number, owner, force=False):
        return list(self.boards[number])

    def get_closed_keys(self, repos):
        return set()


def test_load_issues_drops_done_and_dedups_like_the_web(tmp_path, monkeypatch, capsys):
    """Final-review finding 2: the CLI must apply the web's flatten rule, or
    it proposes for Done-but-open issues and revives records the web pass
    tombstoned (triage-repo sync churn)."""
    dup = Issue(number=67, title="dispatch view", repo=REPO, status="Todo", priority=Priority.NEED)
    done = Issue(number=99, title="shipped", repo=REPO, status="Done", priority=Priority.WANT)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    cfg = Config(
        repos={REPO: RepoConfig(REPO, tmp_path / "co")},
        projects=[
            ProjectConfig(name="HQ", number=1, owner="ben-ralston"),
            ProjectConfig(name="Life", number=2, owner="ben-ralston"),
        ],
        worktree_root=tmp_path / "wt",
        triage=TriageConfig(data_dir=tmp_path / "data"),
    )
    monkeypatch.setattr(triage_cli, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(
        triage_cli,
        "GitHubCache",
        lambda *a, **kw: _StubCache({1: [dup, done], 2: [dup, _issues()[1]]}),
    )
    rc = triage_cli.run_triage_cli(
        dry_run=True,
        repo=None,
        cap=None,
        runner=lambda *a: (FIX / "triage_ok.json").read_text(),
    )
    # #67 counted once, #99 (Done) dropped, #40 kept -> 2 targets, not 4.
    assert rc == 0 and "targeted 2" in capsys.readouterr().out


def test_run_strips_control_characters_from_model_reasoning(tmp_path, monkeypatch, capsys):
    """Model-sourced text reaches a terminal — an ANSI escape in `reasoning`
    must not be able to repaint the operator's screen."""
    envelope = json.loads((FIX / "triage_ok.json").read_text())
    envelope["structured_output"]["proposals"][0]["reasoning"]["value"] = (
        "\x1b[31mred\x1b[0m and a \x07bell"
    )
    monkeypatch.setattr(triage_cli, "load_config", lambda path=None: _cfg(tmp_path))
    monkeypatch.setattr(triage_cli, "_load_issues", lambda cfg: _issues())
    rc = triage_cli.run_triage_cli(
        dry_run=True, repo=None, cap=None, runner=lambda *a: json.dumps(envelope)
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "\x1b" not in out and "\x07" not in out
    assert "red" in out and "and a bell" in out  # spaces survive the strip


def test_run_returns_1_when_seeding_fails(tmp_path, monkeypatch, capsys):
    """Review fix 3: a read-only / unwritable data repo must be reported, not
    silently skipped — the run would otherwise propose against an un-seeded
    store and look like it worked."""

    def _raise(*a, **kw):
        raise PermissionError("ro")

    monkeypatch.setattr(triage_cli, "load_config", lambda path=None: _cfg(tmp_path))
    monkeypatch.setattr(triage_cli, "_load_issues", lambda cfg: _issues())
    monkeypatch.setattr(triage_cli, "ensure_triage_records", _raise)
    rc = triage_cli.run_triage_cli(
        dry_run=False,
        repo=None,
        cap=None,
        runner=lambda *a: (FIX / "triage_ok.json").read_text(),
    )
    assert rc == 1
    assert "seeding failed" in capsys.readouterr().err


def test_run_returns_1_when_data_dir_missing(tmp_path, monkeypatch, capsys):
    """Review fix 4: a data_dir that doesn't exist means the triage data repo
    was never cloned. Creating it silently would start a second, empty store."""
    cfg = Config(
        repos={REPO: RepoConfig(REPO, tmp_path / "co")},
        projects=[ProjectConfig(name="HQ", number=1, owner="ben-ralston")],
        worktree_root=tmp_path / "wt",
        triage=TriageConfig(data_dir=tmp_path / "nope"),
    )
    monkeypatch.setattr(triage_cli, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(triage_cli, "_load_issues", lambda cfg: _issues())
    for dry_run in (True, False):
        rc = triage_cli.run_triage_cli(dry_run=dry_run, repo=None, cap=None)
        assert rc == 1
        err = capsys.readouterr().err
        assert "data_dir does not exist" in err and str(tmp_path / "nope") in err
    assert not (tmp_path / "nope").exists()


def test_run_returns_1_on_invalid_config(monkeypatch, capsys):
    def _raise(path=None):
        raise ConfigError("bad yaml")

    monkeypatch.setattr(triage_cli, "load_config", _raise)
    rc = triage_cli.run_triage_cli(dry_run=True, repo=None, cap=None)
    assert rc == 1
    assert "invalid config" in capsys.readouterr().err
