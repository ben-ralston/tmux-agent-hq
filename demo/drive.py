#!/usr/bin/env python3
"""Change the staged sessions' status over about twenty seconds.

    uv run python demo/drive.py

Run it after ``demo/seed.py`` while the demo dashboard is open. Each step
rewrites a session's registry record (what live status is read from) and, when
the session says something, appends to its transcript and fires the Stop hook,
the same three things a real Claude Code session does.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import data
import seed  # isolates the process on import

# (seconds from start, session, native status, waiting-for, new reply, new phase)
STEPS = [
    (3, "orbit-57", "busy", None, None, None),
    (
        7,
        "scratch",
        "idle",
        None,
        "Release notes for 2.4 are drafted in NOTES.md: four added, five changed, two fixed.",
        "adhoc",
    ),
    (10, "orbit-42", "waiting", "approve Bash", None, None),
    (
        14,
        "orbit-57",
        "idle",
        None,
        "All 200 runs passed, so the flake is gone. The branch is pushed and ready for review.",
        "review",
    ),
    (18, "orbit-42", "busy", None, None, None),
]


def panes() -> dict[str, tuple[str, int]]:
    rows = seed.tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_id}\t#{pane_pid}")
    return {
        name: (pane, int(pid)) for name, pane, pid in (r.split("\t") for r in rows.splitlines())
    }


def apply(
    session: str, status: str, waiting_for: str | None, reply: str | None, phase: str | None
) -> None:
    agent = next(a for a in data.AGENTS if a["session"] == session)
    pane_id, pid = panes()[session]
    registry = Path.home() / ".claude" / "sessions" / f"{pid}.json"
    record = json.loads(registry.read_text())
    record.update(status=status, statusUpdatedAt=int(time.time() * 1000))
    record.pop("waitingFor", None)
    if waiting_for:
        record["waitingFor"] = waiting_for
    registry.write_text(json.dumps(record))
    if reply is None:
        return
    sid, cwd = seed.session_id(session), str(seed.worktree_dir(agent))
    transcript = Path(seed.transcript_path_for(cwd, sid)[0])
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": f"{reply}\n\n<phase>{phase}</phase>"}],
    }
    with transcript.open("a") as handle:
        handle.write(
            json.dumps(
                dict(
                    type="assistant",
                    uuid=str(uuid.uuid4()),
                    sessionId=sid,
                    cwd=cwd,
                    isSidechain=False,
                    timestamp=data.iso(),
                    message=message,
                )
            )
            + "\n"
        )
    seed.fire_hook(
        pane_id,
        {
            "hook_event_name": "Stop",
            "session_id": sid,
            "transcript_path": str(transcript),
            "cwd": cwd,
            "last_assistant_message": reply,
        },
    )


def main() -> None:
    start = time.monotonic()
    for at, session, status, waiting_for, reply, phase in STEPS:
        time.sleep(max(0.0, at - (time.monotonic() - start)))
        apply(session, status, waiting_for, reply, phase)
        print(f"{at:>3}s  {session}: {status}" + (f" ({phase})" if phase else ""))


if __name__ == "__main__":
    main()
