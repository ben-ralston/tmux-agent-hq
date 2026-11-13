# Demo data

Scripts that run Agent HQ against invented data, used for the screenshots and
recordings in the main README. Nothing here is imported by the application, and
nothing in `agent_hq/` was changed to support it.

Agent HQ normally reads live state: your tmux server, `~/.claude`,
`~/.config/agent-hq`, `/tmp/agent-hq`, `gh` and `launchctl`. The demo replaces
each of those:

| Real input | Demo replacement | How it reaches child processes |
|---|---|---|
| `$HOME` (`~/.claude`, `~/.config/agent-hq`, logs, LaunchAgents) | `<root>/home` | `HOME` |
| Your tmux server | A separate server under `<root>/tmux` | `TMUX_TMPDIR`; a foreign `TMUX` is dropped |
| The installed `agent-hq`, `agent-hq-hook`, `agent-hq-mcp` | `bin/agent-hq` and friends, which run this checkout inside the demo | `PATH` |
| `gh` | `bin/gh`, which answers reads from `data.py` and refuses writes | `PATH` |
| `launchctl` | `bin/launchctl`, which answers `print` only | `PATH` |
| `pgrep`, `pkill`, `osascript`, `tailscale`, `security`, `cloudflared` | `bin/_standin`, which does nothing | `PATH` |
| git over the network | refused | `GIT_ALLOW_PROTOCOL=file` |
| `/tmp/agent-hq` (daemon cache, PID file, hook state) | `<root>/runtime` | an import hook, installed in every Python child through `PYTHONPATH` (`site/sitecustomize.py`) |
| Signals to other processes | delivered only to the demo's own processes | the same hook wraps `os.kill` |
| Agent processes | `sleep`, started through symlinks named `claude` and `codex` | |
| Claude Code's session registry and transcripts | Files written by `seed.py` | |
| The log service | `fake_logger.py` on 127.0.0.1 | `LOGGER_URL` |

`<root>` is `demo/.state` unless `AGENT_HQ_DEMO_ROOT` is set. `_isolate.py`
sets all of this up.

The runtime directory is the one input the app has no setting for; its paths
are module constants. Patching them in one process is not enough, because the
app starts `agent-hq` again in new processes: the dashboard's Restart runs
`agent-hq daemon stop` and `start`, and the fzf picker's key bindings run
`agent-hq --list`. That is why the demo puts its own `agent-hq` first on
`PATH` and installs the path hook in child interpreters too. Without both, a
Restart in the demo would stop a real Agent HQ daemon on the same machine.

## Run it

```bash
export AGENT_HQ_DEMO_ROOT=/tmp/hq-demo-acme   # shows up in the UI, so keep it neutral
export TZ=UTC

uv run python demo/seed.py                                   # stage everything
(cd demo && uv run python fake_logger.py 9913 &)             # Errors tab data
uv run python demo/hq.py web --bind 127.0.0.1 --port 9911    # dashboard + daemon
```

The web command prints the sign-in token; open
`http://127.0.0.1:9911/login?token=<token>`.

For the CLI and TUI, open a shell inside the demo tmux server and run
`demo/hq.py` there with the same two variables set:

```bash
tmux -S $AGENT_HQ_DEMO_ROOT/tmux/tmux-$(id -u)/default new-session -s hq \
  -e AGENT_HQ_DEMO_ROOT=$AGENT_HQ_DEMO_ROOT -e TZ=UTC
uv run python demo/hq.py --list
uv run python demo/hq.py --dashboard
```

`uv run python demo/drive.py` changes the staged sessions' status over about
twenty seconds, which is what the dashboard recording shows.
`uv run python demo/run.py script.py` runs any Python script inside the demo
environment.

Stop the web server and the logger with Ctrl-C, then:

```bash
uv run python demo/seed.py --stop    # stops the demo daemon and tmux server
```

`--stop` reads the demo's PID file and signals that process only if its command
line is the demo's, so a stale file cannot stop something else.

## Check the isolation

```bash
uv run python demo/check_isolation.py
uv run python demo/hq.py --print-isolation   # the paths and programs in effect
```

The check starts no daemon, server or tmux session. It asks fresh processes,
started the ways the app starts them (a new interpreter, the executable Restart
would run, `agent-hq` by name from a shell), which paths and programs they
would use, and fails if any is outside the demo root. It also checks the
refused commands, the stand-ins, the signal guard and the stale-PID case.
`tests/test_demo_isolation.py` runs it in the test suite.

## What is and is not enforced

Enforced, and covered by the check:

- Every runtime path, in the launcher and in processes it starts, is under the
  demo root.
- Restart, the fzf key bindings and the Codex wrapper run the demo's
  `agent-hq`, not an installed one.
- `service`, `remote` and `hooks install-codex` commands are refused.
- GitHub writes, `launchctl` changes, `pkill`, Keychain reads and git network
  access are refused; the web server binds to 127.0.0.1 unless you pass
  `--bind`.
- No signal reaches a process that is not the demo's or a child of the caller.

Not enforced:

- A process that clears the environment, or runs Python with `-I` or `-S`,
  loses the hook and the `PATH`. Nothing in the app does this.
- Anything you run yourself in a demo pane with an absolute path to a real
  `agent-hq` gets the demo's paths (through the hook) but is otherwise your
  real install.
- The demo does not stop you pointing `--bind` at a public interface.

## Limits

- `TZ=UTC` is there because the Automations tab's "ago" column reads a local
  time as UTC, so it is wrong by the UTC offset in any other zone.
- AI triage, the hygiene sweep and the Idea Inbox classifier are switched off
  or not exercised: they call `claude -p` or the Anthropic API. The Triage and
  Hygiene tabs show records written by `seed.py` through the same store classes
  the workers use.
- Dispatch hands off to a launcher that is not in this repository, so in the
  demo it can only open its controller window. Kill, jump and new-session act
  on the demo tmux server; "jump" cannot raise a terminal window.
- The Codex session shows Unknown status, as any Codex session started outside
  `agent-hq codex --` does.
