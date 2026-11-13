# Idea Inbox

A single "throw an idea at it" surface that classifies free-form text into the
right repo + project and files a GitHub issue. Items land **untriaged** (no
priority set) with an optional `needs-triage` label for low-confidence
classifications.

## Architecture in 30 seconds

- **API:** `POST /api/idea` on the existing `agent-hq web` FastAPI app. Same
  cookie auth as the rest of the web UI; non-cookie clients (Shortcut, CLI)
  use `Authorization: Bearer <token>` against the same token file.
- **Classifier:** two Anthropic calls per idea — (1) tool-use enum picks one
  of the configured `(repo, project)` pairs with a self-rated confidence
  score, (2) drafts a title + body using the chosen repo's last ~10 issues as
  a tone seed.
- **Filing:** `gh issue create` → `gh project item-add`. Priority is left
  unset so new items show up untriaged; labels `idea-inbox` (and
  `needs-triage` for low-confidence) are auto-created on first use per repo.
- **Surfaces:** iOS Shortcut on phone (Action Button → dictate → notification),
  `agent-hq idea` CLI on desktop, `/inbox` web page for both.

## Configuration

In `~/.config/agent-hq/projects.yaml`, add `project:` (and optional
`description:`) to each repo you want eligible for idea routing:

```yaml
repos:
  ben-ralston/agent-hq:
    local_path: ~/Programming/agent-hq
    project: Agent HQ
    description: "CLI tool to monitor and dispatch AI coding agents in tmux."
  ben-ralston/scratch:
    local_path: ~/scratch
    project: Scratch
    description: "Catch-all for ideas that don't yet have a home."

projects:
  - name: Agent HQ
    number: 12
    owner: "@me"
  - name: Scratch
    number: 15
    owner: "@me"

# Optional — sensible defaults are fine.
idea_inbox:
  model: claude-sonnet-4-6        # Anthropic model
  confidence_threshold: 0.75      # Below this → needs-triage label
  anthropic_api_key_env: ANTHROPIC_API_KEY
  # Optional: fetch the key on demand when the env var is unset (e.g. under
  # the launchd service, which doesn't inherit your shell). argv list — its
  # stdout is used as the key, so it never has to be written to disk.
  # anthropic_api_key_command: [security, find-generic-password, -a, <you>, -s, agent-hq-anthropic-key, -w]
```

A repo without `project:` is silently skipped by the classifier — it stays a
valid agent-hq repo, just not an Idea Inbox candidate.

The key is resolved in this order: the `anthropic_api_key_env` env var first
(so interactive terminal runs work), then `anthropic_api_key_command` if set.
For an interactive shell, export it:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

For the **persistent launchd service**, the env var isn't available (launchd
doesn't inherit your shell), so set `anthropic_api_key_command` to a fetch
command. Use the **macOS login keychain**. Store the key once, granting
`/usr/bin/security` read access:

```bash
security add-generic-password -a "$USER" -s agent-hq-anthropic-key \
  -T /usr/bin/security -w   # prompts for the key; nothing lands in shell history
```

