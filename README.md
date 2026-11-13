# Agent HQ

Agent HQ shows what the AI coding agents running in your tmux sessions are doing, so you can run several at once without checking each terminal. It finds Claude Code, Codex and other agent processes, groups them by repository and worktree, reads their live status (running, waiting on you, idle) and their transcripts, and shows this in a terminal dashboard and a web dashboard that works on a phone. It also keeps a ranked backlog of GitHub issues and starts new agent sessions on them.

This repository is a snapshot of a private working repo that was designed for personal use, by one person on macOS. Most of the code was written with coding agents under review. [CLAUDE.md](CLAUDE.md) and [.claude/skills](.claude/skills) are the instructions those agents work from.

![Agents tab: sessions grouped by phase, with the selected session's terminal on the right](docs/screenshots/web-agents-phase.png)

Every screenshot and recording in this README shows invented data, staged by the scripts in [demo/](demo/README.md).

![Recording: session cards change status and phase as agents work, then one session's history is opened](docs/recordings/dashboard-live-status.gif)

The dashboard as sessions change state: one is approved and starts running, one finishes, one stops to ask for permission, and a finished session's history is opened.

## Screenshots

| | |
|---|---|
| ![Session history](docs/screenshots/web-agent-history.png) Session history, read from the agent's transcript | ![Issue queue](docs/screenshots/web-issues-queue.png) Issue queue: pinned, then ranked by score, then untriaged |
| ![Triage inbox](docs/screenshots/web-issues-triage.png) Triage inbox: AI-proposed value, effort and fitness, each with a reason, waiting for confirmation | ![Hygiene](docs/screenshots/web-issues-hygiene.png) Hygiene: duplicates, stale issues, mismatched scores and issues missing from the board |
| ![Issue detail](docs/screenshots/web-issue-detail.png) Issue detail with triage controls and the autopilot flag | ![Automations](docs/screenshots/web-automations.png) Automations: autopilot attempts and the health of launchd jobs, with a job's log |
| ![Errors](docs/screenshots/web-errors.png) Errors: recent error and alert events by app, with surrounding rows for context | ![Idea inbox](docs/screenshots/web-inbox.png) Idea inbox: free text filed as a GitHub issue in the matching repo |
| ![List view](docs/screenshots/web-agents-list.png) Agents in list view | ![Kanban view](docs/screenshots/web-agents-kanban.png) Agents in kanban view |
| ![New session](docs/screenshots/web-new-session.png) Starting a new session from the browser | ![Browse](docs/screenshots/web-issues-browse.png) Browsing and searching every issue |

On a phone:

| | | |
|---|---|---|
| ![Agents on a phone](docs/screenshots/mobile-agents.png) Agents | ![Session history on a phone](docs/screenshots/mobile-agent-detail.png) Session history | ![Issue queue on a phone](docs/screenshots/mobile-issues-queue.png) Issue queue |

In the terminal:

| | |
|---|---|
| ![TUI agents tab](docs/screenshots/tui-agents.png) `agent-hq --dashboard`, Agents tab | ![TUI dispatch tab](docs/screenshots/tui-dispatch.png) `agent-hq --dashboard`, Dispatch tab |
| ![List and tree output](docs/screenshots/cli-list-tree.png) `agent-hq --list` and `--tree` | ![Watch mode](docs/screenshots/cli-watch.png) `agent-hq --watch 2` |
| ![Status line and recover](docs/screenshots/cli-status-recover.png) `agent-hq --status-line` and `agent-hq recover` | |

![Recording: moving through the terminal dashboard's Agents tab, then selecting issues on the Dispatch tab](docs/recordings/tui-dashboard.gif)

## Features

- **Agent discovery**: finds agent processes (Claude, Codex, Aider, OpenCode, Gemini, Cursor, Roo) in every tmux pane by walking the process tree
- **Git awareness**: groups agents by repository and worktree
- **Live status**: running, waiting or idle, read from the harness itself for Claude Code and wrapped Codex sessions; other agents show Unknown
- **Phase tracking**: design, implement, QA, review, ready or done, read from a tag the agent appends to its replies
- **Two front ends**: a Textual terminal dashboard (`--dashboard`) and a web dashboard (`agent-hq web`)
- **Issue backlog**: a ranked queue over GitHub Project boards, AI-proposed triage that a person confirms, hygiene findings, and dispatch of issues to new agent sessions (dispatch needs a launcher that is not in this repository; see [Dispatch needs an external launcher](#dispatch-needs-an-external-launcher))
- **Health views**: launchd job health, a nightly autopilot, and recent errors from a log service
- **MCP servers**: a local stdio server (`agent-hq-mcp`) and an optional remote one for claude.ai
- **Output for scripts**: list, tree, JSON and a tmux status-line format

## Installation

The project is managed with [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/ben-ralston/tmux-agent-hq
cd tmux-agent-hq
./run build                 # uv sync --extra dev
./run agent-hq --list       # run from the checkout

uv tool install -e .        # or put agent-hq on your PATH
```

## Usage

```bash
# List all agents
agent-hq --list

# Tree view
agent-hq --tree

# JSON output for scripting
agent-hq --json

# Filter by agent type
agent-hq --agent claude

# Filter by repository
agent-hq --repo my-project

# Watch mode (refresh every 2 seconds)
agent-hq --watch 2

# Jump directly to a pane
agent-hq --jump %5

# Status bar one-liner (for tmux status)
agent-hq --status-line

# Terminal dashboard (Agents and Dispatch tabs)
agent-hq --dashboard

# Web dashboard; prints a sign-in token and QR code, and starts the daemon
agent-hq web
```

The commands above must be run inside tmux. `agent-hq web` binds to the
Tailscale address when Tailscale is installed and to 127.0.0.1 otherwise, on
port 8765. `agent-hq-mcp` is a stdio MCP server with tools to list, jump to,
mute and kill agents, and to list, triage and dispatch issues.

### Remote (claude.ai / Duet)

`agent-hq remote serve` exposes a read-only MCP server (`list_agents`, `list_issues`, `automations_health`, `recent_errors`) behind GitHub OAuth for a claude.ai custom connector, so Claude Duet can see your sessions, backlog, automations health and recent errors. With `remote.writes: true` it also serves write tools, including `start_session`, which opens one of the Claude Code sessions you name under `remote.sessions` (for example `daily-review`, prompt `/daily-review`) and never a caller's own prompt. Setup, the data it sends, and the kill switch (`agent-hq remote kill`) are in [docs/remote-connector.md](docs/remote-connector.md).

### tmux Status Bar Integration

Add to your `~/.tmux.conf`:

```bash
set -g status-right "#(agent-hq --status-line)"
```

## Live agent status

Live badges use the harness's native runtime state. Claude is queried with:

```bash
claude agents --settings '{"disableAgentView":false}' --json
```

The setting applies only to that command; your saved Claude configuration is
unchanged. Agent HQ matches each live record's PID and session ID.

Launch Codex in a tmux pane through the runtime wrapper:

```bash
agent-hq codex --                         # new session
agent-hq codex -- resume <thread-uuid>     # resume a session
./run agent-hq codex -- <native arguments> # run this checkout's version
```

The wrapper starts a private app server and the ordinary Codex remote TUI.
Its Unix socket relay records the selected root thread; status queries read
that thread without starting or resuming one. Native model, effort, prompt,
and resume arguments follow `--`. Named profiles (`-p` / `--profile`) and
`--oss` are rejected because the native remote frontend does not forward their
complete configuration. Use explicit `-c` overrides (including `model_provider`
when needed), or bare Codex with Unknown status. Agent HQ's new-chat
and recovery commands use this wrapper. An external worktree launcher or
tmux-resurrect restore command has to be pointed at the wrapper separately.

The daemon collects status independently of discovery, Git, and phase work
on a 0.5-second cadence. Once discovery reports no Claude sessions, Claude
queries pause until discovery finds one again; active-session polling stays fast.
The web watcher checks status every 0.25 seconds.
Observations and the collector heartbeat expire after 1.5 seconds. Missing,
disconnected, stale, unsupported, or ambiguously identified sources display
**Unknown** with a reason. A waiting session reports how long it has waited
(`list_agents`: `waiting 3h12m`, or `waiting_since` in JSON), and a daemon restart
or a source blip does not reset that. Existing Codex sessions launched outside the wrapper
remain Unknown until restarted through it. Hooks, terminal text, and transcript
activity never supply a fallback live badge. Hooks and transcripts still support
phase instructions, summaries, and history.

The target is a native transition reaching the dashboard within two seconds.

When deploying the completed version, install it and restart both the daemon
and persistent web service. `./run install-global`, run from the main worktree
containing that version, performs those steps. For a separately installed
version, use `agent-hq daemon stop`, `agent-hq daemon start`, and
`agent-hq web service restart`; restart manually hosted web/TUI processes too.
These are operational instructions, not an automatic migration of existing
agent sessions.

## Hooks Setup (Optional)

Configure hooks for phase instructions and summaries. They are independent of
the native live-status connection described above.

Add the following to your `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "agent-hq-hook"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "agent-hq-hook"}]}],
    "PreToolUse": [{"hooks": [{"type": "command", "command": "agent-hq-hook"}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": "agent-hq-hook"}]}],
    "SubagentStart": [{"hooks": [{"type": "command", "command": "agent-hq-hook"}]}],
    "PermissionRequest": [{"hooks": [{"type": "command", "command": "agent-hq-hook"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "agent-hq-hook"}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "agent-hq-hook"}]}]
  }
}
```

`agent-hq-hook` writes event metadata to `/tmp/agent-hq/states/`. Existing hook
configuration remains compatible, but its legacy state field does not control
live badges.

### Codex hooks (#206)

Codex CLI (0.144+) supports a Claude-compatible hook system, and Agent HQ ships an installer for it:

```bash
agent-hq hooks install-codex          # merge entries into ~/.codex/hooks.json (backs up any existing file)
agent-hq hooks install-codex --print  # preview the merged JSON without writing
```

The installed entries run `agent-hq-hook --harness codex` on SessionStart,
UserPromptSubmit, PreToolUse, PostToolUse, PermissionRequest, and Stop.
**Codex trusts hooks by content hash**: after installing or editing them, open
Codex and run `/hooks` to trust the entries. Until trusted, hook-based phase
instruction injection and summaries are unavailable.

Trusted hooks inject the `<phase>` instruction via SessionStart
`additionalContext` and provide `latest_summary` from Stop's
`last_assistant_message`, with a rollout-tail fallback. The resolver reads phase
from the transcript. For wrapped sessions, the relay's selected root thread
supplies identity and the transcript path when available.

The Codex hook maintains `~/.codex/session-tags.jsonl` entries shaped as
`{"pid": <frontend-pid>, "session": "<thread-uuid>"}`. Wrapped sessions use
the registered TUI frontend PID, never the app-server PID. Writes are best-effort
and retain the newest 500 entries. Existing tags remain readable.
`tmux-assistant-resurrect` still defaults to a bare `codex resume <uuid>` restore
command: tags alone do not enable the runtime connection after restoration.
Configure its Codex restore command to use `agent-hq codex -- resume <uuid>`.

Hook metadata ignores `codex exec` and subagent rollouts that do not own the
interactive session. Runtime identity is separately validated against the
wrapper registration and process generation. A rollout parser failure affects
phase/summary, not native status.

The `SessionStart` hook also enables **Claude-emitted phase tracking**: for any interactive session in a tmux pane, it injects a small instruction telling Claude to append `<phase>X</phase>` to every assistant message (where X is `adhoc`, `design`, `implement`, `qa`, `review`, `ready`, or `done`). The daemon's resolver then derives the phase by reverse-reading the session transcript for the most recent `<phase>` tag on every poll and feeds it to the dashboard's Phase column — reading the transcript directly (rather than at hook-fire time) means a late tag is picked up on the next tick instead of freezing a stale value (#73). The injection does not depend on the daemon being up (#285): a session that starts during a daemon restart is still tagged. Without the SessionStart hook, phase falls back to the rule-based detector, which defaults a typed feature/bugfix/chore/docs branch to `design` until the agent emits a tag. The instruction survives `/clear` and compaction because `SessionStart` re-fires on those events.

To verify hooks are working:

```bash
# Start a Claude Code session in tmux, then check:
ls /tmp/agent-hq/states/
# You should see a .json file for each active Claude Code pane

# After a hook fires, inspect its revision (not the live badge):
cat /tmp/agent-hq/states/<pane>.json | jq '.revision'
```

State files are ephemeral (in `/tmp`) and cleared on reboot. `SessionEnd` hooks clean up files when Claude Code exits; codex files (no SessionEnd) are cleaned by the daemon's prune instead.

## Crash recovery

A tmux crash, reboot, or OOM can kill every running agent at once. Agent HQ's
in-memory caches live in `/tmp` (wiped on reboot), so to answer *"which agents
were live, and how do I resume each?"* after the fact, the daemon persists a
durable rolling snapshot of the live-agent set to
`~/.config/agent-hq/agent-snapshots.jsonl` (outside `/tmp`, survives a reboot).
A line is appended only when the set changes — an agent added/removed, or any
agent's state / phase / branch moves — so an idle roster doesn't churn the
file, and growth is capped to the most recent 1000 records.

After a crash, run:

```bash
agent-hq recover
```

It picks a **pre-crash baseline** snapshot, diffs it against the agents that
are live right now, and prints each one that's missing, with a ready-to-run
resume command:

```
Baseline: 2026-06-04 18:43:01 (2m ago) — 7 agent(s) recorded (roster collapse 7→1 detected; 2 newer record(s) skipped).

2 agent(s) from the baseline are no longer live:

  agent-hq/feature/125-snapshot-recovery (#125) — was running/implement
    (cd /Users/me/wt/agent-hq.feature-125-snapshot-recovery && claude --resume 0f2c…)

  scratch/main — was idle/—
    no resume id (tool=codex); cwd: /Users/me/scratch
```

The baseline is *not* simply the last snapshot: the daemon keeps polling
while tmux-resurrect restores the survivors, so the last record reflects the
*post-restore* world and a dead agent would vanish from both sides of the
diff. Instead `recover` scans backwards for the most recent sharp roster
collapse (a drop of at least half the roster, ≥ 2 agents, within the last
48 h) and baselines at the last record *before* it, falling back to the last
non-empty snapshot when there's no collapse. The collapse is judged over the
whole run of non-increasing records leading into the drop, not a single
snapshot-to-snapshot step: the daemon polls every ~2 s and agents die over
seconds, so a reboot is recorded as a staircase (`14 → 13 → 13 → 10 → … →
2 → 1`) in which no single step is a cliff (#274). Drops recorded within
5 minutes of the bottom merge into one crash (a record is written within a
tick of the change, so its timestamp is the drop time), and the record
*before* the earliest merged drop is the pre-crash roster however old it is
— an idle roster is deduplicated, so that record is often hours old. (The
window is anchored on the bottom record, so if the daemon itself dies
mid-collapse and only writes the collapsed roster after a long reboot, the
drops recorded before its death fall outside the window and the baseline
is the last pre-death tick — `--baseline N` covers that case.) The
chosen baseline and
the reason are always printed — and when no collapse qualified, the reason
names the largest drop that was seen — so a wrong heuristic pick is
visible. Override it manually with `--since 45m` /
`--since "2026-06-07 14:30"` (newest snapshot at or before that time) or
`--baseline N` (N records back from the latest).

The agent-hq control session itself — a Claude session sitting in agent-hq's
own checkout, or the pane `recover` is invoked from — is excluded from both
sides of the diff: it's never a recovery target and would otherwise mask a
missing agent. The diff compares roster *membership* (by transcript session
id, falling back to `tool:cwd`), never counts.

Add `--json` for a machine-readable list. Claude and codex agents carry a
resume command (`claude --resume <id>` / `agent-hq codex -- resume <uuid>`); other tools
record context (cwd, branch) but can't be resumed this way.

This complements [`tmux-assistant-resurrect`](https://github.com/timvw/tmux-assistant-resurrect):
that plugin restores sessions on a tmux-server restart, while `agent-hq recover`
also catches agents that die *without* a restart and cross-checks what's
actually live against what should be.

## Pinned sessions

When many agents run at once, a high-priority or frequently-checked session
gets shuffled around by the active sort/grouping. **Pinning** anchors it: a
pinned agent floats into a dedicated **📌 Pinned** section at the top of the
list, above every grouping, regardless of the active sort mode (Phase, List,
Kanban). Pinned agents are shown once at the top — they're pulled out of their
normal phase/kanban group, not duplicated — and ordered among themselves by the
usual priority sort (waiting first, then most-recently-accessed).

- **Web:** tap the 📌 button on a card (or **Pin** in the detail panel). The
  card jumps to the Pinned section immediately, with no full reload, on the
  acting tab and every other open tab. Tap again to unpin.
- **TUI (`--dashboard`):** highlight an agent and press **`P`** to toggle the
  pin.

Pins are keyed on the agent's **stable identity** (the Claude transcript
session id, else `tool:cwd:pane_id`) — the same key the web cards use — so a
pin survives a page refresh, a TUI restart, and a tmux pane renumber (for Claude
agents). Pin state is persisted to `~/.config/agent-hq/pinned-sessions.json` and
shared between the terminal TUI and the launchd web service (the file *is* the
cross-surface sync), exactly like `ui-prefs.json`.
Unpinning returns the agent to its natural position in the active sort order.

## Remembered sort order & opening focus

The agents view remembers how you like it sorted (#176). In the TUI
(`--dashboard`), the sort mode cycled with **`s`** (priority / phase /
grouped-by-repo) is saved on every press and restored on the next launch. On
the web dashboard, picking **Phase** or **List** in the toolbar (or visiting
`?view=kanban` directly) is likewise remembered, and opening `/agents` without
a query string restores it. Both live in
`~/.config/agent-hq/ui-prefs.json`, shared between the terminal TUI and the
launchd web service like the pinned-sessions store.

When the TUI opens in **phase** sort, the cursor lands on the agent running in
the same tmux session you launched `agent-hq` from — typically the Claude
session you were just working in — instead of the top of the list. If several
agents share that session, the most recently accessed wins. In other sort
modes, or when no agent matches, the cursor starts at the top as before.

## New session button (web)

The web Agents page has a **+** button in the header for starting a fresh
interactive Claude chat without leaving the browser (#169). Tapping it opens
a small picker:

- **Scratch** — find-or-create a tmux session named `scratch` (each tap adds
  a new window there), rooted at `dispatch.scratch_cwd` (default `$HOME`).
  The MCP `open_chat_session` tool's `scratch` target uses the same session.
- **One entry per configured repo** — opens the new window inside that
  repo's main tmux session (the same find-or-create, path-then-name matching
  that dispatch uses), rooted at the repo's `local_path`.

The new window immediately runs the selected harness's bare REPL — `claude`
(no prompt seeding, default permission mode) or `agent-hq codex --`, per the harness
picker on the same form (see "Dispatch harness selection" below; a blank
selection resolves to the config default) — and the browser jumps you to it
exactly like the existing **⏎ Jump to Terminal** button — including the iOS
Shortcut deep-link branch.
There's no agent card at first (discovery only lists recognized agent
processes); the card appears on the next poll once Claude has started.

## GitHub Dispatch TUI

Open `agent-hq --dashboard` and press `l` for Dispatch. With `triage.data_dir`
configured, the table follows the web Queue: Pinned → Ranked → Untriaged.
Only confirmed records enter Ranked. Complete proposals can show a score while
remaining in Untriaged. SCORE is always visible; RANK breakdowns appear at
terminal widths of 100 columns or more when a store is configured.

| Key | Action |
|-----|--------|
| `F` | Filter in Repo → Src → Status → Eff → Fit order |
| `/` | Search within the active filters |
| `S` | Cycle Rank → Created → Updated |
| `f` | Reverse the active date sort |
| Space, `b`, `u` | Select, group, ungroup issues |
| Enter | Review and dispatch the selection |
| `s`, `d`, `D` | Toggle status, mark Done, or close as not planned |
| `r` | Refresh issues and triage annotations |

Src appears for mixed sentinel/manual backlogs; Eff/Fit require a triage store.
The old numeric priority shortcuts are removed. Triage editing remains on the
web and MCP; the TUI has no triage editor yet.

## Dispatch harness selection

Dispatch (GitHub Dispatch and the **+** new-session
button) can launch a worktree/session with a harness other than Claude.
Configure defaults under `dispatch:` in `~/.config/agent-hq/projects.yaml`:

```yaml
dispatch:
  default_harness: codex
  harnesses:
    codex:
      effort: high
  scratch_cwd: ~/scratch   # where the + button's Scratch session opens
```

`default_harness` picks which harness is pre-selected everywhere a picker is
shown; `harnesses.<name>` supplies that harness's default `effort` (and,
optionally, `model` — a free pass-through string with no allowlist of its
own) when a dispatch doesn't override it explicitly. Any of these can still
be overridden per-dispatch from the web form, the TUI confirm modal, or the
`dispatch_issues` MCP tool.

Non-Claude options (like `codex` above) only ever appear when the installed
`~/.claude/skills/worktree/scripts/launch.sh --capabilities` actually
advertises that harness — an older launcher, or one that fails the probe,
silently degrades every picker to Claude-only.

## Dispatch needs an external launcher

Dispatch and autopilot hand the actual work to tooling that is **not part of
this repository**:

- Dispatch opens a tmux window running `claude` and types `/worktree-create
  <issue numbers>` into it. `/worktree-create` is a Claude Code slash command
  from the author's own configuration. It creates a git worktree and a tmux
  session per issue and starts an agent there.
- Autopilot, and the check for which harnesses can be offered, run
  `~/.claude/skills/worktree/scripts/launch.sh`, the script behind that command.

Neither is published here. In a fresh checkout:

- **Works without them:** discovery, live status, phases, all three front ends,
  the issue queue, triage and hygiene views, automation health, the Errors tab,
  `recover`, and the **+** new-session button (it only starts a plain `claude`
  or `agent-hq codex --` session).
- **Does not work:** dispatching an issue opens the controller window, but
  Claude Code will not recognise `/worktree-create`, so no worktree or agent is
  created. `agent-hq autopilot run` fails at launch. Harness pickers offer
  Claude only, because a missing `launch.sh` is treated as "Claude only" rather
  than as "dispatch unavailable".

To use dispatch you have to supply your own `/worktree-create` command and a
`launch.sh` that answers `--capabilities`; the contract `launch.sh` must meet
is in `agent_hq/capabilities.py` and `agent_hq/autopilot.py`.

## Automation health

The web UI's **Automations** tab shows a health view for launchd-scheduled
automations (e.g. a nightly backup or a morning report) so a failed or
silently-missed run is visible at a glance instead of being discovered by the
*absence* of its usual email. Read-only: agent-hq never starts, stops, or
modifies the jobs.

Each configured automation gets one row: status dot, name, relative last-run
time (absolute on hover), last result, and the next scheduled run. Tapping a
row opens the last 50 lines of the automation's log (ANSI-rendered) in the
detail panel. Statuses:

- **ok** (green) — last run exited 0
- **error** (red) — launchd recorded a non-zero last exit code, or the log's
  last line matched `failure_pattern` / missed `success_pattern`; the log's
  last line is shown on the row
- **missed** (amber) — the most recent scheduled fire time passed (15 min
  grace) without the log being written — catches "the Mac was asleep at 6 AM"
- **unknown** (grey) — nothing observable yet (no log file, no launchd record)

Health is read from what already exists — no changes to the automations
themselves: the job's last exit code via `launchctl print`, its schedule from
the `StartCalendarInterval` of `~/Library/LaunchAgents/<label>.plist` (daily
`Hour`/`Minute` schedules; anything else degrades to exit-code-only health),
and the wrapper's log file for last-run time and the drill-down.

The exit code alone has two blind spots: a wrapper that logs a failure verdict
but exits 0 anyway, and a reboot — launchd forgets the last exit code when it
restarts. `failure_pattern` / `success_pattern` close both: each is a regex
judged against the log's **last line** (where the wrappers write their final
verdict), and a failure match — or a success miss — marks the run **error**
regardless of what launchd remembers. Prefer `failure_pattern` for
long-running jobs: while a run is in progress the last line is interim
output, which a `success_pattern` flags as error until the verdict lands.
A wrapper with neither a reliable exit code nor a stable verdict line needs
to report results itself (see [Unified logging](#unified-logging)).

### Long-running daemons

A KeepAlive daemon has no schedule and its launchd exit code is misleading:
launchd keeps a previous crash's `last exit code` while the restarted process
runs. Give such an entry `stale_after` (a positive duration: `90s`, `3m`,
`2h`) and its health comes from launchd's running state plus log freshness
instead. The daemon is expected to write a line (e.g. a heartbeat) more often
than `stale_after`. Its row shows `running` / `not running` in place of the
next run. Statuses for daemons:

- **ok** (green) — running and the log was written within `stale_after`
- **error** (red) — the log's last line matched `failure_pattern` / missed
  `success_pattern` (a stale non-zero exit code is ignored while running)
- **stale** (amber) — running, but the log has gone quiet past `stale_after`
  (a hung loop)
- **stopped** (red) — not loaded, or loaded but not running

A KeepAlive job with no `stale_after`, such as a server whose log only records
warnings, keeps the scheduled rules. Exit code 15 doesn't count against it, because
that's how launchd stops a job to restart it. Any other non-zero code is an error.
If it has no schedule (no `StartCalendarInterval` or `StartInterval`), it must
still be running: when launchd doesn't report it running, or hasn't loaded it at
all, it reads **stopped**. KeepAlive comes from the plist (`KeepAlive: true`; a
conditional KeepAlive dict doesn't count, since launchd rightly leaves such a job
down at times), or from `.plist.disabled` when the job was killed that way, or
else from launchd. The few seconds after a restart, before launchd brings it
back, don't count, and neither does a `launchctl` that couldn't be read.

A run ended by a signal has no exit code; launchd records the signal instead
(`last terminating signal = Killed: 9`). For any job without `stale_after` it
counts like a non-zero exit: **error**, shown as `error (SIGKILL)`. On a
KeepAlive job SIGTERM is a restart, like exit 15. Any other signal stays an
error once the job runs again, since a job killed over and over (out of memory,
say) is restarted between kills. Daemons with `stale_after` keep their own rules
above.

Configure under `automations:` in `~/.config/agent-hq/projects.yaml`; the
section is hidden when the list is absent or empty:

```yaml
automations:
  - name: nightly-backup
    # Optional; defaults shown:
    label: com.claude.nightly-backup             # launchd label
    log_file: ~/.claude/logs/nightly-backup.log  # wrapper's log
    # Optional, no defaults — regexes judged against the log's last line:
    failure_pattern: "FAILED"             # match ⇒ error
  - name: daily-report
    success_pattern: "Report complete"    # miss ⇒ error
  - name: sync-agent                       # KeepAlive daemon
    failure_pattern: "FAILED"
    stale_after: 3m                       # heartbeat every 60 s
```

## Autopilot

`agent-hq autopilot run` (#302) dispatches a few hand-picked issues
overnight to goal-mode worktree agents, so their PRs are waiting in the
morning. **Autopilot never merges:** each agent's goal stops at an open PR,
and a human merges.

An issue is eligible only when you opt it in with the human-only
**⏵ Autopilot** flag (detail sheet, Triage inbox card, or MCP
`set_triage(autopilot=true)`), its triage record is **confirmed**, it is
open, its repo is allowlisted, it has no existing worktree or branch, and it
has never been attempted. Eligible issues launch in rank order through
`~/.claude/skills/worktree/scripts/launch.sh`, throttled by `per_night` and
`max_open`. Each issue gets one attempt; anything uncertain (a failed GitHub
fetch, a failed worktree check, an unreadable state file) skips rather than
launches, and a failed launch stops the night. State lives in
`<triage data_dir>/autopilot/state.json`.

The **Automations** tab shows an Autopilot card: the last run, Pause/Resume,
the attempts waiting on you (running or with a PR open), and the ones that
need attention, each with a **Retry**. Retry frees the attempt's slot; it
does not stop a running agent, and it does not remove the attempt's worktree
or `feature/<n>-…-ap<id>` branch (shown on the row). Remove those too, or the
issue keeps being skipped as "worktree or branch exists" and cannot launch
again. A night that skipped an opted-in issue because its GitHub fetch or
worktree check failed counts as failed (exit 1, error on the card).

```yaml
autopilot:
  enabled: false        # default false — the hard kill switch
  repos: [owner/name]   # allowlist; each must be a configured repo
  per_night: 2          # default 2 — max launches per run
  max_open: 3           # default 3 — max attempts in flight at once
  turn_bound: 40        # default 40 — goal-mode turn bound
  model: opus           # default "opus" — passed to launch.sh --model
```

```bash
agent-hq autopilot run --dry-run   # print the selection and goal text; writes no state
```

Requires the `triage:` block (`data_dir` must exist) unless autopilot is
disabled. A dry run writes no autopilot state, but it still takes the run
lock (creating `run.lock`) and force-refreshes the GitHub cache. Scheduling is not
part of agent-hq: run `agent-hq autopilot run` from your own nightly launchd
job, and register that job under
`automations:` to watch its health.

## Unified logging

Agent HQ reports its own failures and lifecycle events to the unified
logger (`app=agent-hq`) — the same stream the log sentinel sweeps to
auto-file GitHub issues from `error`-level events. It activates only when
the write token exists at `~/.claude/secrets/logger-write.token`; without
it, events are dropped silently and nothing is queued. Delivery is
best-effort and non-blocking (background threads; the hook binary only
appends to a local queue that the daemon flushes every 5 minutes), with
an offline queue at `~/.cache/agent-hq/logger-queue.jsonl` capped at 500
events (oldest dropped first, trimmed only when a delivery attempt fails or
on a no-network hook append). A logging failure never affects the operation
that emitted it. The client follows the logger's hardening semantics: a
malformed event is quarantined alone to a `.rejected` sidecar rather than
with its whole batch, oversized or invalid fields are repaired before
sending, and for 60 s after a failed delivery each new event is sent on its
own without replaying the backlog (the daemon's periodic flush still
replays it).

## Errors tab

The web UI's **Errors** tab (#148) is the read side of the unified logger:
one place to browse recent `error`/`alert` events across every project that
logs to it, without SSHing anywhere. Rows are grouped by app (newest group
first), with chips to widen the level set (warn/info/debug), switch the
window (1h / 24h / 7d), or focus one app. Tapping a row opens a drill-down
in the detail panel: the full message, timestamp, source, pretty-printed
`json_extra`, and ~10 surrounding rows of that app's stream for context.

The tab lights up only when the logger **admin token** exists at
`~/.claude/secrets/logger-admin.token`; without it the page shows a setup
hint instead. There is no YAML config for this — endpoint and token follow
the same convention as the write side (fixed defaults; `LOGGER_URL` /
`LOGGER_ADMIN_TOKEN_FILE` are test seams, not user settings). The default
view (error+alert / 24h) is cached in-memory for 60 s and served
stale-while-revalidate, so tab switches stay instant.

Relatedly, issues auto-filed by the log sentinel (`sentinel` label) render
in the Dispatch views with distinct **machine-filed / untriaged** styling —
an amber badge and card accent on the web, a flag marker in the TUI table —
so they read as "needs a human look", not planned work (#235). Display
only; they select, group, and dispatch like any other issue.

## Project boards (`repos:` + `projects:`)

Agent HQ sees an issue only when it is an **item on a configured GitHub
Project board** — Queue, Browse, Triage, the AI triage worker and dispatch
all read the boards, never the repos directly. Each repo names its board in
`~/.config/agent-hq/projects.yaml`:

```yaml
repos:
  your-name/widgets:
    local_path: ~/code/widgets
    project: Widgets         # names an entry in projects: below

projects:
  - name: Widgets
    number: 1
    owner: "@me"
```

### New project checklist

GitHub's built-in board workflows are **off by default on a new project and
cannot be enabled through the API** (`gh` has no command for them), so an
issue filed before they are switched on never reaches the board — and
therefore never reaches Agent HQ (#273). After `gh project create`:

1. Open the board on github.com → **⋯ → Workflows**.
2. Enable **Auto-add to project** (pick the repo; the default filter
   `is:issue is:open` is right) and **Item reopened**.
3. Add the repo + project to `projects.yaml` as above.

Issues that slipped through anyway (opened before the toggle, transferred
in, or on a board whose workflow was never enabled) show up in the web
`/issues` **Hygiene** mode under **Not on board**, one card per issue with an
**Add to board…** action (`gh project item-add` + Status → Todo) and a
per-repo **Add all**. Detection is pure local computation — it diffs the
REST issue index against the cached board membership on every hygiene sweep
— so it costs no GraphQL points; only repos with a `project:` are checked.

## Idea Inbox

Free-text → classified GitHub issue. Phone (iOS Shortcut), desktop CLI, or
`/inbox` web page all hit the same `POST /api/idea`. The classifier (Claude
via tool-use) picks one configured `(repo, project)` pair and drafts a
title + body matching the repo's existing tone.

See [docs/idea-inbox/README.md](docs/idea-inbox/README.md) for setup
(YAML config, token rotation, Shortcut steps, PWA install, CLI usage).

## AI triage (Triage inbox)

A headless `claude -p` worker proposes value (1-5) / effort (S/M/L) /
agent-fitness (solo / pair / needs_you) for issues that haven't been triaged
yet, so a human only has to confirm or tweak instead of filling in every
dimension from scratch (#248, Phase 3 of the #245 prioritization redesign).
Requires the `triage:` block already used for the Queue tab's ranking
(`data_dir` at minimum — see the config example below) — the worker needs
somewhere to read and write triage records.

**When it runs:** automatically, off the request's event loop, the first
time any `/issues` container or list load finds an issue that needs a
proposal (a never-triaged issue, or a previously-proposed one whose title,
body, or labels changed) *and* AI triage is available — a triage store is
configured, `triage.ai_enabled` is true, and `claude` is on `PATH`. Runs are
singleflight with a 10-minute failure cooldown, so a broken `claude` can't
loop. You can also trigger a run manually — see `agent-hq triage run` below,
or the **Run now** button in the web Triage tab (disabled with a hint when
AI triage isn't available).

**Config** (`triage:` block in `~/.config/agent-hq/projects.yaml`):

```yaml
triage:
  data_dir: ~/path/to/triage-data-repo
  ai_enabled: true    # default true
  ai_model: opus      # default "opus" — passed to `claude -p --model`; sonnet was ~5x noisier in the 2026-09 eval
  ai_batch_cap: 15    # default 15 — max issues proposed per repo per run
  ai_timeout_s: 300   # default 300 — subprocess timeout per claude -p call
  hygiene_enabled: true   # default true — daily hygiene sweep + Run now (#249)
  hygiene_model: opus     # default: ai_model
  hygiene_timeout_s: 600  # default 600 — one cross-repo claude -p call
```

**CLI:**

```bash
agent-hq triage run --dry-run                              # print proposals, write nothing
agent-hq triage run --repo owner/name --cap 100             # one-shot backfill for a large
                                                              # pool of already-seeded issues
agent-hq hygiene run --dry-run                             # hygiene sweep: print findings, write nothing
```

`--cap` overrides `ai_batch_cap` for that run only. A non-dry-run invocation
seeds/tombstones triage records first (same pass the web container routes
run), so `agent-hq triage run` works as a cold-start backfill even before
the web dashboard has ever loaded `/issues`. Exits 1 on invalid/missing
config or any failed repo.

**Reviewing proposals:** the web dashboard's `/issues` page gets a third
segmented-control mode, **Triage**, after Browse (the strip reads Queue →
Browse → Triage → Hygiene, #268), badged with
the count of proposals awaiting review. Each card shows the AI's proposed
value/effort/fitness plus a one-sentence reason under each dimension. From
there:

- **Confirm** keeps the AI's proposal as-is and flips the record to
  Confirmed (provenance still reads "AI proposed, human confirmed").
- Tapping a different value on any dimension (**adjust**) is a normal human
  edit — it overwrites that dimension and nothing else. The card stays in the
  inbox with your pick active so you can review the other chips; tap
  **Confirm** when the whole row reads right. The same holds in an issue's
  detail sheet from Queue or Browse: the pickers edit, and the "Confirm
  triage" button (enabled once value, effort and fitness are all set) is what
  confirms — so hand triage is three taps plus Confirm.
- **Confirm all (N)** confirms every proposal currently listed (respecting
  the active repo filter) in one shot.
- The row also carries the two human-only flags, **★ Interest** and
  **Pin**, next to the dimension chips (#267) — tap to set, tap again to
  clear — so a whole row can be finished without opening the detail sheet.
  The AI worker never proposes either (its output schema is closed and the
  parser rejects a response that sets them). Interest is written at once.
  A pin tapped on an inbox row is *staged* (stored on the record as
  `pin_staged`, so it survives a refresh and the detail sheet notes it) —
  the issue only floats into the Queue's pinned strip once the row is
  confirmed, by its own **Confirm** or by **Confirm all**, which fold the
  staged value into the real pin. The Queue's and detail sheet's own Pin
  toggles are unchanged: they pin immediately, whatever the record's
  triage state, and an AI proposal never moves a pinned issue.

Confirmed and tombstoned (closed-issue) records are never re-proposed —
once a human has judged an issue, or it's closed, the worker leaves it
alone.

**Operational note:** `claude` must be resolvable on `PATH` for whichever
process runs the web service — the persistent-hosting launchd plist already
puts `~/.local/bin` on `PATH` (see [Persistent hosting](#persistent-hosting-run-as-a-service)),
which is typically where `claude` lives.

### From a chat session (MCP)

`list_issues()` returns the ranked open backlog as text, including score,
breakdown, pin/interest flags, triage state, and GitHub status. Use
`list_issues(format="json")` for structured records, dimensions, and reasoning.
Filters: `repo` (name substring), `status`, `effort` (`S`, `M`, `L`, `none`),
`fitness` (`solo`, `pair`, `needs_you`, `none`), and `state`
(`untriaged`, `proposed`, `confirmed`). Dimension/state filters require a store.

`set_triage(issue_key="owner/repo#123", value=4, effort="M", fitness="pair")`
writes only the supplied fields with human provenance; optional `interest` and
`pin` booleans set those flags. **Edits never confirm.** Call
`confirm_triage(issue_key="owner/repo#123")` to explicitly approve the currently
stored dimensions once all three are set. Confirmation preserves provenance.
Both tools write the local store; neither writes GitHub Priority. The legacy
GitHub Priority field remains read-only input for seeding hints.

## iOS: "Jump to Terminal" opens a Shortcut

On desktop, the web dashboard's **⏎ Jump to Terminal** button switches the
tmux client to the agent's pane and brings Ghostty forward. On iPhone/iPad
there is no local terminal, so the same tap instead:

1. still switches the tmux pane server-side (so a phone terminal that
   attaches over SSH right after sees the tapped agent), skipping only the
   Ghostty raise on the Mac, and
2. opens an iOS **Shortcut** via `shortcuts://run-shortcut?name=…`.

You supply the Shortcut — typically "open my terminal app" or a
"Run Script over SSH" step that runs `tmux attach`. By default the button
looks for a Shortcut named **`Agent HQ Terminal`**; create one with that
exact name and the button works with zero config. To rename or disable it,
add a `web:` block to `~/.config/agent-hq/projects.yaml`:

```yaml
web:
  ios_jump_shortcut: "Open Blink"   # custom Shortcut name
  # ios_jump_shortcut: null         # disable the iOS branch entirely
```

If the named Shortcut doesn't exist on the device, iOS shows its own
"shortcut not found" error. Android/other mobile OSes are unaffected (they
get the desktop behavior).

## Persistent hosting (run as a service)

By default `agent-hq web` runs in the foreground and dies when you close the
terminal. To keep the dashboard always available — reachable from your phone
over Tailscale and installable as a home-screen app — run it as a macOS
**launchd LaunchAgent**. The agent runs in your login session (so it can see
your tmux server), restarts on crash (`KeepAlive`), and starts at login
(`RunAtLoad`). macOS-only.

**Prerequisite:** run `./run install-global` first so `agent-hq` is on your
PATH — the LaunchAgent points at the absolute executable path.

```bash
agent-hq web service install     # write the plist + load it
agent-hq web service status      # loaded? running? log paths + URL
agent-hq web service restart     # force-restart (launchctl kickstart -k)
agent-hq web service uninstall   # unload, remove the plist, stop the daemon
```

- **Plist:** `~/Library/LaunchAgents/com.benralston.agent-hq.web.plist`
- **Logs:** `~/Library/Logs/agent-hq/web.out.log` and `web.err.log`

**Bind / security.** `install` bakes the detected Tailscale IPv4 into the
plist (so the dashboard is reachable only over your tailnet, never on the
local LAN — important on untrusted Wi-Fi). If Tailscale isn't up yet at
login, the bind fails and launchd relaunches the job ~10s later, so it
self-heals once Tailscale is ready. If your tailnet IP ever changes, re-run
`install`. Pass `--bind 0.0.0.0` to listen on all interfaces instead (still
token-gated, but a wider surface). Every route is gated by the `0o600` token
file — never expose port 8765 on a public IP or via port forwarding.

**Staying awake.** The service does not manage sleep; keep the Mac awake
yourself (e.g. Amphetamine). It's reachable only while the machine is awake
and on the tailnet.

## Requirements

- macOS (launchd, `launchctl` and Ghostty are assumed by the service, automation-health and jump features)
- Python 3.10+
- tmux (the CLI and TUI run inside it)
- fzf (optional; only the bare `agent-hq` command uses it)
- git (for repository/worktree detection)
- `gh`, signed in, for the issue features
- `claude` on `PATH` for AI triage and the hygiene sweep

## Development

```bash
./run build      # uv sync --extra dev
./run test       # pytest; isolated from live tmux, $HOME and /tmp state
./run lint       # ruff
./run lint-fix
```

To run the app against invented data instead of your own sessions, see
[demo/README.md](demo/README.md). It lists what the demo redirects, what it
refuses to run, and what it does not cover.

## Supported Agents

- Claude Code (`claude`)
- OpenAI Codex (`codex`)
- Aider (`aider`)
- OpenCode (`opencode`)
- Gemini CLI (`gemini`)
- Cursor (`cursor`)
- Roo (`roo`)

## License

MIT. See [LICENSE](LICENSE).
