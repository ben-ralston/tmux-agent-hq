#!/usr/bin/env python3
"""Stage the demo environment: repos, tmux sessions, transcripts and stores.

    uv run python demo/seed.py           # build (or rebuild) the demo environment
    uv run python demo/seed.py --stop    # stop the demo tmux server and daemon

Everything is written under the demo root (see ``demo/_isolate.py``) from the
invented data in ``demo/data.py``. The staged "agents" are ``sleep`` processes
started through symlinks named ``claude`` and ``codex``, so discovery finds them the way
it finds real ones; no real agent, account or repository is involved.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

from _isolate import is_demo_process, isolate, tmux_socket

ROOT = isolate()
HOME = Path.home()
SOCKET = tmux_socket(ROOT)

import data  # noqa: E402

from agent_hq import hooks  # noqa: E402
from agent_hq.autopilot_store import Attempt, AutopilotStore, Outcome, RunRecord  # noqa: E402
from agent_hq.hygiene_store import Finding, FindingType, HygieneStore  # noqa: E402
from agent_hq.pinned_store import PinnedSessionsStore  # noqa: E402
from agent_hq.transcript import transcript_path_for  # noqa: E402
from agent_hq.triage_store import (  # noqa: E402
    AgentFitness,
    Effort,
    TriageRecord,
    TriageState,
    TriageStore,
)

GREEN, DIM, BOLD, YELLOW, RESET = "\x1b[32m", "\x1b[2m", "\x1b[1m", "\x1b[33m", "\x1b[0m"


def run(*argv: str, cwd: Path | None = None) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout


def tmux(*argv: str) -> str:
    return run("tmux", "-S", str(SOCKET), *argv)


def git(cwd: Path, *argv: str) -> str:
    identity = ["-c", "user.name=Demo User", "-c", "user.email=demo@example.com"]
    return run("git", *identity, *argv, cwd=cwd)


def session_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"agent-hq-demo.{name}"))


# --- lifecycle ---------------------------------------------------------------


def stop() -> None:
    """Stop only what the demo started: its daemon and its tmux server."""
    pid_file = ROOT / "runtime" / "daemon.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        pid = None
    if pid is not None:
        # The file can outlive its daemon, and the number can be reused by an
        # unrelated process: signal it only if its command line is the demo's.
        if is_demo_process(pid, ROOT):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            print(f"not signalling PID {pid}: it is not a demo process (stale PID file)")
        pid_file.unlink(missing_ok=True)
    if SOCKET.exists():
        subprocess.run(["tmux", "-S", str(SOCKET), "kill-server"], capture_output=True)


def reset() -> None:
    stop()
    time.sleep(0.5)
    for name in ("home", "runtime"):
        shutil.rmtree(ROOT / name, ignore_errors=True)
    (ROOT / "home").mkdir()
    (ROOT / "runtime").mkdir(mode=0o700)


# --- repositories ------------------------------------------------------------


def worktree_dir(agent: dict) -> Path:
    if agent["repo"] is None:
        return HOME / "scratch"
    if agent["branch"] == "main":
        return HOME / "code" / agent["repo"]
    slug = agent["branch"].replace("/", "-")
    return HOME / ".claude-worktrees" / f"{agent['repo']}.{slug}"


def build_repos() -> None:
    (HOME / "scratch").mkdir(parents=True)
    for repo in data.REPOS:
        path = HOME / "code" / repo
        path.mkdir(parents=True)
        git(path, "init", "-q", "-b", "main")
        git(path, "remote", "add", "origin", f"git@github.com:{data.full_name(repo)}.git")
        (path / "README.md").write_text(f"# {repo}\n\nInvented repository for the Agent HQ demo.\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "Initial commit")
    for agent in data.AGENTS:
        if agent["repo"] is None or agent["branch"] == "main":
            continue
        main, tree = HOME / "code" / agent["repo"], worktree_dir(agent)
        tree.parent.mkdir(parents=True, exist_ok=True)
        git(main, "worktree", "add", "-q", "-b", agent["branch"], str(tree))
        for index, message in enumerate(agent["commits"]):
            (tree / f"change_{index}.txt").write_text(f"{message}\n" * (12 + 9 * index))
            git(tree, "add", "-A")
            git(tree, "commit", "-q", "-m", message)
        for relative, content in agent["dirty"].items():
            target = tree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content * 6)


# --- configuration -----------------------------------------------------------


def write_config() -> None:
    lines = ["repos:"]
    for repo, (project, _number) in data.REPOS.items():
        lines += [
            f"  {data.full_name(repo)}:",
            f"    local_path: ~/code/{repo}",
            f"    project: {project}",
        ]
    lines.append("projects:")
    for project, number in sorted(set(data.REPOS.values())):
        lines += [f"  - name: {project}", f"    number: {number}", '    owner: "@me"']
    lines += [
        "dispatch:",
        "  scratch_cwd: ~/scratch",
        "triage:",
        "  data_dir: ~/triage-data",
        "  sync_enabled: false",
        "  ai_enabled: false",
        "  hygiene_enabled: false",
        "autopilot:",
        "  enabled: true",
        f"  repos: [{', '.join(data.full_name(r) for r in data.REPOS)}]",
        "automations:",
    ]
    for job in data.AUTOMATIONS:
        lines += [
            f"  - name: {job['name']}",
            f"    label: com.demo.{job['name']}",
            f"    log_file: ~/logs/{job['name']}.log",
        ]
        for key in ("failure_pattern", "stale_after"):
            if key in job:
                lines.append(f'    {key}: "{job[key]}"')
    config = HOME / ".config" / "agent-hq" / "projects.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines) + "\n")
    (HOME / "triage-data").mkdir(exist_ok=True)
    secrets = HOME / ".claude" / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "logger-admin.token").write_text("demo-token-not-a-secret\n")


# --- agents ------------------------------------------------------------------


def colourise(line: str) -> str:
    if line.startswith("⏺"):
        return f"{GREEN}⏺{RESET}{line[1:]}"
    if line.lstrip().startswith("⎿"):
        return f"{DIM}{line}{RESET}"
    if line.startswith(("✻", "✶")):
        return f"{YELLOW}{line}{RESET}"
    if line.startswith(("›", "•")):
        return f"{BOLD}{line[0]}{RESET}{line[1:]}"
    return line


def stage_binaries() -> None:
    for name in ("claude", "codex"):
        target = ROOT / "bin" / name
        # A symlink, not a copy: macOS kills a copied system binary, and the
        # process table reports the path that was executed, which is what
        # discovery matches on.
        if not target.is_symlink():
            target.symlink_to("/bin/sleep")


def start_sessions() -> dict[str, tuple[str, int]]:
    """Start one tmux session per agent. Returns session -> (pane id, pid)."""
    screens = ROOT / "home" / ".demo-screens"
    screens.mkdir(exist_ok=True)
    for agent in data.AGENTS:
        screen = screens / f"{agent['session']}.txt"
        screen.write_text("\n".join(colourise(line) for line in agent["screen"]) + "\n")
        binary = ROOT / "bin" / agent["tool"]
        command = f"clear; cat '{screen}'; exec '{binary}' 100000000"
        tmux(
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            agent["session"],
            "-x",
            "100",
            "-y",
            "28",
            "-c",
            str(worktree_dir(agent)),
            command,
        )
    time.sleep(1.0)
    panes = {}
    for line in tmux(
        "list-panes", "-a", "-F", "#{session_name}\t#{pane_id}\t#{pane_pid}"
    ).splitlines():
        name, pane_id, pid = line.split("\t")
        panes[name] = (pane_id, int(pid))
    return panes


def write_registry(agent: dict, pid: int) -> None:
    """The per-session record Claude Code keeps, which live status is read from."""
    record = {
        "pid": pid,
        "sessionId": session_id(agent["session"]),
        "cwd": str(worktree_dir(agent)),
        "name": agent["name"],
        "nameSource": "user",
        "status": agent["status"],
        "peerProtocol": 1,
        "statusUpdatedAt": int((time.time() - 60 * agent.get("waiting_minutes", 1)) * 1000),
    }
    if agent["status"] == "waiting":
        record["waitingFor"] = agent["waiting_for"]
    sessions = HOME / ".claude" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(json.dumps(record))


def transcript_records(agent: dict) -> list[dict]:
    sid, cwd = session_id(agent["session"]), str(worktree_dir(agent))
    base = dict(sessionId=sid, cwd=cwd, isSidechain=False, userType="external")
    start = 40 + 4 * len(agent["turns"])
    records = [
        dict(
            base,
            type="user",
            uuid=str(uuid.uuid4()),
            timestamp=data.iso(start),
            message={"role": "user", "content": agent["prompt"]},
        )
    ]
    last_text = max(i for i, turn in enumerate(agent["turns"]) if turn[0] == "text")
    for index, turn in enumerate(agent["turns"]):
        if turn[0] == "tool":
            block = {
                "type": "tool_use",
                "id": f"toolu_demo_{index}",
                "name": turn[1],
                "input": turn[2],
            }
        else:
            text = turn[1]
            if index == last_text:
                text += f"\n\n<phase>{agent['phase']}</phase>"
            block = {"type": "text", "text": text}
        records.append(
            dict(
                base,
                type="assistant",
                uuid=str(uuid.uuid4()),
                timestamp=data.iso(start - 4 * (index + 1)),
                message={"role": "assistant", "content": [block]},
            )
        )
    return records


def write_transcript(agent: dict) -> Path:
    path = Path(transcript_path_for(str(worktree_dir(agent)), session_id(agent["session"]))[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in transcript_records(agent)))
    return path


def fire_hook(pane_id: str, payload: dict) -> None:
    """Run the real hook entry point for one event, as the pane would."""
    stdin, argv = sys.stdin, sys.argv
    os.environ["TMUX_PANE"] = pane_id
    sys.stdin, sys.argv = io.StringIO(json.dumps(payload)), ["agent-hq-hook"]
    try:
        with redirect_stdout(io.StringIO()):
            hooks.handle_hook_event()
    finally:
        sys.stdin, sys.argv = stdin, argv
        os.environ.pop("TMUX_PANE", None)


def stage_agents() -> None:
    stage_binaries()
    panes = start_sessions()
    for agent in data.AGENTS:
        if agent["tool"] != "claude":
            continue
        pane_id, pid = panes[agent["session"]]
        write_registry(agent, pid)
        transcript = write_transcript(agent)
        summary = [turn[1] for turn in agent["turns"] if turn[0] == "text"][-1]
        fire_hook(
            pane_id,
            {
                "hook_event_name": "Stop",
                "session_id": session_id(agent["session"]),
                "transcript_path": str(transcript),
                "cwd": str(worktree_dir(agent)),
                "last_assistant_message": summary,
            },
        )
    PinnedSessionsStore().add(session_id("orbit-57"))


# --- issue-side stores -------------------------------------------------------


def key(short: str) -> str:
    return f"{data.OWNER}/{short}"


def seed_triage() -> None:
    store = TriageStore(HOME / "triage-data")
    for short, fields in data.TRIAGE.items():
        proposed = fields["state"] == "proposed"
        record = TriageRecord(
            issue_key=key(short),
            value=fields["value"],
            effort=Effort(fields["effort"]),
            agent_fitness=AgentFitness(fields["fitness"]),
            state=TriageState.PROPOSED if proposed else TriageState.CONFIRMED,
            interest=fields.get("interest"),
            pin=fields.get("pin"),
            autopilot=fields.get("autopilot"),
            reasoning=fields.get("reasoning", {}),
            proposed_at=data.iso(90) if proposed else None,
            model="opus" if proposed else None,
            updated_at=data.iso(90),
        )
        store.upsert(record, actor="ai" if proposed else "human")
        if not proposed:
            # A human edit never confirms; confirmation is its own step.
            store.confirm(record.issue_key, now=data.iso(80))


def seed_hygiene() -> None:
    open_keys = {key(f"{row[0]}#{row[1]}") for row in data.ISSUES}
    offboard = [key(f"{repo}#{number}") for repo, number, *_ in data.OFFBOARD_ISSUES]
    findings = [
        Finding.new(
            FindingType.DUPLICATE,
            [key("tide-web#24"), key("tide-web#21")],
            action={"close": key("tide-web#24"), "of": key("tide-web#21")},
            reasoning="Both report the table losing its sort order when a filter changes; "
            "#21 has the reproduction steps.",
        ),
        Finding.new(
            FindingType.STALE,
            [key("kiln-cli#11")],
            action={"close": key("kiln-cli#11")},
            reasoning="No activity in 300 days and no triage record.",
        ),
        Finding.new(
            FindingType.MISMATCH,
            [key("orbit-api#64")],
            action={"key": key("orbit-api#64"), "proposed": 4},
            reasoning="Marked value 2, but 3.10 leaves support before the next release, "
            "so CI will start failing on its own.",
        ),
        *[
            Finding.new(
                FindingType.OFFBOARD,
                [key(f"{repo}#{number}")],
                action={
                    "kind": "add_to_board",
                    "key": key(f"{repo}#{number}"),
                    "project_number": data.REPOS[repo][1],
                    "project_owner": "@me",
                    "project_name": data.REPOS[repo][0],
                    "title": title,
                },
                reasoning=f"Open in {data.full_name(repo)} but not an item on the "
                f"{data.REPOS[repo][0]} board — invisible to Queue, Triage and dispatch.",
            )
            for repo, number, title, *_ in data.OFFBOARD_ISSUES
        ],
    ]
    HygieneStore(HOME / "triage-data").record_sweep(
        findings,
        now=data.iso(120),
        sweep_id="demo-sweep",
        model="opus",
        open_keys=open_keys | set(offboard),
    )


def seed_autopilot() -> None:
    store = AutopilotStore(HOME / "triage-data")
    attempts = [
        (
            "tide-web#21",
            "feature/21-table-sort-ap1",
            Outcome.PR_OPEN,
            "https://github.com/acme-labs/tide-web/pull/33",
            None,
        ),
        (
            "orbit-api#64",
            "chore/64-drop-py310-ap2",
            Outcome.ENDED_WITHOUT_PR,
            None,
            "session ended after 40 turns without opening a PR",
        ),
    ]
    for short, branch, outcome, pr_url, detail in attempts:
        repo = key(short.split("#")[0])
        attempt = Attempt(
            issue_key=key(short),
            repo=repo,
            branch=branch,
            run_id="demo-run",
            dispatched_at=data.iso(60 * 9),
            outcome=Outcome.LAUNCHING,
            attempt_id=uuid.uuid4().hex[:8],
        )
        store.reserve(attempt, max_open=3)
        store.finish_launch(
            attempt.issue_key,
            attempt.attempt_id,
            outcome=outcome,
            session_name=branch.replace("/", "-"),
            worktree_path=None,
            detail=detail,
            now=data.iso(60 * 9),
        )
        if pr_url:
            state_file = HOME / "triage-data" / "autopilot" / "state.json"
            state = json.loads(state_file.read_text())
            state["attempts"][attempt.issue_key]["pr_url"] = pr_url
            state_file.write_text(json.dumps(state, indent=2))
    store.record_run(
        RunRecord(
            run_id="demo-run",
            started_at=data.iso(60 * 9),
            finished_at=data.iso(60 * 9 - 1),
            launched=[key("tide-web#21"), key("orbit-api#64")],
            skipped=[(key("orbit-api#66"), "max_open reached")],
            throttled=True,
        )
    )


def seed_automations() -> None:
    agents_dir = HOME / "Library" / "LaunchAgents"
    logs = HOME / "logs"
    agents_dir.mkdir(parents=True)
    logs.mkdir()
    now = datetime.now()
    for job in data.AUTOMATIONS:
        label = f"com.demo.{job['name']}"
        plist: dict = {"Label": label, "ProgramArguments": ["/usr/bin/true"]}
        if job.get("keepalive"):
            plist["KeepAlive"] = True
            last_run = now - timedelta(minutes=job["ran"])
        else:
            plist["StartCalendarInterval"] = {"Hour": job["hour"], "Minute": 0}
            fired = now.replace(hour=job["hour"], minute=0, second=0, microsecond=0)
            if fired > now:
                fired -= timedelta(days=1)
            last_run = (
                fired + timedelta(minutes=2)
                if job["ran"] is None
                else now - timedelta(minutes=job["ran"])
            )
        with open(agents_dir / f"{label}.plist", "wb") as handle:
            plistlib.dump(plist, handle)
        log = logs / f"{job['name']}.log"
        stamp = last_run.strftime("%Y-%m-%d %H:%M:%S")
        log.write_text("".join(f"[{stamp}] {line}\n" for line in job["log"]))
        os.utime(log, (last_run.timestamp(), last_run.timestamp()))


def main() -> int:
    if "--stop" in sys.argv[1:]:
        stop()
        print("demo tmux server and daemon stopped")
        return 0
    reset()
    build_repos()
    write_config()
    stage_agents()
    seed_triage()
    seed_hygiene()
    seed_autopilot()
    seed_automations()
    print(f"demo environment staged under {ROOT}")
    print(f"tmux socket: {SOCKET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
