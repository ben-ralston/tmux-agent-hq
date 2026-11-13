# tests/test_hygiene_cli.py
"""agent-hq hygiene run (#249)."""

from pathlib import Path

import pytest

from agent_hq import hygiene_cli
from agent_hq.cli import create_parser
from agent_hq.config import Config, ConfigError, ProjectConfig, RepoConfig, TriageConfig
from agent_hq.hygiene_store import HygieneStore
from agent_hq.models import Issue, Priority
from agent_hq.triage_store import TriageRecord, TriageStore

FIX = Path(__file__).parent / "fixtures" / "claude_p"
REPO = "ben-ralston/agent-hq"


def _cfg(tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return Config(
        repos={REPO: RepoConfig(REPO, tmp_path / "co")},
        projects=[ProjectConfig(name="HQ", number=1, owner="ben-ralston")],
        worktree_root=tmp_path / "wt",
        triage=TriageConfig(data_dir=tmp_path / "data"),
    )


def _issues():
    def mk(n, title, labels=()):
        return Issue(number=n, title=title, repo=REPO, status="Todo", priority=Priority.IDEA,
                     labels=list(labels), updated_at="2026-09-01T00:00:00Z")

    return [
        mk(1, "reading list A"),
        mk(2, "reading list B"),
        mk(3, "[sentinel] boom", ["sentinel"]),
        mk(4, "[sentinel] boom", ["sentinel"]),
        mk(5, "token leak"),
    ]


def _seed(tmp_path):
    TriageStore(tmp_path / "data").upsert(
        TriageRecord(issue_key=f"{REPO}#5", value=2, updated_at="t"), actor="human"
    )


def test_parser_accepts_hygiene_run_flags():
    args = create_parser().parse_args(["hygiene", "run", "--dry-run"])
    assert (args.subcommand, args.hygiene_action, args.dry_run) == ("hygiene", "run", True)


def test_bare_hygiene_requires_action():
    with pytest.raises(SystemExit) as exc:
        create_parser().parse_args(["hygiene"])
    assert exc.value.code == 2


def test_dry_run_prints_findings_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hygiene_cli, "load_config", lambda path=None: _cfg(tmp_path))
    monkeypatch.setattr(hygiene_cli, "_load_issues", lambda cfg, cache=None, force=False: _issues())
    _seed(tmp_path)
    rc = hygiene_cli.run_hygiene_cli(
        dry_run=True, runner=lambda *a: (FIX / "hygiene_ok.json").read_text()
    )
    out = capsys.readouterr().out
    assert rc == 0 and "DRY RUN" in out
    assert "duplicate" in out and f"{REPO}#2" in out and "Sentinel: recurring boom" in out
    assert "mismatch" in out and "2 → 5" in out
    assert HygieneStore(tmp_path / "data").all_findings() == []


def test_run_writes_findings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hygiene_cli, "load_config", lambda path=None: _cfg(tmp_path))
    monkeypatch.setattr(hygiene_cli, "_load_issues", lambda cfg, cache=None, force=False: _issues())
    _seed(tmp_path)
    rc = hygiene_cli.run_hygiene_cli(
        dry_run=False, runner=lambda *a: (FIX / "hygiene_ok.json").read_text()
    )
    assert rc == 0
    assert len(HygieneStore(tmp_path / "data").all_findings()) == 3
    assert "written 3" in capsys.readouterr().out


def test_returns_1_on_failed_sweep(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hygiene_cli, "load_config", lambda path=None: _cfg(tmp_path))
    monkeypatch.setattr(hygiene_cli, "_load_issues", lambda cfg, cache=None, force=False: _issues())
    rc = hygiene_cli.run_hygiene_cli(
        dry_run=False, runner=lambda *a: (FIX / "hygiene_malformed.json").read_text()
    )
    assert rc == 1 and "parse:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "loader",
    [
        lambda path=None: None,
        lambda path=None: (_ for _ in ()).throw(ConfigError("bad")),
    ],
)
def test_returns_1_when_unconfigured_or_invalid(tmp_path, monkeypatch, capsys, loader):
    monkeypatch.setattr(hygiene_cli, "load_config", loader)
    assert hygiene_cli.run_hygiene_cli(dry_run=True) == 1
    assert "hygiene" in capsys.readouterr().err


