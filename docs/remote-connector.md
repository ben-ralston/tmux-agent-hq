# Remote connector: agent-hq for claude.ai / Duet (#396, #397, #412, #414, #429)

`agent-hq remote` serves an MCP server so Claude Duet (and claude.ai chat) can see your coding sessions, backlog, automations health and recent errors, for example "3 sessions are waiting on you" or "the nightly backup failed" in the morning brief. It is **read-only by default**. With `remote.writes: true` (#397) it can also start sessions, set triage and mute sessions, (#414) file, comment on and close GitHub issues, and (#429) start a named Claude Code session from a list you keep in config; see [Writes](#writes-397).

It is a claude.ai **custom connector**, a remote MCP server that Anthropic's cloud reaches over HTTPS. It is a separate connector from notes-graph's (`connector.example.com`). The two have separate sign-ins, tunnels, LaunchAgents and kill switches, so you can switch one off without the other.

```
claude.ai / Duet ──HTTPS──▶ Cloudflare (WAF) ──tunnel agent-hq-mcp──▶ 127.0.0.1:9894
                                                                     agent-hq remote serve
                                                                     ├─ OAuth (GitHub sign-in, pinned to your id)
                                                                     └─ /mcp: list_agents, list_issues, automations_health,
                                                                        recent_errors (+ writes when remote.writes)
```

## What it exposes

| Tool | Reads | Never |
|---|---|---|
| `list_agents` | The daemon's `/tmp/agent-hq/cache.json`, plus the live status overlay. Each Claude session carries its Remote Control link (`open:` / `remote_control_url`) so you can answer it from your phone; `include_muted` lists muted ones too | No tmux, ps, git or `capture-pane`: no live discovery at all |
| `list_issues` | The GitHub board cache and the local triage store, in Queue order | Never forces a refresh, never seeds triage |
| `automations_health` (#412) | Each job in `automations:`: status (ok, error, missed, unknown, stale, stopped), why it is unhealthy, last and next run, and the last log line. A job also notes how many logger errors arrived in the last hour with its name as their app or source; this never changes its status, because another copy of the job can send them. `job=<name>` adds that job's last 50 log entries, with lines that differ only in their timestamps collapsed into a count and the run's start time (a span that can hide gaps; a run cut off by the read shows `+` and no start), reaching back up to the last 64 KB of the log (it says when older lines weren't read). Same rules as the Automations tab, cached 30 s | Never the launchd label or the log file's path |
| `recent_errors` (#412) | Error and alert rows from the unified logger over 1h, 24h or 7d, grouped by app with counts and the newest message. `app=<name>` lists that app's newest 50 rows with `json_extra`. Same query as the Errors tab's default, cached 60 s per window. A full page of 500 is reported as `500+ (logger cap)`. When the logger is down it serves the last page with a notice | Never another level, never the admin token or its path |

- **`list_issues` refreshes the cache** only when it is older than the 300 s TTL, and only above the `github.graphql_floor` budget. That is about 24 GraphQL points per refresh, at most about 12 refreshes an hour (one per TTL), however often Duet calls.
- **`refresh_issues` is not exposed.** It forces a refresh past the TTL and past the budget gate.
- **Writes are off unless `remote.writes: true`.** Then eight write tools are served (below): four stdio wrappers, three remote-only GitHub issue tools, and `start_session` (#429). Never remote: `kill_agent`, `jump_to_agent` (focuses a pane on the Mac), `open_chat_session` (a free prompt; `start_session` is its allowlisted remote counterpart), `set_status`, `confirm_triage` and `refresh_issues`.
- **New stdio tools must be classified before the server will start.** `agent_hq/remote/tools.py` puts every stdio tool in `REMOTE_READ`, `REMOTE_WRITE` or `NOT_REMOTE`. If a new stdio tool is in none of them, `serve` and `check-config` refuse to start. Tools with no stdio twin sit in `REMOTE_ONLY_READ` (the two health tools, always served) and `REMOTE_ONLY_WRITE` (the issue tools and `start_session`).

### Writes (#397)

Turn on with `writes: true` in the `remote:` block, run `./run install-global` (or `launchctl kickstart -k gui/$(id -u)/com.benralston.agent-hq.remote`), then **reconnect the connector in claude.ai**: it refreshes the tool list only on reconnect. `check-config` prints `writes: on|off`. Turning it back off removes the tools on the next restart; `agent-hq remote kill` still stops everything.

| Tool | Does | Guards |
|---|---|---|
| `dispatch_issues` | Starts a `/worktree-create` session on any open issue on your boards, the same path as web dispatch. One repo per call. `model` must be a plain model id. | Refused when the issue already has a live session (its number on a live branch in that repo) or a leftover worktree. Also refused when it was dispatched in the last 10 minutes and may still be starting: a slow start reports failure but can still run, and an attempt with no logged result counts. Refused when the daemon is down. No caps (your call). |
| `start_session` (#429) | Starts one of the Claude Code sessions listed under `remote.sessions`, by name (for example `daily-review`): a new window in the `scratch` tmux session running `claude --name <name> -- <prompt>`, the same launch path as the **+** button. It returns the session's `@name` and its Remote Control link when the daemon has seen it within about 12 s; otherwise it says to look in `list_agents`. When a Claude session with that `@name` is already open it starts nothing and returns that session's name and link, saying it is the existing one. See [Named sessions](#named-sessions-429). | The caller sends a name and nothing else: the prompt, folder, model and effort come only from config. A name that isn't on the list is refused with the allowed names; with no list, everything is refused. A name with a live session never opens a second one. Before the first start is visible, the same name within 10 minutes is refused (a slow start and an attempt with no logged result both count). Refused when the daemon is down or the folder is missing. |
| `set_triage` | Sets value / effort / fitness, plus one optional sentence of reasoning, applied at once with `duet` provenance. The record is confirmed once all three are set and none is still the AI's; a partial edit of an AI proposal stays in the inbox. | Duet may overwrite your own values (your call), but it's always labelled `duet`, never `human`. Without reasoning, the old reasoning of the changed dimensions is cleared. `interest`, `pin` and `autopilot` aren't settable; a pin you staged in the inbox commits on the confirm, as it would on yours. The AI worker never overwrites a Duet dimension. |
| `mute_agent` / `unmute_agent` | Hides or restores a session in your views (mute is sticky, #303), by `session_id` (the Claude transcript id or Codex thread id) or exact `@name`. | Exact matches only. A set, not a toggle, so a repeated mute stays muted. An unreadable mute-state file is a refusal, never a fake success. No capture-pane. |
| `create_issue` (#414) | Files an issue in a repo from `projects.yaml`, with a `Filed by Claude Duet via agent-hq` footer. It lands on that repo's board with Status Todo and gets an untriaged triage record. The board is refetched once if the GraphQL budget is above the floor; otherwise `list_issues` catches up within a TTL. | Only configured repos. Labels must already exist in the repo (never created); `sentinel` and `deep-audit` are reserved. Refused when an open board issue in that repo has the same normalized title (case, spacing and trailing punctuation ignored), or the same title was filed in the last 10 minutes. An off-board or Done issue with the same title isn't caught. A failed board add or Todo write still reports the issue as filed (`partial`), and the hygiene off-board check flags it. |
| `comment_issue` (#414) | Comments on an open issue, with a `Claude Duet via agent-hq` footer. | Only configured repos, and only open issues. That is checked over REST, so issues that aren't on the board work too, and a PR is refused. A failed check is a refusal. The same comment on the same issue within 10 minutes is refused. |
| `close_issue` (#414) | Closes an open issue as `completed`, `not_planned`, or `duplicate` of another issue in a configured repo (recorded natively, like `gh issue close --duplicate-of`), with a required closing comment and the same footer. It leaves `list_issues` at once. | No guard on sentinel or deep-audit issues, or on issues with a live session (your call: the claude.ai **Ask first** rule is the gate). The same open-issue check as comments. Closing the same issue again within 10 minutes is refused. |

#### Named sessions (#429)

`start_session` lets a Duet page (or Duet itself) run something on the Mac that isn't tied to an issue, such as a saved prompt you run every morning. It can only start what you list:

```yaml
remote:
  writes: true
  sessions:
    daily-review:
      prompt: /daily-review
      cwd: ~/scratch        # optional, absolute or ~/...; default dispatch.scratch_cwd
      # model: opus         # optional; a plain model id
      # effort: high        # optional; low, medium, high, xhigh or max
```

- **One session per name.** While a session named `daily-review` is open, another `start_session("daily-review")` returns that session's link (logged with code `existing`) and starts nothing. Close the session, or rename it, to start a fresh one. A session you named `daily-review` by hand counts too.
- **Names** are lowercase letters, digits, `-` or `_`, up to 40 characters. The name is also the session's display name (`claude --name`), so it reads `@daily-review` in `list_agents`.
- **The prompt** is sent as the session's first message, so `/daily-review` runs that skill. Up to 800 bytes once shell-quoted, where each apostrophe counts as five (the line is typed into a terminal, which drops the rest of a longer one; put longer instructions in a skill and name it here); newlines are fine, other control characters are refused at load time (the launch line is typed into a shell).
- **Permissions.** The session starts in Claude Code's default permission mode, like the **+** button's. When it asks for something, answer it from the Remote Control link.
- **First run in a folder.** Claude Code asks you to trust a folder the first time a session opens there, and the prompt waits behind that dialog. Open a session in the folder once by hand before relying on it.
- `check-config` prints the allowed names. A change needs a server restart (`./run install-global`), like `writes`; adding a name doesn't change the tool list, so no reconnect is needed for that.
- In claude.ai, keep `start_session` on **Ask first**.

**Issue writes are public.** Titles, bodies and comments Duet writes go to GitHub, and Duet may draft them from mail or web content. The instructions tell it to use its own words and never paste private text. Anything that later reads these issues (dispatch prompts, the AI triage worker) already treats issue bodies as data. In claude.ai, set the three issue tools to **Ask first**.

A GitHub call that times out may still have landed, so it reports `uncertain`. Like a `partial` create, it counts toward the 10-minute repeat guard. So does a close GitHub refused (`close_failed`), because gh posts the closing comment before it closes.

**Action log.** Before any write runs, an `attempt` line is appended to `~/.config/agent-hq/remote/actions.jsonl` (file 600, newest 2000 lines kept). **If that append fails, the write is refused.** A `result` line follows with the outcome. The full outcome text (which can name a pane) stays in that file.

**In the Errors tab.** Each write also emits an info row `remote action` carrying only the tool, issue key(s), repo, close reason and duplicate target, or session name, the `start_session` name, dimensions, ok, and a fixed code (`ok`, `partial`, `not_found`, `not_open`, `invalid`, `not_allowed`, `start_failed`, `existing`, `duplicate`, `launch_uncertain`, `uncertain`, `close_failed`, `daemon_down`, `log_unavailable`, `failed`, `crashed`). Never an issue title, body, comment or label. Every field is emitted only in its strict shape (deny by default; ASCII, GitHub's own owner/repo/number limits), and identifiers (issue keys, repo, duplicate target, session, `start_session` name) only on outcomes reached after the call resolved them (`ok`, `partial`, `uncertain`, `close_failed`, `not_open`, `duplicate`, `launch_uncertain`, `start_failed`, `existing`); any other code drops them. So free text Duet put in one stays local. Pick the info level in the Errors tab to see them. A log failure is a warn row, `remote action log unavailable`.

### Data that leaves the Mac

**Sent to Anthropic, when Duet or claude.ai calls a tool:**
- session names, including ones Claude Code generated from a session's first prompt;
- repo, branch and worktree names;
- native status, what a session is waiting for, and since when;
- phase;
- issue keys, titles, labels and board status;
- triage value, effort and fitness, plus the AI's one-sentence reasoning for each;
- opaque session ids;
- each Claude session's Remote Control URL (`https://claude.ai/code/session_…`, which only your claude.ai account can open);
- with writes on: the result text of each write (issue keys and URLs, triage values, `@name`s), never pane ids or paths;
- automation and logger text, redacted (below): job names, schedules, statuses, last log lines, a job's last 50 log entries on request (at most the last 64 KB of its log), and each app's error and alert messages, sources and `json_extra`. **This is full detail for every job and app:** only the patterns listed under redaction are removed, so anything else a job logs is sent as written. Check what your jobs log before you turn the connector on. Log text can quote email, since some jobs log agent output about mail, so the connector's instructions tell Claude to treat it as data.

**Written to GitHub (#414), with writes on:** the issue titles, bodies, labels and comments Duet composes. These come from Duet, not from the Mac.

**Never sent:** pane contents, transcripts, session summaries, prompts, cwd paths, issue bodies, PR content, launchd labels, log file paths, tokens.

**Redaction (#412).** `agent_hq/remote/redact.py` runs on the Mac over the whole record of every `automations_health` and `recent_errors` call, before any formatting, so nothing unredacted reaches the text or JSON. It strips terminal escapes and control characters, then removes:
- tokens and API keys: known prefixes (`sk-`, `gh*_`, `github_pat_`, `xox*-`, `AKIA`, `AIza`, JWTs), PEM blocks, and opaque runs of 32 or more characters where a segment over 12 characters mixes letters and digits;
- any value named like a secret, numeric ones included (`token=`, `api_key:`, `password=`, `--token x`, `session=` and so on, and any `json_extra` key with such a name). A list or object under such a name goes through its closing bracket, across lines, or to the end of the text if it never closes. Counts such as `input_tokens: 2000` stay;
- `Authorization` and `Cookie` headers and `Bearer` values;
- query strings on any URL or path (`/cb?code=x`, `127.0.0.1:9894/cb?code=x`), plus URL fragments and userinfo, including a password holding an unencoded `/`, `?` or `#`;
- account numbers: 8 or more digits in a run, in hyphen or dot groups (`acct-1234-5678-9012`, `123-45-6789`, `Z12-345678`), or in 3 or more spaced groups, keep their last four (`****9012`). Dates, IPs, UUIDs and hex ids stay;
- home directories, shortened to `~`.

Text is capped only after redaction, so a cut can't split a secret past its pattern. A job's tail is redacted as one text across everything read, so a multi-line secret can't straddle the 50-entry cut. A value whose end can't be found masks to the end of the text, so one odd line can hide newer lines until 64 KB more has been logged; the tool says so when that happens. Rows and tail entries are dropped, oldest first, until the result fits the 150k limit as the server measures it. If the redactor fails, the tool fails with a fixed error and sends nothing. It only runs on these two tools; the web tabs are unchanged.

**The limit:** this is pattern matching, tuned to over-mask (it also masks git SHAs, request ids and epoch numbers). A secret in an unknown format with no sensitive name beside it can pass when it is shorter than 32 characters, all letters, or split into short hyphenated parts. `tests/test_remote_redact.py` pins each rule and what must survive; the leak sweep in `tests/test_remote_health.py` plants a secret of every class in every field.

**Local request log.** `~/Library/Logs/agent-hq/remote-requests.log` records only method, path (never the query string, which carries OAuth codes), status, tool name, decision and GitHub login. It never records arguments or results. It is capped at 5 MB, with one rotated copy.

## Security model

Vendored from notes-graph PR #168, which had four review rounds plus a hardening round:

- **Sign-in.** GitHub sign-in is accepted only when both the login AND the numeric account id match `remote.allowed_github_id`. It fails closed.
- **Consent page.** Before GitHub, our own consent page runs. It names the client, needs a form POST with a CSRF value matching a `SameSite=Strict` cookie, and can't be framed. A `SameSite=Lax` flow cookie ties the GitHub callback to the browser that approved.
- **Trust on first use.** The first connector client to sign in is pinned; any other client gets a refusal page. `allow-client` admits a re-added connector.
- **Tokens.** Tokens are opaque and stored only as SHA-256 hashes, in `~/.config/agent-hq/remote/tokens.json` (dir 700, file 600). Access tokens last 1 h, and refresh tokens rotate. A replayed code or refresh token revokes its whole token family. `revoke-all` bumps an epoch that cancels sign-ins already in flight.
- **Registration limits.** `/register` is capped at a 16 KB body and 10 registrations a minute. Stored client fields are length-capped, and clients holding live grants are never evicted.
- **Network.** The server binds only 127.0.0.1, with DNS-rebinding protection. uvicorn ignores `X-Forwarded-*`. Results over 150k characters are refused with a "narrow it" hint.
- **Tool errors.** Claude gets a fixed message naming only the exception type, never its text (which can carry local paths). Bad arguments are reported as such and are not logged as a fault.

**Vendored files.** `agent_hq/remote/auth.py` and `request_log.py` are verbatim copies of notes-graph's files (`agent_hq/remote/vendor.py` names the commit), with only `vendor.SUBSTITUTIONS` applied. `tests/test_remote_vendor.py` reverses the substitutions and checks the upstream sha256, so a hand edit fails CI. notes-graph pins the same hashes in its own tests, so changing those files there fails its suite until this copy is updated. `scripts/check-remote-vendor.sh` diffs the pinned commit against notes-graph `origin/main`.

**Errors.** Faults reach the unified logger through eventlog (role `remote`):

| Level | Event |
|---|---|
| error | `remote config or secret failed to load` |
| error | `remote server exited with an error` |
| error | `remote server: unhandled exception` |
| error | `remote GitHub token exchange failing repeatedly` |
| error | `remote token reuse detected` |
| warn | `remote sign-in denied: identity not allowed` |
| warn | `remote tool failed` |
| warn | `remote action log unavailable` |
| warn | `remote logger read failed` (`recent_errors`; carries only the error type) |
| info | `remote mcp started` |
| info | `remote action` (one per write, #397) |

Each event carries metadata only.

## Setup (one-time, by hand)

1. **GitHub OAuth App**: create it under github.com → Settings → Developer settings → OAuth Apps.
   - Name it "agent-hq remote".
   - Homepage: `https://agents.example.com`.
   - Callback: `https://agents.example.com/oauth/github/callback`.

   It has to be a separate app from notes-graph's, because the callback is on a different host. Generate a client secret.

2. **Keychain**: put the secret in the login Keychain. The command prompts for it, so it never lands in shell history:
   ```bash
   security add-generic-password -s agent-hq-mcp-remote -a github-client-secret -w
   ```

3. **Config**: add this to `~/.config/agent-hq/projects.yaml`:
   ```yaml
   remote:
     public_url: https://agents.example.com
     allowed_github_login: your-login
     allowed_github_id: 12345678   # gh api user --jq .id
     github_client_id: <the OAuth App's client id>
     # port: 9894
     # writes: true   # #397/#414: dispatch / triage / mute / issue writes from Duet (default off)
     # sessions:      # #429: what start_session may open; see "Named sessions"
     #   daily-review:
     #     prompt: /daily-review
     #     cwd: ~/scratch
   ```
   - `host` is fixed at 127.0.0.1, and a `github_client_secret` key is refused. Both are rejected at load time.
   - Then check it with `agent-hq remote check-config`. It prints the URL, the tools and the Automations entries.

4. **Tunnel**: a separate tunnel, not a second ingress on notes-graph's.
   ```bash
   cloudflared tunnel create agent-hq-mcp
   cloudflared tunnel route dns agent-hq-mcp agents.example.com
   ```
   Then write `~/.cloudflared/agent-hq-mcp.yml`:
   ```yaml
   tunnel: <tunnel uuid>
   credentials-file: /Users/you/.cloudflared/<tunnel uuid>.json
   no-autoupdate: true
   ingress:
     - hostname: agents.example.com
       service: http://127.0.0.1:9894
     - service: http_status:404
   ```

5. **WAF** (Cloudflare → example.com → Security → WAF → custom rule, action Block):
   ```
   (http.host eq "agents.example.com"
    and (http.request.uri.path in {"/mcp" "/token" "/register" "/revoke"}
         or starts_with(http.request.uri.path, "/.well-known/"))
    and not ip.src in {160.79.104.0/21})
   ```
   - Only Anthropic's range reaches the API paths.
   - `/authorize`, `/oauth/consent` and `/oauth/github/callback` stay open, because your browser uses them.
   - Don't put Cloudflare Access in front (it blocks Claude).
   - Check that Bot Fight Mode doesn't 403 Anthropic.

6. **LaunchAgents**:
   ```bash
   agent-hq remote service install            # com.benralston.agent-hq.remote
   agent-hq remote service install --tunnel   # com.benralston.agent-hq.remote-tunnel
   agent-hq remote service status
   ```
   - They run the **global** `agent-hq` binary, like the web service. `./run install-global` restarts the server when it is loaded.
   - No TCC grants are needed.

7. **Automations tab**: paste the two `automations:` entries that `check-config` prints into `projects.yaml`.
   - The server entry uses `stale_after: 20m` against its 5-minute heartbeat.
   - The tunnel entry mirrors notes-graph's.

8. **claude.ai**: Settings → Connectors → Add custom connector, URL `https://agents.example.com/mcp`.
   - Sign in with GitHub.
   - Approve the consent page. It names the client id, so only approve right after clicking Connect yourself.
   - Then enable the connector in Duet.

9. **Check it**: ask Duet "how many sessions are waiting on me?", then look at `~/Library/Logs/agent-hq/remote-requests.log`.

## Operations

| Command | What it does |
|---|---|
| `agent-hq remote kill` | **Kill switch.** Boots out the tunnel agent, then the server agent, then any stray `agent-hq remote serve`. Renames both plists to `*.plist.disabled` so a reboot doesn't bring them back. Exits 1 if a job is still loaded afterwards. Never touches notes-graph's agents or other cloudflared tunnels. Undo with `service install` (both). |
| `agent-hq remote revoke-all` | Wipes every token and code and clears the client pin. Claude must sign in again. Works even when `projects.yaml` is broken or has no `remote:` block. A sign-in or token exchange already in flight stores nothing: every grant write checks the revocation epoch under the store lock (notes-graph #192). |
| `agent-hq remote allow-client <id>` | After re-adding the connector in claude.ai (which registers a new client), allows the id shown on the refusal page. |
| `agent-hq remote check-config` | Validates the config and the Keychain secret, and prints what is exposed. |
| `agent-hq remote service status` | Shows whether both LaunchAgents are loaded and their PIDs. |

**Logs** are in `~/Library/Logs/agent-hq/`:
- `remote.log`: launchd stdout/stderr, WARNING and above, with library messages redacted, plus the heartbeat.
- `remote-requests.log`
- `remote-tunnel.log`

**claude.ai quirks:**
- Re-adding a connector registers a new client, so it needs `allow-client`.
- The tool list refreshes only on reconnect.
- Tool calls time out at about 240 s, and results over about 150k characters fail.

**Rotating the GitHub secret:** replace it in the Keychain, then run `agent-hq remote revoke-all` and `launchctl kickstart -k gui/$(id -u)/com.benralston.agent-hq.remote`.
