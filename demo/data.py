"""Invented data for the demo instance. Nothing here describes a real repo, issue or session.

Shared by ``demo/seed.py`` (which stages it on disk and in tmux) and by the
stand-in ``gh`` / ``launchctl`` commands in ``demo/bin``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

OWNER = "acme-labs"


def iso(minutes_ago: float = 0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# name -> (project name, project number)
REPOS = {
    "orbit-api": ("Orbit", 1),
    "tide-web": ("Tide", 2),
    "kiln-cli": ("Tide", 2),
}

# (repo, number, title, status, labels, created days ago, updated hours ago, body)
ISSUES = [
    (
        "orbit-api",
        42,
        "Return rate-limit headers on every 429",
        "In Progress",
        ["api"],
        9,
        1,
        "Clients cannot tell when to retry. Add `Retry-After` and the three "
        "`X-RateLimit-*` headers to every throttled response, and document them.",
    ),
    (
        "orbit-api",
        57,
        "Webhook retry test fails about one run in five",
        "In Progress",
        ["bug", "tests"],
        4,
        2,
        "`test_webhook_retry_backoff` depends on wall-clock sleeps. Replace them with "
        "the fake clock the scheduler tests already use.",
    ),
    (
        "orbit-api",
        61,
        "Paginate GET /v2/events with a cursor",
        "Todo",
        ["api"],
        12,
        30,
        "Offset pagination skips rows when events arrive mid-scan. Switch to an opaque "
        "cursor over (created_at, id).",
    ),
    (
        "orbit-api",
        63,
        "Sentinel: connection pool exhausted in worker",
        "Todo",
        ["sentinel", "bug"],
        1,
        5,
        "Filed automatically from 14 error events in 24h.\n\n"
        "`PoolTimeout: no connection available after 30s` in `worker.drain_queue`.",
    ),
    (
        "orbit-api",
        64,
        "Drop Python 3.10 from the CI matrix",
        "Todo",
        ["chore"],
        20,
        96,
        "3.10 reaches end of life before the next release. Remove it from CI and classifiers.",
    ),
    (
        "orbit-api",
        66,
        "Audit log misses bulk deletes",
        "Todo",
        ["bug", "security"],
        2,
        7,
        "`DELETE /v2/projects/{id}/items` writes one audit row for the request, not one per "
        "deleted item, so the log cannot answer who removed a given item.",
    ),
    (
        "orbit-api",
        68,
        "Document the idempotency key contract",
        "Todo",
        ["docs"],
        30,
        200,
        "Explain key lifetime, the replayed-response behaviour and the 409 on body mismatch.",
    ),
    (
        "tide-web",
        18,
        "Add a dark mode toggle to the settings page",
        "In Progress",
        ["ui"],
        6,
        3,
        "Respect `prefers-color-scheme` by default and persist an explicit choice.",
    ),
    (
        "tide-web",
        21,
        "Table view loses the sort order after a filter change",
        "Todo",
        ["bug", "ui"],
        3,
        9,
        "Steps: sort by Updated, then add any filter chip. The table falls back to the "
        "default sort.",
    ),
    (
        "tide-web",
        23,
        "Keyboard shortcuts for the command palette",
        "Todo",
        ["ui"],
        15,
        50,
        "Open with Cmd-K, arrow keys to move, Enter to run, Esc to close.",
    ),
    (
        "tide-web",
        24,
        "Table view forgets its sort when filters change",
        "Todo",
        ["bug"],
        1,
        4,
        "Looks the same as an older report: sorting is reset whenever a filter is toggled.",
    ),
    (
        "tide-web",
        27,
        "Bundle size grew 180 kB after the charting upgrade",
        "Todo",
        ["perf"],
        5,
        20,
        "The new charting release pulls in its full locale set. Import only what we render.",
    ),
    (
        "kiln-cli",
        7,
        "Validate config files before running a build",
        "In Progress",
        ["cli"],
        8,
        6,
        "`kiln build` should fail fast with the line and column of the first config error.",
    ),
    (
        "kiln-cli",
        9,
        "kiln init overwrites an existing config without asking",
        "Todo",
        ["bug", "cli"],
        2,
        8,
        "Running `kiln init` in a configured directory replaces `kiln.toml`. Ask first, "
        "or require `--force`.",
    ),
    (
        "kiln-cli",
        11,
        "Shell completions for zsh and fish",
        "Todo",
        ["cli"],
        40,
        300,
        "Generate completions from the argument parser and ship them in the release archive.",
    ),
]

# Open but missing from its project board: the Hygiene tab's "Not on board" finding.
OFFBOARD_ISSUES = [
    ("kiln-cli", 12, "Progress bar flickers in narrow terminals", 1, 3),
]

# issue key suffix -> triage fields. state: confirmed | proposed (others stay untriaged).
TRIAGE = {
    "orbit-api#42": dict(value=4, effort="M", fitness="solo", state="confirmed", pin=True),
    "orbit-api#57": dict(value=3, effort="S", fitness="solo", state="confirmed"),
    "orbit-api#61": dict(value=4, effort="L", fitness="pair", state="confirmed", interest=True),
    "orbit-api#66": dict(value=5, effort="M", fitness="pair", state="confirmed", autopilot=True),
    "orbit-api#64": dict(value=2, effort="S", fitness="solo", state="confirmed", autopilot=True),
    "tide-web#18": dict(value=3, effort="M", fitness="pair", state="confirmed"),
    "tide-web#21": dict(value=3, effort="S", fitness="solo", state="confirmed", autopilot=True),
    "kiln-cli#7": dict(value=4, effort="M", fitness="solo", state="confirmed"),
    "orbit-api#63": dict(
        value=4,
        effort="M",
        fitness="needs_you",
        state="proposed",
        reasoning=dict(
            value="Fourteen errors in a day from the queue worker; jobs are being dropped.",
            effort="Pool sizing plus a regression test around drain_queue.",
            agent_fitness="Needs a decision on the pool limit for the shared database.",
        ),
    ),
    "tide-web#27": dict(
        value=3,
        effort="S",
        fitness="solo",
        state="proposed",
        reasoning=dict(
            value="Slower first load for every user, but nothing is broken.",
            effort="One import change and a bundle-size check in CI.",
            agent_fitness="Mechanical change with a measurable result.",
        ),
    ),
    "kiln-cli#9": dict(
        value=5,
        effort="S",
        fitness="solo",
        state="proposed",
        reasoning=dict(
            value="Silent data loss on a command new users run first.",
            effort="A confirmation prompt, a --force flag and two tests.",
            agent_fitness="Small and well specified.",
        ),
    ),
    "orbit-api#68": dict(
        value=2,
        effort="S",
        fitness="pair",
        state="proposed",
        reasoning=dict(
            value="Reduces support questions; no behaviour change.",
            effort="One page of documentation.",
            agent_fitness="An agent can draft it; the 409 wording needs a review.",
        ),
    ),
}

# Staged agent sessions. ``status`` is Claude's native value: busy | waiting | idle.
AGENTS = [
    dict(
        session="orbit-42",
        repo="orbit-api",
        branch="feature/42-rate-limit-headers",
        tool="claude",
        name="rate-limit-headers",
        status="busy",
        phase="implement",
        prompt="Work on issue #42: return rate-limit headers on every 429.",
        turns=[
            ("tool", "Read", {"file_path": "orbit/middleware/throttle.py"}),
            ("tool", "Grep", {"pattern": "Retry-After"}),
            (
                "text",
                "The throttle middleware already knows the window and the remaining "
                "budget; it just never writes them to the response. I'll add the four "
                "headers there and cover them with a test for each limit type.",
            ),
            ("tool", "Edit", {"file_path": "orbit/middleware/throttle.py"}),
            ("tool", "Write", {"file_path": "tests/test_throttle_headers.py"}),
            ("tool", "Bash", {"command": "uv run pytest tests/test_throttle_headers.py -q"}),
            (
                "text",
                "Headers are written for per-key and per-IP limits. Two of six new tests "
                "fail: the burst limiter reports its window in milliseconds. Converting "
                "that next.",
            ),
        ],
        screen=[
            "⏺ Update(orbit/middleware/throttle.py)",
            "  ⎿  Added 14 lines, removed 2 lines",
            "",
            "⏺ Bash(uv run pytest tests/test_throttle_headers.py -q)",
            "  ⎿  ....FF                                                     [100%]",
            "     2 failed, 4 passed in 0.41s",
            "",
            "⏺ The burst limiter reports its window in milliseconds. Converting it",
            "  before the header is written.",
            "",
            "✻ Editing throttle.py… (2m 14s · ↓ 3.1k tokens · esc to interrupt)",
        ],
        commits=["Add rate-limit headers to throttled responses"],
        dirty={"orbit/middleware/throttle.py": "WINDOW_MS = 1000\n"},
    ),
    dict(
        session="orbit-57",
        repo="orbit-api",
        branch="bugfix/57-flaky-webhook-retry",
        tool="claude",
        name="flaky-webhook-retry",
        status="waiting",
        phase="qa",
        waiting_for="approve Bash",
        waiting_minutes=14,
        prompt="Fix the flaky webhook retry test (#57). Use the fake clock.",
        turns=[
            ("tool", "Read", {"file_path": "tests/test_webhook_retry.py"}),
            (
                "text",
                "The test sleeps for real and asserts on elapsed time. I replaced the "
                "sleeps with the scheduler's FakeClock and advanced it explicitly.",
            ),
            ("tool", "Edit", {"file_path": "tests/test_webhook_retry.py"}),
            (
                "text",
                "The change is in place. I'd like to run the test 200 times to confirm "
                "the flake is gone before opening the PR.",
            ),
            ("tool", "Bash", {"command": "uv run pytest tests/test_webhook_retry.py --count 200"}),
        ],
        screen=[
            "⏺ Update(tests/test_webhook_retry.py)",
            "  ⎿  Added 9 lines, removed 11 lines",
            "",
            "⏺ I'd like to run the test 200 times to confirm the flake is gone.",
            "",
            "╭──────────────────────────────────────────────────────────────────╮",
            "│ Bash command                                                     │",
            "│                                                                  │",
            "│   uv run pytest tests/test_webhook_retry.py --count 200          │",
            "│                                                                  │",
            "│ Do you want to proceed?                                          │",
            "│ ❯ 1. Yes                                                         │",
            "│   2. Yes, and don't ask again for uv run pytest                  │",
            "│   3. No, and tell Claude what to do differently (esc)            │",
            "╰──────────────────────────────────────────────────────────────────╯",
        ],
        commits=["Use the fake clock in webhook retry tests"],
        dirty={},
    ),
    dict(
        session="tide-18",
        repo="tide-web",
        branch="feature/18-dark-mode-toggle",
        tool="claude",
        name="dark-mode-toggle",
        status="idle",
        phase="ready",
        prompt="Implement #18, the dark mode toggle, and open a PR when it is reviewed.",
        turns=[
            ("tool", "Edit", {"file_path": "src/settings/Appearance.tsx"}),
            ("tool", "Bash", {"command": "npm test -- appearance"}),
            ("tool", "Bash", {"command": "gh pr create --fill"}),
            (
                "text",
                "The toggle is in, the choice persists across reloads, and the system "
                "preference is used until someone picks one. Tests and lint pass, the "
                "review found nothing open, and the PR is up for you to merge.",
            ),
        ],
        screen=[
            "⏺ Bash(gh pr create --fill)",
            "  ⎿  Opened pull request #31: Add a dark mode toggle",
            "",
            "⏺ The toggle is in, the choice persists across reloads, and the system",
            "  preference is used until someone picks one. Tests and lint pass and",
            "  the PR is up for you to merge.",
            "",
            "╭──────────────────────────────────────────────────────────────────╮",
            "│ >                                                                │",
            "╰──────────────────────────────────────────────────────────────────╯",
            "  ? for shortcuts",
        ],
        commits=["Add appearance settings with a dark mode toggle", "Persist the theme choice"],
        dirty={},
    ),
    dict(
        session="tide-web",
        repo="tide-web",
        branch="main",
        tool="claude",
        name="tide-backlog",
        status="idle",
        phase="adhoc",
        prompt="Which open issues look like duplicates of each other?",
        turns=[
            ("tool", "Bash", {"command": "gh issue list --state open --limit 50"}),
            (
                "text",
                "#21 and #24 describe the same bug: the table loses its sort order when "
                "a filter changes. #24 is newer and has less detail, so I'd close it in "
                "favour of #21.",
            ),
        ],
        screen=[
            "⏺ #21 and #24 describe the same bug: the table loses its sort order",
            "  when a filter changes. #24 is newer and has less detail, so I'd",
            "  close it in favour of #21.",
            "",
            "╭──────────────────────────────────────────────────────────────────╮",
            "│ >                                                                │",
            "╰──────────────────────────────────────────────────────────────────╯",
            "  ? for shortcuts",
        ],
        commits=[],
        dirty={},
    ),
    dict(
        session="kiln-7",
        repo="kiln-cli",
        branch="feature/7-config-validate",
        tool="codex",
        name=None,
        status=None,
        phase=None,
        prompt="",
        turns=[],
        screen=[
            "› Plan the config validation for #7 before changing any code.",
            "",
            "• I'll read the loader first, then propose where validation should run.",
            "",
            "• Explored",
            "  └ Read kiln/config/loader.py",
            "    Search TomlDecodeError in kiln",
            "",
            "• Proposed plan: validate in load_config(), return every error with its",
            "  line and column, and stop the build before any step runs.",
            "",
            "› ",
        ],
        commits=[],
        dirty={},
    ),
    dict(
        session="scratch",
        repo=None,
        branch=None,
        tool="claude",
        name="release-notes",
        status="busy",
        phase="adhoc",
        prompt="Draft release notes for orbit-api 2.4 from the merged PR titles.",
        turns=[
            ("tool", "Bash", {"command": "gh pr list --state merged --search 'milestone:2.4'"}),
            ("text", "Eleven PRs merged for 2.4. Grouping them into Added, Changed and Fixed."),
        ],
        screen=[
            "⏺ Bash(gh pr list --state merged --search 'milestone:2.4')",
            "  ⎿  11 pull requests",
            "",
            "⏺ Eleven PRs merged for 2.4. Grouping them into Added, Changed and",
            "  Fixed.",
            "",
            "✶ Drafting notes… (38s · ↓ 1.2k tokens · esc to interrupt)",
        ],
        commits=[],
        dirty={},
    ),
]

# branch -> state reported by the stand-in `gh pr view`.
PULL_REQUESTS = {"feature/18-dark-mode-toggle": "OPEN"}

# launchd jobs for the Automations tab. ``hour`` is the daily schedule; ``ran`` is how
# many minutes ago the log was last written; ``exit`` is launchd's last exit code.
AUTOMATIONS = [
    dict(
        name="nightly-backup",
        hour=2,
        ran=None,
        exit=0,
        log=["backup started", "uploaded 412 files (1.8 GB)", "Backup complete"],
    ),
    dict(
        name="dependency-audit",
        hour=6,
        ran=None,
        exit=1,
        failure_pattern="FAILED",
        log=["audit started", "checking 148 packages", "audit FAILED: 2 advisories need attention"],
    ),
    dict(name="link-checker", hour=7, ran=60 * 30, exit=0, log=["checked 930 links", "0 broken"]),
    dict(
        name="metrics-exporter",
        keepalive=True,
        stale_after="30m",
        ran=0.5,
        exit=None,
        log=["exporter listening on :9109", "heartbeat", "heartbeat"],
    ),
    dict(
        name="autopilot-nightly", hour=1, ran=None, exit=0, log=["autopilot: 2 launched, 1 skipped"]
    ),
]

# Rows served by the stand-in logger (demo/fake_logger.py) for the Errors tab.
# (app, level, minutes ago, source, message, json_extra)
LOG_ROWS = [
    (
        "orbit-api",
        "error",
        12,
        "worker-2",
        "PoolTimeout: no connection available after 30s",
        {"job": "drain_queue", "pool_size": 10, "waiting": 37},
    ),
    (
        "orbit-api",
        "error",
        47,
        "worker-1",
        "PoolTimeout: no connection available after 30s",
        {"job": "drain_queue", "pool_size": 10, "waiting": 22},
    ),
    (
        "orbit-api",
        "alert",
        95,
        "api-1",
        "p99 latency above 2s for 5 minutes",
        {"route": "GET /v2/events", "p99_ms": 2410},
    ),
    ("orbit-api", "warn", 100, "api-1", "slow query: events by project (1.9s)", {"rows": 48211}),
    ("orbit-api", "info", 110, "api-1", "deploy 2.3.7 finished", None),
    (
        "dependency-audit",
        "error",
        180,
        "ci-runner",
        "audit FAILED: 2 advisories need attention",
        {"advisories": ["DEMO-2026-0142", "DEMO-2026-0077"]},
    ),
    (
        "tide-web",
        "error",
        260,
        "edge",
        "Unhandled rejection in settings loader",
        {"release": "tide-web@1.12.0", "browser": "Safari 19"},
    ),
    ("tide-web", "warn", 300, "edge", "asset over budget: charts.js 412 kB", None),
    (
        "agent-hq",
        "error",
        400,
        "daemon",
        "daemon poll loop failing",
        {"consecutive_errors": 3, "error": "TimeoutExpired('tmux list-panes')"},
    ),
    ("agent-hq", "info", 405, "web", "web service started", {"bind": "127.0.0.1", "port": 9911}),
]

# Idea Inbox history: (text, minutes ago, status, repo, number, title, confidence, needs_triage)
INBOX = [
    (
        "kiln build should print how long each step took",
        25,
        "filed",
        "kiln-cli",
        13,
        "Print per-step timings after a build",
        0.93,
        False,
    ),
    (
        "the events page should remember my last filter",
        190,
        "filed",
        "tide-web",
        28,
        "Remember the last filter on the events page",
        0.88,
        False,
    ),
    (
        "something about retries being noisy in the logs?",
        1400,
        "filed",
        "orbit-api",
        69,
        "Reduce log noise from webhook retries",
        0.52,
        True,
    ),
]


def full_name(repo: str) -> str:
    return f"{OWNER}/{repo}"
