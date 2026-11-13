# Baseline security scan — 2026-10-03 (#263)

This was the first `claude-security` plugin scan of Agent HQ. Every verified finding was fixed on `feature/263-security-scan`, so no follow-on issues were filed and no finding was accepted as-is.

## Scan

| | |
|---|---|
| Tool | `claude-security` plugin 0.12.0, scan-codebase job |
| Revision | `b0dc6fb8a3d2` (`feature/263-security-scan`, clean tree) |
| Shape | Whole repository, `medium` effort, attack-surface focus |
| Verification status | `verified` (three-lens panel, 2-of-3 votes to survive) |
| Result | 8 findings: 0 critical, 0 high, 1 medium, 7 low |

**Coverage.** All 439 tracked files were accounted for. 14 components were researched: the web routes, templates and frontend, dispatch/tmux, the idea inbox, MCP, triage/hygiene, the GitHub layer, codex/status/hooks, transcripts/state, the TUI, and the repo automation. 46 of 46 researchers returned, and a breadth sweep and a secrets pass found nothing more. These areas were skipped on purpose:
- vendored minified JS, fonts, icons and theme CSS
- `tests/`
- `docs/`
- `issues/`
- `prototypes/`
- `terminal_screenshots/`

The panel lowered F2 and F8 from MEDIUM to LOW. Everything came from reading the code; nothing was executed.

The raw report (Markdown, JSONL, SARIF and the revision stamp) stayed local in `CLAUDE-SECURITY-20261003-135421/`, behind the plugin's own `.gitignore`. It is not committed.

## Findings and dispositions

| ID | Sev | Surface | Where | Finding | Decision | Commits |
|---|---|---|---|---|---|---|
| F1 | MED | Web: agent pane preview and automation log drill-down | `agent_hq/web/routes/agents.py` `ansi_to_html` | The one-pass OSC strip could splice the bytes around a removed inner sequence into a new OSC-8 link. ansi2html turned that into a raw `<a href>`, which was rendered with `\|safe` (stored XSS). | Fixed | `40399e1`, `791096d`, `8aa89b7` |
| F2 | LOW | Codex wrapper | `agent_hq/codex_runtime.py` `run_codex` / `_write_receipt` | Only `codex-runtimes/` was owner-checked, never its world-creatable parent `/tmp/agent-hq`. The receipt was written through a fixed `registration.tmp` name. | Fixed | `a028bef`, `b243364`, `2d738c3` |
| F3 | LOW | Web token | `agent_hq/web/auth.py` `TokenManager.ensure` | The token was written at umask permissions and only then chmodded to 0600. | Fixed | `640dfa3` |
| F4 | LOW | Daemon | `agent_hq/daemon.py` `stop_daemon` / `write_pid` | SIGTERM went to whatever PID the shared `/tmp` file named. The PID write followed symlinks. | Fixed | `a028bef`, `b243364`, `50c9f1b`, `b92db2b` |
| F5 | LOW | Web: every cookie-authenticated POST | `agent_hq/web/auth.py` `AuthMiddleware`, `agent_hq/idea/handler.py` | The only CSRF defence was SameSite=lax, which a same-site origin bypasses. Reroutes acted on any `src_repo`. | Fixed | `253487d`, `d815716` |
| F6 | LOW | Hooks | `agent_hq/hooks.py` `_atomic_write` | State files, which carry each session's last assistant message, were 0644 in a 0755 shared directory. | Fixed | `a028bef`, `b243364`, `8f8dfa2`, `6c28b08`, `b471c50` |
| F7 | LOW | TUI | `agent_hq/screens/personal_dispatch_screen.py` | Personal titles and list names were parsed as Rich markup. | Fixed | `7df7c4e` |
| F8 | LOW | Idea inbox | `agent_hq/idea/classifier.py` `draft`, `agent_hq/github.py` | Any author's issue bodies were pasted unfenced into the drafting prompt. | Fixed | `1582538`, `c84846b`, `3a4b63c` |

## What changed