def test_returns_1_when_data_dir_missing(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    (tmp_path / "data").rmdir()
    monkeypatch.setattr(hygiene_cli, "load_config", lambda path=None: cfg)
    assert hygiene_cli.run_hygiene_cli(dry_run=True) == 1
    assert "data_dir" in capsys.readouterr().err


def test_strips_control_characters_from_model_reasoning(tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.setattr(hygiene_cli, "load_config", lambda path=None: _cfg(tmp_path))
    monkeypatch.setattr(hygiene_cli, "_load_issues", lambda cfg, cache=None, force=False: _issues())
    _seed(tmp_path)  # the fixture's mismatch needs #5 scored (rubric: delta ≥ 2)
    env = json.loads((FIX / "hygiene_ok.json").read_text())
    env["structured_output"]["duplicates"][0]["reasoning"] = "evil\x1b[2Jtext"
    rc = hygiene_cli.run_hygiene_cli(dry_run=True, runner=lambda *a: json.dumps(env))
    out = capsys.readouterr().out
    assert rc == 0 and "\x1b" not in out and "evil[2Jtext" in out


# ---- #273: off-board findings ----


def test_describe_add_to_board():
    from agent_hq.hygiene_store import Finding, FindingType

    f = Finding.new(
        FindingType.OFFBOARD, ["ben-ralston/gadget#8"],
        action={"kind": "add_to_board", "key": "ben-ralston/gadget#8", "project_number": 13,
                "project_owner": "@me", "project_name": "Gadget", "title": "misfiles"},
        reasoning="off",
    )
    assert hygiene_cli._describe(f) == "add ben-ralston/gadget#8 (misfiles) to the Gadget board"


def test_run_passes_off_board_inputs_to_the_sweep(tmp_path, monkeypatch):
    from agent_hq import hygiene_worker as hw
    from agent_hq.hygiene_worker import SweepReport

    monkeypatch.setattr(hygiene_cli, "load_config", lambda path=None: _cfg(tmp_path))
    monkeypatch.setattr(hygiene_cli, "_load_issues", lambda cfg, cache=None, force=False: _issues())
    inputs = hw.OffboardInputs(open_by_repo={REPO: {f"{REPO}#9"}})
    monkeypatch.setattr(hygiene_cli, "collect_offboard_inputs", lambda cfg, cache: inputs)
    seen = {}

    def fake_sweep(issues, ts, hs, tcfg, repos, **kw):
        seen.update(kw)
        return SweepReport()

    monkeypatch.setattr(hygiene_cli, "run_sweep", fake_sweep)
    assert hygiene_cli.run_hygiene_cli(dry_run=True) == 0
    assert seen["offboard"] is inputs
    assert seen["describe"] is hygiene_cli.fetch_issue_summary


def test_run_forces_the_board_load(tmp_path, monkeypatch):
    """Review round 6: a non-forced load can serve a board seconds from its
    TTL; by the time the off-board collector reads it the entry has lapsed
    and the repo is skipped. The CLI is a manual entry point — force."""
    from agent_hq.hygiene_worker import SweepReport

    monkeypatch.setattr(hygiene_cli, "load_config", lambda path=None: _cfg(tmp_path))
    seen = {}

    def fake_load(cfg, cache=None, force=False):
        seen["force"] = force
        return _issues()

    monkeypatch.setattr(hygiene_cli, "_load_issues", fake_load)
    monkeypatch.setattr(hygiene_cli, "run_sweep", lambda *a, **kw: SweepReport())
    assert hygiene_cli.run_hygiene_cli(dry_run=True) == 0
    assert seen["force"] is True
