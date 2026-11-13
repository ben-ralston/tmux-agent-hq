"""Behavioral check for #412: the remote health tools on the demo instance.

Run it inside the demo environment (see demo/README.md), where launchd and the
logger are stand-ins serving invented data:

    uv run python demo/run.py .claude/skills/verify-changes/scripts/remote_health_probe.py

Read-only. Calls automations_health and recent_errors through the real
RemoteFastMCP boundary (annotated, threaded tool copies), then checks each
output against data read independently. Prints counts and job names only, so a
run's output carries no log text. eventlog.emit is captured, never sent.
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from agent_hq import automations, eventlog, logreader
from agent_hq.config import load_config
from agent_hq.remote import tools as rt
from agent_hq.remote.app import remote_tool_copies
from agent_hq.remote.asgi import RemoteFastMCP
from agent_hq.remote.request_log import RequestLog

eventlog.emit = lambda *a, **k: True

tmp = Path(tempfile.mkdtemp(prefix="verify-412-"))
server = RemoteFastMCP(
    "probe",
    request_log=RequestLog(tmp / "requests.log"),
    tools=remote_tool_copies(rt.build_tool_source()),
)
failures = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


async def call(name, **args):
    result = await server.call_tool(name, args)
    content = result[0] if isinstance(result, tuple) else result
    return "".join(block.text for block in content)


SECRET_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{8,}|github_pat_|\bsk-[A-Za-z0-9_-]{8,}|xox[a-z]-|AKIA[0-9A-Z]{12}"
    r"|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}|-----BEGIN|Bearer [A-Za-z0-9]|https?://[^\s\"]+\?[^\[\s]"
    r"|/Users/|(?<![0-9A-Za-z*])\d{9,}(?![0-9A-Za-z])"
)


def clean(output, what):
    hits = SECRET_SHAPES.findall(output)
    check(not hits, f"{what}: no secret-shaped text ({len(hits)} hits, {len(output)} chars)")


async def main():
    cfg = load_config()
    truth = automations.collect_health(cfg.automations, datetime.now(), os.getuid())
    data = json.loads(await call("automations_health", format="json"))
    jobs = {job["name"]: job for job in data["jobs"]}
    check(set(jobs) == {h.name for h in truth}, f"every configured job present ({len(jobs)})")
    # A job can change state between the two reads, but not usually.
    same = [h.name for h in truth if jobs[h.name]["status"] == h.status.value]
    check(len(same) == len(truth), f"statuses match collect_health ({len(same)}/{len(truth)})")
    print("     ", Counter(job["status"] for job in jobs.values()).most_common())

    text = await call("automations_health")
    clean(text, "automations_health text")
    leaks = [h.name for h in truth if h.label in text or str(h.log_file) in text]
    check(not leaks, "no launchd label or log path in text")

    for h in truth:
        for fmt in ("text", "json"):
            output = await call("automations_health", job=h.name, format=fmt)
            clean(output, f"job {h.name} {fmt}")
            check(str(h.log_file) not in output, f"job {h.name} {fmt}: no log path")

    page = logreader.fetch_logs(levels=logreader.DEFAULT_LEVELS, since="7d", limit=500)
    want = Counter(row.app for row in page.rows)
    summary = json.loads(await call("recent_errors", window="7d", format="json"))
    got = {group["app"]: group["count"] for group in summary["apps"]}
    check(got == dict(want), f"7d counts match fetch_logs {dict(want)}")
    for window in logreader.WINDOWS:
        clean(await call("recent_errors", window=window), f"recent_errors {window} text")
    for app in want:
        for fmt in ("text", "json"):
            output = await call("recent_errors", window="7d", app=app, format=fmt)
            clean(output, f"recent_errors {app} {fmt}")


asyncio.run(main())
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'all checks passed'}")
sys.exit(1 if failures else 0)
