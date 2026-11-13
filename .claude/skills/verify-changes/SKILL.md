---
name: verify-changes
description: Verification toolkit for agent-hq. Use when validating a change before save, before merge, or when a general verify step defers to the project. A decision matrix maps the shape of the diff to the relevant phases — pick the minimum set that exercises the actual risk surface. Not a fixed sequence.
---

# Agent HQ Verification Toolkit

A **toolkit**, not a script. Pick the phases that match what changed; over-running
everything dilutes signal. Two parts ARE fixed: always run Phase A first (cheap floor),
and always clean up what you started.

**Golden rule: verify against the demo instance, never a real installation.** Agent HQ
reads live machine state (tmux, `~/.claude`, `~/.config/agent-hq`, `/tmp/agent-hq`, `gh`,
`launchctl`). `demo/` stages an invented copy of all of it and runs this checkout's code
against that copy; `demo/README.md` explains what is replaced. Every command below goes
through `demo/hq.py`. A bare `agent-hq` on `$PATH` is whatever was installed last, runs
against real sessions, and may be stale code.

## Setup (once per verification)

```bash
export AGENT_HQ_DEMO_ROOT=/tmp/hq-demo-verify TZ=UTC
uv run python demo/seed.py                                   # repos, tmux sessions, transcripts, stores
(cd demo && uv run python fake_logger.py 9913 &)             # only for the Errors tab
SOCK=$AGENT_HQ_DEMO_ROOT/tmux/tmux-$(id -u)/default
dtmux() { tmux -S "$SOCK" "$@"; }                            # the demo tmux server, never the default one
```

The CLI and TUI must run inside tmux, so they run in a pane of the demo server:

```bash
dtmux new-session -d -s vc -x 130 -y 40 -c "$(pwd)" \
  -e AGENT_HQ_DEMO_ROOT=$AGENT_HQ_DEMO_ROOT -e TZ=UTC
run_in_pane() { dtmux send-keys -t vc "clear; $*" Enter; sleep "${WAIT:-6}"; dtmux capture-pane -p -t vc; }
```

## What changed → recommended phases