then point the config at
`[security, find-generic-password, -a, <you>, -s, agent-hq-anthropic-key, -w]`.
The login keychain is unlocked while you're logged in, so the service reads the
key without a prompt, even when the capture comes from your phone and the Mac
is unattended. Avoid a password-manager CLI here (e.g. Dashlane's
`[dcli, n, …]`). Once its vault auto-locks, each fetch waits for the master
password on the Mac, so phone captures fail until someone types it (#300). If a
fetch does fail, the error shown on the phone says to unlock it on the Mac.

The resolved key is cached in the web process's memory for its lifetime, so the
command runs once per process start, not per idea. It runs again only if the
Anthropic API rejects the key (HTTP 401): the capture re-fetches once and
retries. So after rotating the key, the service keeps using the old one until
you revoke it (the next capture then picks up the new one) or restart the
service (`agent-hq web service restart`). The key never touches disk or the
plist.

## Auth + token rotation

The web app already creates a token file at `~/.config/agent-hq/web-token`
on first run. Both cookie and Bearer auth verify against this same file.

| Action | Cookies | Bearer (Shortcut/CLI) |
|---|---|---|
| `touch ~/.config/agent-hq/web-token` | invalidated | preserved |
| `echo $(openssl rand -hex 32) > ~/.config/agent-hq/web-token` | invalidated | invalidated |

To rotate the bearer token (and force every Shortcut + CLI to re-auth):

```bash
echo "$(openssl rand -hex 32)" > ~/.config/agent-hq/web-token
```

After rotating, paste the new value into the iOS Shortcut and update
`~/.config/agent-hq/idea-cli-token` (CLI) — see below.

## Phone surface — iOS Shortcut

The Shortcut runs `Dictate Text` → `Get Contents of URL` → `Show Notification`.

Build it once (Shortcuts app → "+", iPhone or Mac):

1. **Dictate Text** — set Language to your preference, "Stop Listening"
   when "After Pause".
2. **Get Contents of URL**:
   - URL: `https://<your-tailnet-host>:8765/api/idea`
   - Method: `POST`
   - Headers:
     - `Authorization` → `Bearer <paste your token>`
     - `Content-Type` → `application/json`
   - Request Body: JSON
     - `text` → `Dictated Text` (variable)
     - `idempotency_key` → `Random Number` from `1` to `9999999999` as text
       (close enough to a UUID for our purposes)
3. **Get Dictionary Value** — `issue_url` from the previous step's JSON output.
4. **Show Notification** — body: `Filed: <Dictionary Value>`. Title: `Inbox`.

Bind it: Settings → Action Button → Shortcut → pick this one. Or Back-Tap, or
Lock Screen widget — whatever feels lowest-friction.

The token is stored inside the Shortcut. Treat it like the file it copies
from: rotate by overwriting the token file, then paste the new value in.

## Computer surface — `agent-hq idea` CLI

```bash
agent-hq idea "track agent state better"     # one-shot
agent-hq idea                                  # opens $EDITOR
echo "..." | agent-hq idea                     # stdin
```

The CLI reads its bearer token from `~/.config/agent-hq/web-token` (the same
file the web app uses) — no separate setup. It posts to
`http://127.0.0.1:8765` by default; override with `--port` if you've changed
the bind port.

## Web surface — `/inbox`

The same FastAPI app serves a tiny `/inbox` page: a textarea + Send button,
plus a "Recent ideas" list pulled from the `idea-inbox` label across all
configured repos (cached 30s in-process).

Each entry has a Re-route picker — pick a different `(repo, project)` pair
and the issue is moved via `gh issue transfer` and re-added to the new
project. This is the correction path when the classifier picks the wrong
target.

### Sent-message log (#140)

Sending is **optimistic**: the compose box clears immediately and a
"Sending…" bubble shows the message until the classify round-trip finishes
(success folds it into the Recent list; warnings/needs-triage keep a
dismissible notice). Every non-empty submission — from the web page, the
CLI, or the iOS Shortcut — is also recorded durably in
`~/.config/agent-hq/inbox-sent.jsonl` (append-only JSONL, capped to the
newest 500 lines, path injectable via `SentMessageStore(path=...)` /
`create_app(sent_store=...)`).

An **in-flight** send also survives navigating away: the Recent list renders
it as a dimmed "Sending…" row until the outcome lands (the page that owns
the live bubble hides its duplicate row client-side). A send that **fails**
(classifier/GitHub error) or is **interrupted** (the server died
mid-request; detected as a pending record older than 120s)
survives a page reload: it renders in the Recent list with the error, a
**Restore** button (puts the raw text back in the compose box) and a
**Dismiss** button (`POST /api/idea/sent/{id}/dismiss`; dismissal is
terminal — a late zombie write can't resurrect the row). Callers that omit
`idempotency_key` get a server-generated record id; callers that send one
get retry-supersede semantics for free, because the key doubles as the
record id.

To pin the dashboard as a PWA on iOS:
1. Open `https://<your-tailnet-host>:8765/agents` in Safari.
2. Share → "Add to Home Screen".

There is **one shared Web App Manifest** (`/static/manifest.json`) for the
whole app; it opens the PWA to `/agents` (the dashboard), without browser
chrome. The Idea Inbox is one tap away via the bottom nav. For always-on
hosting so the PWA is reachable without a terminal open, see the
"Persistent hosting" section in the top-level [README](../../README.md).

## Failure modes

- `401` — bad/missing token.
- `422` — empty `text` field.
- `502` — classifier upstream error (no issue filed; the attempt is recorded
  in the sent log and shown as a failed row on `/inbox`).
- `503` — `ANTHROPIC_API_KEY` unset (or whatever `idea_inbox.anthropic_api_key_env`
  points at). Web app keeps running.
- `200` with `warnings: [...]` — issue filed, but a follow-up step (project
  add, priority set) failed. Surface the warnings; the issue exists in the
  repo, just may need manual project assignment.

## Threat model

Same as the rest of `agent-hq web`: Tailscale-only. Both the cookie HMAC and
the bearer token live in the same file the user owns at mode `0o600`. Don't
expose `agent-hq web` on a public IP.