- **F1.** After the OSC strip, one linear pass drops every ESC not followed by `[`. Afterwards every surviving ESC opens a CSI/SGR sequence, so ansi2html can never see an `ESC ]` link introducer however the input nests. The first attempt repeated the strip until the string stopped changing. A background commit review flagged that loop as quadratic on deep nesting and as still mirroring ansi2html's parser, so it was replaced.
- **F2, F4, F6: shared root.** These three share one cause: nothing checked who owned `/tmp/agent-hq`. The new `agent_hq/runtime_dir.py` handles both sides:
  - **Writers** go through `ensure_dir`, which makes the root a 0700 directory owned by this user and refuses a symlink or a foreign owner.
  - **Readers** call `check_trusted`, which ignores the data under a root that is symlinked, foreign-owned or group/world-writable. This covers processes that only read, like the web watcher, TUI and MCP; a review of the first version found that gap.
  - The error subclasses `FileNotFoundError`, so every existing "file missing" handler degrades to empty. The paths themselves are unchanged.
- **F2, F4, F6: per-site fixes.**
  - The codex receipt is written via `mkstemp`.
  - `stop_daemon` signals a PID only if its command line is an agent-hq Python process. That accepts any entry point, because the daemon is forked from `daemon start`, `web serve` or `--dashboard`.
  - The PID file is written via temp file plus rename.
  - Hook state files are always 0600.
- **F3.** The token is created with `os.open(O_CREAT|O_EXCL, 0o600)`, and a fresh config directory gets 0700.
- **F5.** The auth middleware rejects cookie-authenticated unsafe methods unless `Sec-Fetch-Site` is `same-origin` or `none`. Browsers without that header fall back to `Origin` matching `Host`. Requests with neither header, and bearer-token clients (the iOS Shortcut and the CLI), are unaffected. `IdeaHandler` reroutes now require `src_repo` to be a configured repo.
- **F7.** Titles and list names are escaped with `rich.markup.escape`, as the Dispatch screen already did.
- **F8.** Tone examples are fetched with `gh issue list --author @me` and wrapped in a nonce-named `<style_examples_…>` fence, with a system instruction never to copy instructions, commands or links from them.

### Follow-ups from code review (round 1 of `/multi-review`)

- **F1.** Drop non-CSI escape sequences *whole* (`ESC ( B` from `tput sgr0`, `ESC 7`, a trailing ESC). Removing only the ESC byte left `(B` visible in the log drill-down.
- **F4.** `stop_daemon` fails closed when `ps` cannot answer: it keeps the PID file and signals nothing, rather than orphaning a live daemon.
- **Root fix.** The hook's state directory is created only inside its error handler, so a hostile root degrades the hook instead of making it traceback. The refusal message now names a remedy that works on a sticky `/tmp`: admin removal or a reboot.
- **F5.** A request that carries a valid bearer token is authorized even when its cookie fails the origin check.
- **F8.** The examples fence is named with a per-request random nonce, so no example body can close it early. Escaping one spelling of the tag would still have left a parser differential.

Waived after review:
- **PID-reuse check in `is_daemon_running`.** This is pre-existing behavior, and the private root already closes the attacker path.
- **Moving the runtime root to a per-user directory.** The owner chose to keep the same path and harden it.
- **Atomic-write and ownership-check dedup refactors.**
- **Caching the root check per process.** macOS's periodic `/tmp` cleanup would reopen the swap window.

Round 2 (Codex and `/code-review` on the fix files) found nothing.

### Recommendation parts deliberately not taken

- **F2: peer-credential check in the codex relay, and a per-user runtime directory.** With the 0700 owner-verified root, no other user can reach the sockets. Relocating the state would have touched external consumers (the hook binary and the tmux status line) for no added protection.
- **F5: rejecting non-JSON `Content-Type` in `_read_json`.** The origin check already blocks the forged `text/plain` request, and leaving the content type alone avoids breaking existing clients.

## Re-running

Open a Claude Code session in the repo, run `/claude-security`, and pick **Scan codebase**. Scans are nondeterministic, so running them periodically builds coverage over time. To scan only a branch's diff, use the **Scan changes** job.