| Diff shape | Run |
| --- | --- |
| discovery / enrichment / grouping / display (core pipeline) | A + B |
| models / resolver / phase / transcript / hooks | A + B (+ hook-state spot check below) |
| dashboard.py / screens/* (Textual TUI) | A + C |
| web routes / templates / static / watcher / state | A + D (+ E when the change is visual or interactive) |
| dispatch.py / github.py / filters / config | A + D issue checks (execute-dispatch only in `full`) |
| service.py / automations.py | A + D `/automations` (the demo's stand-in `launchctl` answers `print`); service install/uninstall is not verifiable in the demo |
| snapshots / recover | A + B with `recover --json` |
| daemon.py / restart.py / anything that spawns or signals a process | A + I, then F's Restart check in `full` |
| remote/ (the claude.ai connector) | A + `uv run python demo/run.py .claude/skills/verify-changes/scripts/remote_health_probe.py` (calls `automations_health` and `recent_errors` through the real tool boundary on demo data and checks the output for unredacted text). The write tools are covered by `tests/test_remote_writes.py`; `remote serve` is refused by the demo launcher. The other three scripts in `scripts/` read a real installation's config, daemon cache and GitHub boards (with every write redirected), so they cannot run on the demo and are for that installation's owner only |
| demo/ | A + I + B + D |
| Docs / comments / test-only | A only (name the skip category) |

## Mode argument

- (default) → pick phases from the matrix for the current diff
- `full` → also run Phase F (creates tmux windows and restarts the demo daemon)
- `quick` → A only
- Explicit list like `A C D` → run only those

---

## Phase A — Floor (always)

```bash
./run test    # pytest, fully mocked subprocess; isolated from live state (tests/conftest.py)
./run lint    # ruff
```

The suite is mocked and path-isolated, so it cannot catch: real tmux interaction, live
GitHub API shape changes, launchd behaviour, or anything visual. Phases B–F cover what the
demo can; the rest is listed under "What the demo cannot verify".

## Phase I — Isolation check (when process spawning, signals or demo/ changed)

```bash
uv run python demo/check_isolation.py
```

Starts no daemon, server or tmux session. It asks fresh processes, started the ways the
app starts them, which paths and programs they would use, and fails if any is outside the
demo root. `tests/test_demo_isolation.py` runs the same check in the suite.

## Phase B — CLI smoke (demo tmux server, read-only)

```bash
run_in_pane "uv run python demo/hq.py --list"
run_in_pane "uv run python demo/hq.py --tree"
run_in_pane "uv run python demo/hq.py --status-line"
run_in_pane "uv run python demo/hq.py --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)), \"agents\")'"
run_in_pane "uv run python demo/hq.py recover --json | head -5"
```

Assert on the captured text: six agents (five `claude`, one `codex`), three repos, no
traceback. The Codex session shows Unknown status because it is not started through the
wrapper; that is expected.

Hook-state spot check (when hooks/resolver/phase changed):

```bash
ls $AGENT_HQ_DEMO_ROOT/runtime/states/ && python3 -m json.tool $AGENT_HQ_DEMO_ROOT/runtime/states/0.json | head -20
```

## Phase C — TUI smoke (tmux-driven)

The dashboard is a fullscreen Textual app: `capture-pane` sees exactly one viewport, which
is what you want here. The `vc` session above is wide enough for all columns and the
preview.

```bash
WAIT=9 run_in_pane "uv run python demo/hq.py --dashboard" | head -30   # Agents tab: headers, six rows
dtmux send-keys -t vc l; sleep 4; dtmux capture-pane -p -t vc | head -30   # Dispatch tab: Pinned / Ranked / Untriaged
dtmux send-keys -t vc j j        # row nav
dtmux send-keys -t vc F          # chip-filter palette (Dispatch tab)
dtmux send-keys -t vc Escape
dtmux send-keys -t vc q          # quit
```

Assert on captured text, not absence of errors: tab headers, table column titles, at least
one agent/issue row, the filter bar when chips are active. Textual consumes Enter and Tab
at the widget level, so a binding needs this real-key smoke, not only a unit test.

## Phase D — Web endpoint smoke (curl + Bearer)

```bash
uv run python demo/hq.py web --bind 127.0.0.1 --port 9911 &
for i in {1..20}; do curl -s -o /dev/null http://127.0.0.1:9911/healthz && break; sleep 0.5; done
AUTH="Authorization: Bearer $(cat $AGENT_HQ_DEMO_ROOT/home/.config/agent-hq/web-token)"
```

Route sweep (200 and a marker each):

```bash
for path in /agents "/agents?view=list" "/agents?view=kanban" "/issues?mode=queue" \
            "/issues?mode=browse" "/issues?mode=triage" "/issues?mode=hygiene" \
            /automations /inbox /errors; do
  echo "$path -> $(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" "http://127.0.0.1:9911$path")"
done
curl -s -H "$AUTH" http://127.0.0.1:9911/agents | grep -c 'data-card-key'                       # 6 cards
curl -s -H "$AUTH" "http://127.0.0.1:9911/issues/container?mode=queue" | grep -c 'filter-chip'  # chip filters present
```

- Unauthenticated and garbage-cookie requests must redirect (302), never leak content and
  never 500: `curl -s -o /dev/null -w '%{http_code}' -H 'Cookie: agent_hq_token=%ff%fe' http://127.0.0.1:9911/agents`.
- htmx partials are curl-able too, useful when the diff touched a partial template:
  `/agents/list`, `/agents/phase`, `/agents/{slug}/card`, `/issues/container`, `/issues/list`.
- `/dispatch` is a legacy path that redirects to `/issues`; test the `/issues` routes.
- Do not POST to `/issues/dispatch`, `/agents/new`, `/agents/{slug}/kill` or
  `/system/restart` outside Phase F.

## Phase E — Browser smoke at phone viewport (visual/interactive changes)

The web UI is mobile-first: **verify at about 390 px width** with a real emulated viewport
(headless Playwright with `viewport: {width: 390, height: 844}`, or a DevTools device
emulation). Resizing a desktop browser window does not go below roughly 500 px, so a
"390 px" test done that way silently runs at desktop layout.

1. Server up as in Phase D. Sign in by opening `http://127.0.0.1:9911/login?token=<TOKEN>`.
2. Poke what the diff touched. Standard sweep:
   - **/agents** (phase view is default): cards grouped by phase, tap a card → detail
     sheet with Live and History, pin toggles without a full reload, + opens the
     new-session picker (do not confirm outside `full`).
   - **/issues**: chip filters, free-text search in Browse, select → group → ungroup,
     selection survives a filter change, Triage cards, Hygiene cards.
   - **/automations**: rows render; tap → log tail in the detail slot.
   - Nav bar scrolls horizontally at 390 px — all five tabs reachable.
