"""Tests for the gh call meter (#255)."""

import json
import subprocess
import time

import pytest

from agent_hq import gh_meter


def test_classify_api_projects_and_issues_are_graphql():
    assert gh_meter.classify_api(["gh", "project", "item-list", "4"]) == "graphql"
    assert gh_meter.classify_api(["gh", "issue", "list", "--repo", "o/r"]) == "graphql"


def test_classify_api_rest_endpoint():
    assert gh_meter.classify_api(["gh", "api", "repos/o/r/issues?state=all"]) == "rest"
    assert gh_meter.classify_api(["gh", "api", "rate_limit"]) == "rest"


def test_classify_api_graphql_endpoint_is_graphql():
    """`gh api graphql` spends the GraphQL bucket despite being `gh api`."""
    assert gh_meter.classify_api(["gh", "api", "graphql", "-f", "query=x"]) == "graphql"


def test_run_gh_records_a_successful_call(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="{}", stderr=""),
    )
    gh_meter.run_gh(["gh", "api", "rate_limit"], kind="rate-limit")
    summary = gh_meter.summarize(hours=1)
    assert summary.total == 1
    assert summary.by_api == {"rest": 1}
    assert summary.by_kind == {"rate-limit": 1}


def test_run_gh_returns_the_completed_process(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout='{"ok": true}', stderr=""),
    )
    result = gh_meter.run_gh(["gh", "api", "rate_limit"], kind="rate-limit")
    assert json.loads(result.stdout) == {"ok": True}


def test_run_gh_enforces_check_and_timeout(monkeypatch):
    """The wrapper owns check=True and the timeout so no caller can forget."""
    seen = {}

    def _capture(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", _capture)
    gh_meter.run_gh(["gh", "api", "rate_limit"], kind="rate-limit")
    assert seen["check"] is True
    assert seen["timeout"] == gh_meter.GH_TIMEOUT
    assert seen["capture_output"] is True
    assert seen["text"] is True


def test_run_gh_records_a_failed_call(monkeypatch):
    """A non-zero gh exit still spent the API call — count it."""

    def _boom(*a, **k):
        raise subprocess.CalledProcessError(1, a[0], stderr="rate limit")

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(subprocess.CalledProcessError):
        gh_meter.run_gh(["gh", "project", "item-list", "4"], kind="project-item-list")
    assert gh_meter.summarize(hours=1).total == 1


def test_run_gh_records_a_timeout(monkeypatch):
    def _slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], gh_meter.GH_TIMEOUT)

    monkeypatch.setattr(subprocess, "run", _slow)
    with pytest.raises(subprocess.TimeoutExpired):
        gh_meter.run_gh(["gh", "project", "item-list", "4"], kind="project-item-list")
    assert gh_meter.summarize(hours=1).total == 1


def test_run_gh_does_not_record_when_gh_is_missing(monkeypatch):
    """No gh binary means no API call was spent."""

    def _missing(*a, **k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", _missing)
    with pytest.raises(FileNotFoundError):
        gh_meter.run_gh(["gh", "api", "rate_limit"], kind="rate-limit")
    assert gh_meter.summarize(hours=1).total == 0


def test_summarize_excludes_rows_outside_the_window():
    stale = time.time() - 7200
    gh_meter._DEFAULT_COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    gh_meter._DEFAULT_COUNTER_FILE.write_text(
        json.dumps({"t": stale, "kind": "old", "api": "graphql"}) + "\n"
    )
    assert gh_meter.summarize(hours=1).total == 0
    assert gh_meter.summarize(hours=3).total == 1


def test_summarize_reports_per_hour():
    for _ in range(6):
        gh_meter.record("k", "graphql")
    assert gh_meter.summarize(hours=2).per_hour == 3


def test_counter_file_is_capped(monkeypatch):
    """The cap is amortized, not exact.

    Trimming rewrites the whole file, so it runs every ``_TRIM_EVERY``
    appends rather than on each one — the file may legitimately sit up to
    ``_TRIM_EVERY - 1`` rows over the cap between trims. Asserting an exact
    cap would be asserting a per-append rewrite we deliberately don't do.
    """
    monkeypatch.setattr(gh_meter, "_MAX_RECORDS", 10)
    monkeypatch.setattr(gh_meter, "_TRIM_EVERY", 5)
    for _ in range(40):
        gh_meter.record("k", "rest")
    lines = gh_meter._DEFAULT_COUNTER_FILE.read_text().splitlines()
    assert len(lines) <= 10 + 5
    # ...and it really is bounded, not just growing slower than the loop.
    assert len(lines) < 40


def test_trim_keeps_the_newest_rows(monkeypatch):
    monkeypatch.setattr(gh_meter, "_MAX_RECORDS", 5)
    monkeypatch.setattr(gh_meter, "_TRIM_EVERY", 1)
    for i in range(20):
        gh_meter.record(f"kind-{i}", "rest")
    kinds = gh_meter.summarize(hours=1).by_kind
    assert "kind-19" in kinds
    assert "kind-0" not in kinds


def test_summarize_tolerates_malformed_lines():
    gh_meter._DEFAULT_COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    gh_meter._DEFAULT_COUNTER_FILE.write_text(
        "not json\n" + json.dumps({"t": time.time(), "kind": "k", "api": "rest"}) + "\n"
    )
    assert gh_meter.summarize(hours=1).total == 1


def test_summarize_on_a_missing_file_is_zero():
    gh_meter.clear_counter()
    summary = gh_meter.summarize(hours=1)
    assert summary.total == 0
    assert summary.by_api == {}


def test_record_never_raises_when_the_path_is_unwritable(monkeypatch, tmp_path):
    """Metering is diagnostics — it must never break the fetch it measures."""
    blocked = tmp_path / "nope"
    blocked.write_text("i am a file, not a directory")
    monkeypatch.setattr(gh_meter, "_DEFAULT_COUNTER_FILE", blocked / "calls.jsonl")
    gh_meter.record("k", "rest")  # must not raise


def test_github_module_has_no_unmetered_gh_calls():
    """`run_gh` is the single chokepoint for every gh invocation (#255).

    Asserted structurally rather than per-function: the value of a chokepoint
    is that nothing bypasses it, and a behavioural test only covers the call
    sites someone remembered to write a test for. A new
    `subprocess.run(["gh", ...])` in github.py is invisible to `gh-stats` and
    to the budget gate, so it should fail here the moment it is added.
    """
    import ast
    import pathlib

    source = pathlib.Path("agent_hq/github.py").read_text()
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "run"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
            continue
        if not node.args:
            continue
        argv = node.args[0]
        first = None
        if isinstance(argv, ast.List) and argv.elts:
            head = argv.elts[0]
            if isinstance(head, ast.Constant):
                first = head.value
        elif isinstance(argv, ast.Name):
            # A `cmd = [...]` variable — resolve it below.
            first = _resolve_cmd_head(source, argv.id)
        if first == "gh":
            offenders.append(node.lineno)
    assert not offenders, (
        f"unmetered gh calls at agent_hq/github.py lines {offenders}; "
        "route them through gh_meter.run_gh(argv, kind=...)"
    )


def _resolve_cmd_head(source, name):
    """First element of the last `name = [...]` assignment in the module."""
    import ast

    head = None
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if name not in targets:
            continue
        if isinstance(node.value, ast.List) and node.value.elts:
            first = node.value.elts[0]
            if isinstance(first, ast.Constant) and first.value == "gh":
                head = "gh"
    return head