3. Screenshot when the change is visual; check the console for JS errors.

To watch status changes arrive live, run `uv run python demo/drive.py` with `/agents` open.

## Phase F — Actions that change state (`full` mode only, demo only)

These create tmux windows and restart processes. In the demo they act on the demo tmux
server and the demo daemon. Never run them against a real installation unless its owner
has asked for exactly that.

- **New-session button:** POST `/agents/new` (Scratch) → assert a new window exists in the
  `scratch` session of the demo tmux server (`dtmux list-windows -t scratch`). It runs the
  demo's stand-in `claude`, a `sleep`.
- **Restart:** POST `/system/restart` → the page lists `daemon stop`, `clear snapshot
  cache`, `daemon start`; `$AGENT_HQ_DEMO_ROOT/runtime/daemon.pid` changes; `/agents` shows
  six cards again.
- **Dispatch execute:** opens the controller window only. The worktree launcher it hands
  off to is not part of this repository, so the hand-off itself is not verifiable here.

## Always — cleanup + report

```bash
lsof -ti :9911 :9913 | xargs kill 2>/dev/null   # the demo web server and fake logger
uv run python demo/seed.py --stop               # the demo daemon and demo tmux server
```

Report `Verification PASSED / FAILED / SKIPPED — <why>`, listing per phase what was
exercised AND what was not. A bare Phase-A pass is never "validated" — say so if that's
all that ran. Don't report on phases you skipped as if they ran.

```
Agent HQ Verification: PASSED
  A floor:      5255 passed / lint clean
  B CLI:        --json 6 agents, --list/--tree/--status-line clean
  C TUI:        dashboard rendered, tab cycle + F palette OK (130x40)
  D web:        10 routes 200 w/ Bearer, 302 without; issue chips present
  NOT covered:  Phase E browser (change was non-visual), Phase F (not full mode)
```

## What the demo cannot verify

Say so in the report when the change touches one of these:

- Real launchd: service install/uninstall/restart, and automation health from a real
  `launchctl print`.
- Real GitHub: API shape changes, rate-limit behaviour, and every write.
- Real agents: Claude Code's own session registry and Codex's app server. The demo writes
  registry files in the documented shape and has no wrapped Codex session.
- AI triage, the hygiene sweep and the Idea Inbox classifier, which call `claude -p` or
  the Anthropic API.
- The worktree launcher that dispatch and autopilot hand off to.

---

## Bugs this skill was shaped by

Seed findings here as they're caught so the recipe evolves with the codebase.

- **Stale-install smoke (recurring)** — smoke tests run against a bare `agent-hq`
  silently exercised a previously installed checkout; changes "didn't work" live. Running
  this checkout through `demo/hq.py` exists for this.
- **Demo Restart reached the real daemon** — the demo redirected paths inside one
  process, and Restart re-ran `agent-hq` in a new one, which would have stopped a real
  daemon on the same machine. The demo now puts its own `agent-hq` first on `PATH` and
  carries its isolation into child processes; Phase I checks it.
- **Viewport-fake mobile testing** — resizing a browser window left the layout
  desktop-width; mobile regressions passed. Phase E requires real viewport emulation.
- **Bindings that never fired (#64/#86 era)** — Textual consumed Enter/Tab at the
  widget level; unit tests passed, the real TUI ignored the key. Phase C drives the real
  keys through tmux.
- **Empty-projects TUI crash (#170 audit, AHQ-004)** — `_apply_refresh` arity mismatch
  crashed the whole dashboard when projects.yaml had no `projects:` block; invisible to
  the suite because the test screen no-ops `_refresh_data`. Lesson for Phase C: smoke
  the TUI at least once against a config shape that differs from the usual one.
- **Crafted-cookie pre-auth 500 (#170 audit, AHQ-013)** — a non-ASCII
  `agent_hq_token` cookie 500'd in AuthMiddleware before auth. Phase D includes the
  no-auth and garbage-cookie checks (expect 302, never 500).
- **Jump false-timeout (#170 audit, AHQ-010)** — the jump route's 2.0s ceiling was
  below the ~2.1s terminal-activation script; successful jumps toasted "Jump timed out."
  Timing-sensitive paths need a live exercise on a real installation, not just route tests.
