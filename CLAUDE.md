# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

All commands use the `./run` wrapper:

```bash
./run build      # Install in editable mode with dev dependencies
./run test       # Run tests with pytest (supports additional args: ./run test -k discovery)
./run lint       # Check code with ruff
./run lint-fix   # Auto-fix lint issues
./run agent-hq … # Run the worktree's agent-hq (e.g. ./run agent-hq web --bind 127.0.0.1 --port 9876)
```

**Important:** when smoke-testing changes to a worktree, ALWAYS launch
the server via `./run agent-hq …`. The bare `agent-hq` on `$PATH` is the
globally-installed tool and is pinned to whichever worktree was last
`install-global`'d — it will not pick up edits made in any other
worktree, so the browser ends up testing stale code.

## Architecture

Agent HQ is a CLI tool that monitors AI coding agents (Claude, Codex, Aider, etc.) running in tmux panes.

### Data Flow

```
# Agent monitoring (fzf mode)
discover_agents() → enrich_agent() → group_by_worktree() → format_for_fzf() → run_fzf()

# Agent monitoring (dashboard mode)
discover_agents() → enrich_agent() → build_table_rows() → AgentDashboard (Textual)

# Dispatch flow
load_config() → fetch_project_issues() → build_issue_rows() → DispatchScreen
                                                              → select/group → classify → confirm → execute_dispatch()
```

### Module Responsibilities

| Module | Purpose |
|--------|---------|
| `discovery.py` | Find agents via tmux pane enumeration and process tree walking |
| `enrichment.py` | Add cwd, git context, and agent state detection |
| `grouping.py` | Group agents by repository and worktree |
| `display.py` | Format output for fzf, tree view, JSON, status line |
| `dashboard.py` | `AgentDashboard(App)` — live entry point for `--dashboard`. Hosts the agent table, installs the Dispatch screen, owns shared keybindings; injects the configured triage store into Dispatch |
| `actions.py` | Jump to pane, kill agent |
| `cli.py` | Argparse entry point |
| `models.py` | Data classes: `Agent`, `AgentType`, `AgentState`, `RepoGroup`, `WorktreeGroup`, `Issue`, `Priority`, `Complexity`, `PhaseSnapshot`/`PhaseFeedback`/`InputsObserved`; `replaces()` snapshot-comparison rule. `Agent.phase` is a read-only property over `phase_snapshot.effective_phase`. Harness-generic identity (#205 Stage 1B): `SessionMetadata` (`harness`, `stable_id`, `native_uri`, `display_name`) built by `Agent.session_metadata` (claude: `claude_transcript_session_id` / bridge URL; codex: `codex_thread_id` serves stable_id + `codex://threads/<uuid>`); `Agent.stable_key` prefers `session_metadata.stable_id` for both harnesses, falling back to `legacy_stable_key` (`tool:cwd:pane_id`) only when absent. Serialization keeps a compat window — `to_dict`/`from_dict` and snapshot records emit both the old fields (`claude_session_id`, `session_id`) and the new (`harness`, `stable_id`, `codex_thread_id`); on read, the new field wins when both are present and disagree. `Agent.native_name`/`native_name_source` (#287) carry the Claude session-registry `name`/`nameSource` (`enrichment.get_claude_native_name`, registry-only — no transcript `agentName` fallback, since only the registry name is `@`-addressable). `Agent.agent_pid` identifies the frontend process; `status_snapshot` is an immutable `LiveStatus` with backend `status_label`/`status_reason` presentation properties. |
| `hooks.py` | Phase instruction injection, summary/event metadata, and Codex resurrection tags. Legacy hook state never controls live badges. Wrapped Codex tags use the registered frontend PID and selected root thread; other hooks require interactive-rollout coherence. Tags preserve the existing format, but the external tmux-resurrect restore command still has to be pointed at the wrapper separately. |
| `status_types.py` | Immutable `LiveStatus` observations and defensive serialization, independent of the Agent model. `waiting_since` (#415) is optional: a bad value costs that field, not the observation. |
| `waiting_clock.py` | `WaitingClock` (#415): when each session entered native `waiting`, stamped by the collector onto `LiveStatus.waiting_since`, so `apply_live_status` hands it to every reader (the remote `list_agents` included). `waiting-since.json` sits beside the status snapshot, keyed by pid with the session id inside: a new process or session is a new wait. Only a seen idle/running clears an entry (Unknown keeps it, so a blip can't reset an overnight wait); dead pids are dropped. Claude's undocumented registry `statusUpdatedAt` is the start whenever a read supplies it (measured: it moves on status changes, not on other writes to the file), which also catches a wait that ended between polls or while the daemon was down; the CLI fallback has no stamp and keeps the tracked value. Codex has no such record, so a wait that ends and restarts unseen looks continuous there. |
| `runtime_status.py` | Bounded read-only native queries and backend status mapping for Claude and Codex. Claude reads the `~/.claude/sessions` registry first (`read_claude_registry`, `CLAUDE_SESSIONS_DIR`), falling back to `claude agents --json` (#289). |
| `status_collector.py` | Independent daemon collection, atomic status snapshot, identity/freshness validation, and the shared live overlay. |
| `codex_runtime.py` / `codex_relay.py` | Private app-server/TUI lifecycle, transparent relay, and explicit selected-root-thread registration for `agent-hq codex -- …`. |
| `codex_hooks.py` | `agent-hq hooks install-codex` — merge-preserving, idempotent installer for `~/.codex/hooks.json` (backup + `--print`); prints the trust-by-hash instructions (`/hooks` in codex — an untrusted/edited hook silently stops, V9) |
| `triage_store.py` | Triage records for the prioritization redesign (#245/#246): value 1-5 / effort S-M-L / agent-fitness solo\|pair\|needs_you / human-only interest flag, per-dimension `ai`/`human` provenance (AI upserts never overwrite human dims, never set interest/pin; `None` dims mean "not supplied" so partial updates keep existing values), untriaged→proposed→confirmed state, tombstone/revive via `apply_open_issue_set`. Rank formula `rank_score`/`rank_breakdown` (legible chips: `V4`/`M`/`solo`/`★`) with weights from the data repo's config.json (`load_rank_weights`, defaults-tolerant). Store files: `<data_dir>/triage/<owner>__<repo>.json`, pinned_store-style flock + read-or-raise + atomic writes. `confirm(key, *, now, expected_proposed_at=None)` / `confirm_many(keys, *, now, expected=None)` (#248 Triage inbox) flip a complete PROPOSED record to CONFIRMED in one lock + one write per repo file — dimensions, reasoning, and provenance (`ai` stays `ai`, so the audit trail reads "AI proposed, human confirmed") are untouched; a `None` dimension, a tombstoned record, or a missing key is skipped, not an error. The optional expectation is the `proposed_at` revision the caller saw (Codex review of #248): a record the worker re-proposed since then (same key, new dimensions — `select_targets` re-targets a PROPOSED record whose body hash moved) has a different revision and is skipped under the lock, so Confirm means "approve what I saw", never the replacement; `None` confirms unconditionally. Because confirm leaves provenance `ai`, the human-provenance guard can't protect a confirmed record, so `_merge` freezes one outright: an AI upsert onto a CONFIRMED record keeps its dims, reasoning and provenance and refreshes only proposal metadata (`body_hash`/`proposed_at`/`model`) — otherwise a worker run still in flight when the user tapped Confirm would rewrite the decision. `TriageRecord.autopilot` (#302) is a human-only opt-in to the nightly autopilot, guarded like `interest`: AI upserts never set or clear it, and a human write that omits it keeps the stored value. Provenance has a third actor, `duet` (#397, `ACTORS`/`NON_AI_ACTORS`): Claude Duet over the remote connector writes like a human (overwrites any dim, never changes state or flags) but is never recorded as one; the AI worker treats it like `human` (never overwrites the dim or its reasoning); a duet write replaces the reasoning of every dim it wrote (cleared when it sends none). |
| `triage_sync.py` | `TriageSyncer` — debounced, never-raising git commit/push of the triage data repo from the daemon poll loop (#246). `tick()` self-throttles (one `git status` per 15 s on the 2 s loop); commits only when dirty AND quiescent (newest *dirty-path* mtime older than `sync_debounce_s` — committed files' mtimes don't count); push failures keep the commit and retry next tick, surfaced via eventlog. Config: `triage:` section in projects.yaml (`TriageConfig`: data_dir, sync_debounce_s, sync_enabled, plus the #248 AI triage worker knobs `ai_enabled` (default True), `ai_model` (default `"opus"`, passed to `claude -p --model` — chosen over sonnet in the 2026-09-07 prompt eval: sonnet flipped on the same issues between rounds), `ai_batch_cap` (default 15, max issues per repo per worker run), `ai_timeout_s` (default 300, subprocess timeout per `claude -p` call)) |
| `triage_worker.py` | AI triage worker (#248 Phase 3): pure functions from the cached issue list + a store snapshot to per-repo `claude -p` batches. `body_hash(issue)` hashes title/body/sorted-labels — stored on the record so a later run only re-proposes when the issue actually changed. `select_targets(issues, records, *, cap)` picks per-repo, capped batches: UNTRIAGED (or record-less) issues always; PROPOSED only when `body_hash` moved; CONFIRMED and tombstoned records never (design decision — a human decision is never second-guessed). `TRIAGE_OUTPUT_SCHEMA` is the `--json-schema` contract (value 1-5, effort/fitness enums, a `reasoning` object with one string per dimension capped at `MAX_REASONING_CHARS` = 200). `parse_proposals` is a strict parser — any violation (unknown/duplicate issue number, out-of-range value, bad enum, missing/oversized reasoning) rejects the WHOLE batch rather than write partial garbage; issues the model silently omits are fine, they're retried next run — but an ENTIRELY empty proposals list is rejected too (zero progress would re-select the same targets and loop the web trigger's paid calls). `readme_head`/`build_prompt` assemble the RUBRIC + repo description + README head (first ~60 lines) + each issue's title/labels/body plus its old priority label as a weak "previous priority label" hint. `invoke_claude` runs `claude -p --model <ai_model> --output-format json --no-session-persistence --strict-mcp-config --setting-sources "" --tools "" --json-schema <schema>` (flags pinned by a Task 1 probe: no tools, no hooks/MCP/settings, no session file) with `cwd` set to the triage data dir — never the worktree — so the repo's own `CLAUDE.md` never enters the model's context; reads the envelope's `structured_output` key. `run_triage` drives one worker run: per repo, sequentially (concurrency 1), select → prompt → invoke → parse, retrying once with the parser's error appended to the prompt on a parse failure; never raises. Eventlog literals: `"triage worker invoke failed"` (warn), `"triage worker output rejected"` (error, batch rejected twice), `"triage write failed"` (warn), `"triage worker run"` (info, one per run). Dry-run writes nothing. The subprocess boundary (`Runner`) is injected — `default_runner` is the real `subprocess.run`, but `tests/conftest.py` raises on any `claude -p` argv reaching it, and `tests/fixtures/claude_p/*.json` are recorded real envelopes used as fixture stdout |
| `triage_cli.py` | `agent-hq triage run [--dry-run] [--repo OWNER/NAME] [--cap N]` (#248): manual/backfill entry point to `triage_worker.run_triage`, usable outside tmux. Loads issues via `GitHubCache` through the shared `issues_hub.merge_issue_lists` (same Done-drop + de-dup as the web, so the CLI can't propose for Done-but-open issues or revive records the web pass tombstoned), drops closed keys, and — unless `--dry-run` — seeds/tombstones triage records first via `issues_hub.ensure_triage_records` (same idempotent pass the web container routes run). Exits 1 on invalid/missing config or any failed repo, on a `triage.data_dir` that isn't an existing directory (the store would otherwise create the tree and start a second, empty store from a typo'd/un-cloned data repo), and on an `OSError` out of the seeding pass (proposing against an un-seeded store must not report success); 2 (via argparse) on a bare `agent-hq triage` with no `run` subcommand. `--cap` overrides `triage.ai_batch_cap` for one run — `--cap 100` is the one-shot backfill for a large pool of already-seeded issues. Prints a per-repo summary (targeted/proposed/failed counts, each proposal's dimensions and reasoning — model-sourced text is stripped of non-printables so an escape sequence can't repaint the terminal) |
| `hygiene_store.py` | Hygiene findings (#249, Phase 4): one file `<data_dir>/hygiene/findings.json` (one flock, triage_store conventions — tolerant reads, read-or-raise + atomic writes, unparseable entries preserved). `Finding(id, type duplicate\|stale\|cluster\|mismatch\|offboard, issue_keys, action, reasoning, status open\|accepted\|dismissed\|resolved, found_at, sweep_id, model, decided_at, execution)`; `finding_id` = `type:` + sorted keys (a cluster id excludes its umbrella title, so a rephrased title still matches a dismissed cluster). `record_sweep(findings, *, now, sweep_id, model, open_keys)` in one lock: suppresses any id already dismissed/accepted/resolved, refreshes an open one in place, inserts new, resolves an open finding whose premise is gone (dup: either key closed; cluster: fewer than two children still open — the umbrella still makes sense for the rest, and `plan_steps` skips the closed ones; stale/mismatch: its key closed) — scoped to repos present in `open_keys`, so a cold/partial issue list never resolves findings for repos it didn't see (accepted corollary: a repo that drops to zero open issues keeps its findings until Dismiss — the safe side of that trade), never refreshes a finding that is in flight, resolves an older OPEN cluster that overlaps a newly inserted one (regrouped clusters supersede, dismissed ones stay dismissed), resolves an open STALE finding the sweep did not re-report (stale detection is code and runs every sweep, so a missing one stopped qualifying — Codex round 2; AI-derived findings are never resolved this way), protects a partially executed cluster (a failed attempt whose umbrella exists) from refresh, supersession and premise-resolve until no child is open, stamps `last_swept_at` (a failed claude call still goes through `record_sweep` with the code-detected findings, so the stamp rides that path; `stamp_swept` is the bare stamp for callers with nothing to record). Round 4 (built-in, 2026-09-13): a RESOLVED finding the sweep proposes again is revived through the insert path, so the same suppression/overlap guards apply; a human-edited or partially executed finding is never refreshed and never superseded; the stale-not-re-reported resolve and the premise floor (one open child instead of two) both respect a partially executed cluster; `record_progress(fid, *, plan, done, now, facts)` is the checklist writer and merges execution facts (umbrella number, `commented_at`) as each step lands. `dismiss(fid, *, now, expected_found_at)` is revision-guarded like `TriageStore.confirm` ("suppress what I saw"); `mark_accepted` / `mark_failed` store the step log (both drop the in-flight claim and the live `plan`/`progress` keys `record_progress` writes for the Execute checklist); `remove_child(id, key, *, now, expected_found_at)` drops one issue from an OPEN cluster (hand-test round: revision-guarded, refused in flight or when fewer than two children would remain), marks it `edited_by: human` and moves `found_at`; a sweep never refreshes a human-edited finding and drops an incoming cluster that overlaps one (the edit wins over supersession). `claim(id, *, now, expected_found_at)` is the CAS the accept route takes under the flock BEFORE any GitHub write — an in-flight finding refuses a second claim and a dismiss, so a double tap never replays the plan; the claim is a LEASE (`CLAIM_LEASE_S` = 30 min, round 3): past it every writer and `snapshot(now=)` treat the finding as released, so a process that died mid-Execute cannot wedge it; `record_sweep` also revives a RESOLVED finding the sweep proposes again (an issue parked in the board's Done column looks closed to the sweep; dismissed/accepted stay suppressed) and `remove_child` re-keys the edited cluster to its remaining children's fingerprint, leaving the AI's original id as a DISMISSED tombstone; the execution log carries facts a retry reuses (`umbrella` — a cluster whose umbrella landed before a later step failed does not create a second one). `Finding.from_dict` types the action VALUES as well as the keys (round 5: a null `close` 500'd the card template) — malformed entries stay preserved-but-unparsed. `load_hygiene_thresholds(data_dir)` reads config.json `hygiene` (`stale_days` 90, `stale_max_value` 2), defaults-tolerant. **Off-board** (#273): `FindingType.OFFBOARD` (action `add_to_board` carrying `key`/`project_number`/`project_owner` + optional `project_name`/`title`, so `plan_steps` needs no config) is pure code like STALE — its premise is gone when the key shows up IN `open_keys` (the board set; judged before the repo guard), and `record_sweep(offboard_repos=…)` resolves an OPEN one a sweep that checked its repo did not re-report (closed / gone), never for a repo the detector skipped; a partially added finding (`item_id` in a failed log — item-add landed, Todo write did not) keeps its card only while its key is in `offboard_statusless` (board items with no Status), then resolves like any other (review rounds 2–3). Repo names and off-board finding ids compare case-insensitively (Codex review): an empty board keeps the config's repo spelling and the first add switches the sweep to GitHub's, so the same issue re-reported under the other spelling is refreshed / suppressed in place, never a twin card |
| `hygiene_worker.py` | Hygiene sweep (#249): `detect_stale` is pure code (updated_at ≥ stale_days old AND a human-CONFIRMED value ≤ stale_max_value; unknown date / unscored / unconfirmed / tombstoned skipped — round 3: a PROPOSED value is awaiting a human and must never back a close); duplicates, same-repo sentinel clusters and ≥2-point value mismatches come from ONE cross-repo `claude -p` call (`build_hygiene_prompt` over every open issue, body ≤ 300 chars, `HYGIENE_OUTPUT_SCHEMA` via `triage_worker.invoke_claude(schema=…)`). `parse_findings` is strict — unknown key, keep==close, cross-repo or <2-child cluster, clusters sharing a child, non-sentinel child, proposed==current, bad reasoning rejects the WHOLE batch — but EMPTY lists are a good result (unlike the triage worker: an empty sweep means the backlog is clean, and the 24 h stamp already prevents re-fire loops). **Off-board detection** (#273): `collect_offboard_inputs(cfg, cache)` → `OffboardInputs(open_by_repo, board_keys, targets)` from the cache's never-fetching board read (Done items count as on-board — never `merge_issue_lists`) and `github.open_issue_keys` (debounced REST index sync; `None` = unseeded → repo skipped, as is a cold board or a repo without `project:`); `detect_offboard(inputs, describe=)` diffs per repo, `describe` (`github.fetch_issue_summary`, REST, capped at `MAX_OFFBOARD_LOOKUPS`=100/sweep) supplies the card title and drops a candidate the index still thinks is open but REST says is closed; `run_sweep(offboard=…, describe=…)` writes them with the code-detected stale findings even when claude fails — and writes them for an EMPTY open set too (a freshly configured, empty board is the headline case; every resolve rule is repo-scoped). Review round 3 guards: `failed_keys()` is read BEFORE the cached board reads (serving a TTL-fresh entry clears the record), a TTL-expired entry is skipped like a failed one, an issue created after the board snapshot (`GitHubCache.cached_at`) minus `OFFBOARD_MIN_AGE_S` (600 s, GitHub's own Auto-add lag) is not judged, and a per-repo index failure is warned and skipped (the collector runs outside `run_sweep`'s never-raises boundary). `run_sweep` retries once on parse failure; on invoke/parse failure the code-detected stale findings are STILL written and premise-gone findings still resolve (only the AI-derived findings are absent), `report.error` still reports (web cooldown), and `last_swept_at` is stamped (a broken claude must not re-fire a paid call on every /issues load; Run now bypasses the stamp), never raises. Dry run writes nothing, stamp included; an empty issue list writes nothing either, not even the stamp (round 3: stamping after a cold cache silently disabled the automatic sweep for 24 h — the web trigger skips an empty list, and the CLI reports 0 open issues). Eventlog literals: `"hygiene sweep run"` (info), `"hygiene sweep invoke failed"` (warn), `"hygiene sweep output rejected"` (error), `"hygiene write failed"` (warn) |
| `hygiene_actions.py` | Accepting a finding (#249): `plan_steps(finding, *, issues_by_key, github_cache, triage_store, now)` returns `Step(label, run)` — the SAME list renders the preview modal and drives `execute` (sequential, stop at first failure, step log stored on the finding). duplicate/stale: comment (`github.comment_issue`, new best-effort wrapper, `kind="issue-comment"`) → `close_issue_not_planned` → `mark_closed`; cluster: `create_issue` umbrella (label `sentinel`, body lists children) → per child "Tracked by #U" comment → close; mismatch: `TriageStore.upsert(value, actor="human")`, no GitHub write; add_to_board (#273): `add_to_project` (item id into the retry facts) → Status→Todo via `github_cache.get_field_info` + `set_field_value`; membership is judged on the RAW cached board (`_board_member`, Done included — a hand-parked Done item is never pulled back to Todo): on board with a Status is nothing-to-do, on board with NO Status plans just the Todo write on the board item (a retry after a failed Todo write), closed is a gap; the fresh-list fallback (raw board unreadable) counts an `Issue` only when both its `project_number` AND `project_owner` match the target (Codex: two owners can share a number), and every Todo-step failure — missing Status field / Todo option included — carries the `item_id` fact. A finding whose issue is no longer open yields a single non-actionable "no longer open" step (the preview disables Execute) instead of acting on a stale key — for a duplicate, the keeper too (closing the survivor of a pair would delete the tracking issue); a stale finding whose issue has newer activity or a raised value, and a mismatch whose value moved since the sweep, likewise yield a non-actionable "nothing to do" step (Codex round 2: revalidate before closing); a comment step records `commented_at` on success only, so a retry after a failed close skips it and reads activity within `OWN_ACTIVITY_GRACE_S` of THAT comment as its own — the anchor is the comment's own timestamp, carried unchanged across attempts, never the latest attempt's start (rounds 3–4); `execute(on_step=)` hands the cumulative log AND the facts so far to the route after every step, which `record_progress(facts=)` persists at once (an umbrella number must outlive a process death); the accept route plans against `_load_issues_fresh` (a forced, strict refetch of every board + closed keys, verdict from `fetch_status()` because the cache serves last-good on failure rather than raising, then `github.refresh_issue_timestamps` re-reads dates from the index the closed-keys sync just refreshed — a failed refetch is a failed attempt shown on the card, never a plan against the SWR cache); a cluster skips children already closed (by hand, an earlier duplicate accept, or its own previous attempt — Codex round) and needs two open children for a fresh umbrella, one for a retry that already has one. Cross-repo duplicate comments carry the full `owner/repo#N` reference. Round 5 (Codex): a cluster retry checks the prior umbrella against the cache's closed-keys index (`_known_closed`) and refuses when it was closed between attempts; a mismatch whose issue left the open set is a non-actionable gap, never a value bump on a soon-tombstoned record |
| `hygiene_cli.py` | `agent-hq hygiene run [--dry-run]` (#249): manual/dry-run entry to `run_sweep`, usable outside tmux; same config / data_dir guards as `triage_cli`, issues via `triage_cli._load_issues`. Exit 1 on config error or a failed sweep |
| `autopilot.py` | Nightly autopilot (#302): selection, launch and outcome probes. No LLM decides what runs — the human-only `TriageRecord.autopilot` flag plus a CONFIRMED record replace `/worktree-create`'s tier classification, and every uncertain input fails CLOSED. `select(issues, records, blocked_keys, *, in_flight, active_issue_keys, unsafe_repos, cfg, weights)` is pure: only `autopilot is True` records are considered; skip reasons are the `SKIP_*` literals (`not confirmed`, `not open` — tombstoned or absent from the open list, `repo not allowlisted`, `already attempted` — any attempt in the store, parsed or not, so one attempt per issue until Retry, `worktree or branch exists`, `github fetch failed` / `worktree check failed` for an unsafe repo, `throttled`); eligible issues sort by `rank_score` desc then issue number and the launch count is `min(per_night, max_open - in_flight)` (`throttled` = eligible but zero slots). `compute_outcomes` builds compare-and-set `OutcomeUpdate`s for RUNNING / PR_OPEN / UNKNOWN attempts only: a PR for the exact attempt branch maps OPEN/MERGED/CLOSED to `pr_open`/`merged`/`closed`; no PR plus a positively-dead tmux session → `ended_without_pr`; a live session leaves UNKNOWN as UNKNOWN (spec rule 7: an uncertain launch stays in flight and in Needs attention until the user presses Retry — only a real PR or a positively gone session moves it); a failed lookup or undecidable probe (None) changes nothing — failure never frees capacity — and LAUNCHING is never touched (only `finish_launch` or a stale-launch Retry resolves it). `fill_goal_condition` strips the template's leading HTML comment explicitly, requires the remainder to start with `/goal ` and to contain all three of `<ISSUE>`/`<BRANCH>`/`<TURN_BOUND>` (a template missing any raises `GoalTemplateError` — the CLI refuses before reserving), substitutes them and raises `GoalTemplateError` on any leftover `<UPPER>` placeholder. `run_launcher` runs `launch.sh --branch <b> --base <base> --auto --model <autopilot.model>` (goal text on stdin, cwd = the repo's `local_path`, injected `Launcher`, 900 s timeout — the repo's wt pre-start hook runs inside it, #312): exit 0 = OK (session/worktree parsed from `Launched session '…' in … (agent `), exit 1 / `OSError` = FAILED with `launcher_failure` (nothing running), exit 2 / any other non-zero / timeout = UNCERTAIN (a session may be running, keeps holding capacity); every non-zero exit also carries `stderr_tail` (last 15 non-empty stderr lines, ≤2000 chars — wt's own error, #312) while `detail` stays the one last line. `attempt_branch` = `generate_branch_name` + `-ap<attempt_id[:6]>`, unique per attempt so a retry never adopts a historical PR. `fetch_default_branch(repo)` (#312) is the repo's GitHub `default_branch` (`gh api repos/X`, REST via `gh_meter` kind `autopilot-base`; None on any failure — never a guessed `main`: magic-eye-gen's `main` is a stub whose missing `./run` failed the wt pre-start hook). `cleanup_failed_launch(repo_path, branch)` removes what a launcher-stage failure left (the worktree on the attempt's own branch via `git worktree remove --force`, then `git branch -D` — plain git, never `wt remove`), refusing a branch without the `-ap<6 hex>` suffix or with any commit no other branch/remote/tag reaches (judged without the base name — GitHub's default may exist only as `origin/<base>` locally); returns a short note, never raises. Strict probes: `issue_branch_numbers` (issue numbers in any worktree or local branch named `feature\|bugfix\|chore\|docs/<n>[-<n>…]-`; None on any git failure), `fetch_pr` (`gh pr list --head <branch> --state all` via `gh_meter.run_gh(kind="autopilot-pr")`; the `NO_PR` singleton — matched by identity — when GitHub says none, None on failure/malformed output), `tmux_session_alive` (`tmux has-session -t =<name>` — the `=` makes it an exact match, never a prefix; False only when tmux positively reports the session/server absent; a socket/permission error is None). `sync_outcomes(store, *, now, write=True)` observes outside the lock and applies inside it (dry run patches a snapshot copy only); `load_issues_strict` force-fetches (`force=True`) every board and the closed keys and marks a repo unsafe (`github fetch failed`) when it appears in a failed board's raw list or its `closed:<repo>` lookup failed — a failed board with nothing cached marks every allowlisted repo unsafe. `LAUNCH_SCRIPT` / `GOAL_TEMPLATE` point into `~/.claude/skills/{worktree,goal-mode}`. Autopilot has no merge code path: the goal stops at an open PR |
| `autopilot_store.py` | Autopilot state (#302): one file `<triage data_dir>/autopilot/state.json` (`paused`, `refreshed_at`, `attempts` keyed by issue key, last `MAX_RUNS`=30 `runs`), triage/hygiene-store conventions (flock'd read-modify-write, atomic writes). Fail-closed rules: read-or-raise — invalid JSON, non-UTF-8 bytes, a non-object root or non-dict `attempts` / non-list `runs` raise `OSError`, so every writer refuses (`snapshot()` alone degrades to empty; callers that must not be fooled call `assert_readable()` first); a present non-bool `paused` reads as PAUSED; an unparseable attempt (or one whose `issue_key` disagrees with its key) is preserved verbatim, still blocks its issue (`blocked_keys`) and still counts toward `in_flight`. Outcomes: `launching`/`running`/`pr_open`/`unknown` are in flight (`IN_FLIGHT_OUTCOMES`); `merged`/`closed`/`ended_without_pr`/`launch_failed` are final. `reserve(attempt, *, max_open)` writes a LAUNCHING reservation BEFORE launch.sh runs and re-checks paused / existing attempt / capacity under the lock (`ReserveResult` OK/PAUSED/EXISTS/FULL); `finish_launch` and `apply_outcomes` are compare-and-set on (attempt_id, expected outcome), so a slow refresh never rewrites a replacement attempt. `apply_outcomes` writes — and stamps `refreshed_at` — only when at least one update lands; no updates or all CAS misses return 0 without touching the file (the triage data repo's syncer commits + pushes every write, so a no-op refresh must not churn it). `retry(key, *, now)` deletes an attempt only when it is `launch_failed`/`ended_without_pr`/`closed`/`unknown` or a LAUNCHING one older than `STALE_LAUNCH_S` (1 h; an unparseable `dispatched_at` is never stale) — it frees the slot, but the attempt's worktree and `-ap<id>` branch stay (unless `cleanup_failed_launch` already removed them after a launcher-stage failure, #312), and the strict branch probe keeps skipping the issue (`worktree or branch exists`) until they are removed; it does not touch a running session. `set_paused`, `record_run(RunRecord)` |
| `autopilot_cli.py` | `agent-hq autopilot run [--dry-run]` (#302): the nightly entry point, usable outside tmux (a launchd wrapper outside this repo runs it). Order: invalid or missing config (`load_config` → None) → FAILED, exit 1; config loaded but `autopilot.enabled` false/absent → `autopilot: skipped (disabled)` (checked BEFORE the triage block, so a disabled install with no `triage:` stays quiet); a missing/non-directory `triage.data_dir` → exit 1; paused → `skipped (paused)`, and a held single-flight run lock (`<data_dir>/autopilot/run.lock`, `LOCK_EX\|LOCK_NB`) → `skipped (another run holds the lock)`, all exit 0; then `assert_readable()` (corrupt state → exit 1 before anything launches), the goal template is validated FIRST (an unusable template records an errored run and exits 1), `sync_outcomes`, `select`. Dry run prints the selection, each would-be branch (`-apxxxxxx`) and filled goal text, and the skips, and writes no state (it still opens `run.lock` and force-refreshes the GitHub cache). A real run revalidates each issue right before reserving it — opt-in + confirmation (`record_still_eligible`, else `opted out or unconfirmed during run`) and a fresh branch probe (`worktree check failed` / `worktree or branch exists`) — then `reserve` (PAUSED skips the rest as `paused during run`; EXISTS/FULL skip that issue as `exists`/`full`), launch, `finish_launch` (OK→`running`, FAILED→`launch_failed`, UNCERTAIN→`unknown`). Each launch's base is `RepoConfig.base_branch` else `fetch_default_branch`, resolved lazily once per repo per run and BEFORE `reserve` — an unknown base skips the issue as `base branch unknown` (a failure skip like `github fetch failed`: error + exit 1); the dry run prints `(base <b>)`. A launcher-stage failure runs `cleanup_failed_launch` (never after an uncertain launch) and appends its note to the attempt's `detail` (an exception out of it becomes `cleanup failed: …` so the outcome still lands); the stderr tail is printed (→ `autopilot.log`) and rides the eventlog as `stderr_tail`, the note as `cleanup` (seams `base_lookup=`/`cleanup=`). Any failed or uncertain launch stops the night. Spec rule 9: when an opted-in candidate was skipped for `github fetch failed` or `worktree check failed` (pre-select or mid-run re-probe), the run's error is `"<reason>: <repo>"` and it exits 1 — an unsafe repo with no candidates is not a failure. Every run is recorded as a `RunRecord` — including a crash after `assert_readable()`: `_Progress` shares the launched/skipped lists with the handler, which makes a best-effort `record_run` with the error before the FAILED line; the last line is `autopilot: ok\|FAILED — launched N, skipped M[, throttled]` and any error means FAILED + exit 1 (lock-open, state and unexpected errors also print a `FAILED` line). Eventlog literals: `"autopilot run"` (info — every completed run and the disabled/paused/locked skips) and `"autopilot launch failed"` (error — per failed launch, plus `stage` lock/state/template/unexpected). Tests inject `launcher`/`pr_lookup`/`session_probe`/`issues_loader`/`branch_probe`/`template_path`; the real `launch.sh` is blocked by a conftest guard (see Testing) |
| `transcript.py` | Pure Claude-transcript JSONL primitives: reverse-read `extract_last_phase_tag` / `extract_last_assistant_text`, line-leading `<phase>` regex, and `transcript_path_for(cwd, sid)` path construction. Shared by `hooks` (summary) and `phase`/`resolver` (phase) |
| `codex_transcript.py` | Pure Codex rollout JSONL primitives (#206): `read_rollout_session_meta` (first line only — the subagent-child fixture's second inherited-parent meta must never bind), `extract_last_codex_phase_tag` / `extract_last_codex_assistant_text` (assistant `response_item`/`output_text` records only; `event_msg` duplicates ignored; `is_subagent` rollouts always refused). Reverse scans are capped at `CODEX_REVERSE_SCAN_BUDGET_BYTES` so an untagged multi-MB rollout can't blow the 2s daemon tick. Tolerant of unknown record types/malformed lines — parser failure degrades codex phase/summary only, never discovery. Pinned against `tests/fixtures/codex_rollouts/` |
| `transcript_history.py` | Transcript-local scrollback (#171): `read_history_page()` pages a transcript JSONL into `TranscriptTurn`s (user prompts, assistant text, tool one-liners, dimmed `system` notices; skips thinking/tool_result/sidechain) with byte-offset cursors (append-only ⇒ stable). Injected `type: user` records are classified, not shown as the user: `isMeta` skipped, compact summaries → divider, task-notification/teammate/command-stdout/interrupt → condensed `system` turns, `<command-*>` → the typed `/name args` line. Received cross-session messages (#287) render as `message from @name: <first line>` system turns in both stored shapes — an `isMeta` user record with `origin.kind == "peer"` (normal delivery) and a `queued_command` attachment with a peer origin (delivered mid-turn); every other attachment stays skipped. `locate_transcript()` is the stat-side availability gate. Behind the web Live\|History preview toggle and the TUI `h` binding — replaces the remote-control scroll rejected in PR #152 |
| `resolver.py` | `resolve_phase_snapshot()` — the single pure-read boundary. Phase comes from the transcript via the harness adapter's `transcript_phase` (claude: `transcript_path_for` candidates; codex: `Agent.codex_rollout_path`), a single `os.stat` recorded in `inputs_observed`, with a `previous_snapshot` stat-cache fast-path keyed on `session_metadata.stable_id`; hook state file read only for revision/validity. Called by daemon, web watcher, live fallback |
| `phase.py` | `detect_transcript_phase()` (last `<phase>` tag) + `detect_branch_phase()` rule fallback (PR state / commits / spec-plan files / branch name). Owns the per-process `pygit2.Repository` cache and `_PYGIT2_ERRORS = (GitError, OSError)` — pygit2's C layer raises a bare `OSError` for libgit2 OS-class failures (seen on macOS: `failed to read descriptor: Resource deadlock avoided`), so every pygit2 call site here and in `enrichment.py` catches that tuple and degrades one agent's git context rather than aborting the daemon tick. `enrichment.git_diff_shortstat` also catches `TypeError` (#358): pygit2's `Repository.diff` raises `TypeError('unexpected "<class '_pygit2.Commit'>"')` when the commit's tree can't be read, hiding the real `KeyError` in `__context__`. `enrich_all_agents(raw, previous=)` isolates each agent: a failing one becomes `_fallback_agent` (fresh pane fields, state UNKNOWN, plus the previous tick's git context and session identity when the pane's agent pid is known and unchanged), and is reported once per 15 min per repo/branch/cause as the warn `"agent enrichment failed"` / `"git diff stats failed"` (`error_type`, `cause_type`, `repo`, `branch`) with the full traceback in `~/.cache/agent-hq/enrich-errors.log` (`ENRICH_ERRORS_FILE`, one `.1` rotation at 512 KB). A Claude agent whose registry read returns no ids keeps the same process's previous ids (`_keep_exiting_claude_ids`, #433): Claude deletes `~/.claude/sessions/<pid>.json` ~1 s before it exits, and that tick used to snapshot `session_id: null` — the record `recover` baselines on after a reboot |
| `_locking.py` | `file_lock()` — `fcntl.flock` cross-process serialization for `overrides.json` and per-pane hook state files |
| `runtime_dir.py` | Private `/tmp/agent-hq` root (#263 F2/F4/F6): writers create directories via `ensure_dir()` (root forced to a 0700 dir owned by this user; symlink/foreign owner → `UnsafeRuntimeDirError`), readers call `check_trusted()` before trusting a runtime file (also rejects a group/world-writable root). The error subclasses `FileNotFoundError`, so existing "file missing" handlers degrade to empty. New `/tmp/agent-hq` readers/writers must go through these; `RUNTIME_ROOT` is redirected by conftest like the other path constants. Baseline scan results: `docs/security/2026-10-03-baseline-scan.md` |
| `logclient.py` | Vendored write-side client for the unified logger (#231, from the logger project's reference Python client): `log()`/`enqueue()`/`flush()` never raise; flock'd queue-and-replay at `~/.cache/agent-hq/logger-queue.jsonl` (capped 500, drop-oldest), `.rejected` quarantine sidecar, `User-Agent: claude-logger/1.0` (Cloudflare BIC 403s default UAs). Divergence from upstream: a missing, blank or unreadable write token (`~/.claude/secrets/logger-write.token`) ⇒ disabled — events DROP, never queue (#237: a blank one used to queue to the cap and pay a doomed replay per `log()`). logger#9 hardening ported: a 400/422 naming `item N:` quarantines only that event, an unattributed reject or multi-event 413 halves the chunk (`MAX_SPLITS_PER_CALL` = 7, own budget), guaranteed rejects repaired at build time (empty message → `(empty message)`, source ≤128, message ≤8192, scalar json_extra → `{"value": …}`, >16384 → truncation marker, NaN → unserializable; UTF-16 lengths), `log()`/`flush()` cap the queue only when the call ends on a transient failure (drop-oldest kept; `enqueue()` still caps every append), and `log()` after a transient failure POSTs only its own event for `LOGGER_COOLDOWN_MS` (60 s; `flush()` ignores it; any non-transient answer, ok or a permanent reject, clears it). Module-level cooldown state is reset by the conftest isolation fixture |
| `logreader.py` | Read side of the unified logger (#148): stdlib `fetch_logs()` → `GET /logs` with the ADMIN token (`LOGGER_ADMIN_TOKEN_FILE` env seam else `~/.claude/secrets/logger-admin.token`; endpoint `LOGGER_URL` else `logclient.DEFAULT_URL`; UA `agent-hq-logreader/1.0`), returning `LogPage`/`LogRow` (json_extra parsed from JSON text, raw string kept on parse failure). Unlike the write side it FAILS LOUDLY (`LogReadError`) — routes decide presentation. Also `is_configured()` (the Errors tab's enable gate), `group_rows_by_app()` (groups by newest row), and `ErrorsCache` (60s in-memory TTL, `AutomationsCache` shape) for the default error+alert/24h query. Deliberately separate from vendored `logclient.py` (untouched) and unrelated to `web/errors.py` (action-error wrappers). No projects.yaml config — env vars are test seams |
| `eventlog.py` | `emit(level, message, json_extra)` — agent-hq-facing wrapper pinning `app="agent-hq"`: stamps `json_extra["process"]` from `set_process_role()` (daemon/web/tui/hook/mcp/cli), dedupes identical (level, message) within `min_interval_s`, delivery per role (thread: daemon/web/tui/mcp; queue-only: hook — daemon flushes every 5 min; inline: cli). Messages are STABLE literals (sentinel fingerprints on the first 48 normalized chars); level semantics: error = broke (sentinel auto-files an issue), warn = degraded, info = lifecycle. `logclient` is imported on first send, not at import time — in the spawned thread for thread roles (#237: hooks import eventlog on every harness hook, and urllib/ssl cost ~10 ms). `emit` returns False when the dedupe swallowed the event; `mark_reported(exc)` / `was_reported(exc)` tag an exception whose origin's emit went out (the dispatch crash), so `mcp_server._guarded` does not file the same incident twice |
| `web/refresh.py` | `BackgroundRefresher` — singleflight background refresh for stale-while-revalidate routes (#226): `kick(factory)` coalesces concurrent stale serves onto one `asyncio.Task` and trips a 30s cooldown after a failure (no refresh loops); `kick(factory, force=True)` clears that cooldown for a deliberate human retry (#248's "Run now") while keeping the singleflight. Instances on `app.state` include `issues_refresher` and `automations_refresher`. The Dispatch container routes serve TTL-expired cache data instantly via the caches' never-fetching accessors (`GitHubCache.get_issues_cached`/`get_closed_keys_cached`, `AutomationsCache.get_cached`), render an "Updating…" pill, and publish `dispatch.refreshed` on completion — JS listeners refetch the container. Cold first-ever loads and the explicit refresh buttons stay blocking. `detect_active_worktrees` is TTL-cached (15s, `invalidate_worktree_cache()` after dispatch) with a 5s git timeout; `/agents` + `/automations` render their initial view inline on the page GET |
| `service.py` | macOS launchd LaunchAgent for persistent hosting (`agent-hq web service install/uninstall/status/restart`). Pure `build_plist()` + `launchctl` lifecycle (`gui/$UID` domain). Binds the tailnet IP, sets PATH so the server finds tmux/git/gh/tailscale under launchd |
| `restart.py` | Runtime restart core behind the web ⚙ menu's Restart action (#215): `perform_daemon_restart()` (daemon stop → clear `cache.json` → daemon start, tolerant of a stopped/wedged daemon), `web_service_owns_process()` (launchctl-print pid match — the "safe to kickstart myself" gate that keeps a dev server from bouncing the live service), `kick_web_service()` (`launchctl kickstart -k` via Popen — kills the caller). Skips `uv tool install --reinstall` (install-only; editable install means a process restart already picks up main's code) |
| `snapshots.py` | `SnapshotStore` — durable rolling record of the live-agent set at `~/.config/agent-hq/agent-snapshots.jsonl` for crash recovery (#125). Daemon appends a compact per-agent record (`agent_to_record`) on any captured-field change (deduped, order-insensitive), capped to `MAX_RECORDS`. Path injectable via constructor / `_DEFAULT_SNAPSHOTS_FILE`; `append_if_changed` swallows its own I/O errors so it never wedges the poll loop |
| `recover.py` | `agent-hq recover` — `select_baseline()` picks the pre-crash roster (most recent sharp collapse within 48 h, else last non-empty; `--since`/`--baseline` override) because the last snapshot reflects the post-restore world (#135); the collapse is judged over the non-increasing run of records leading into each drop (`_run_start`: `CLIFF_MERGE_WINDOW_SECONDS` bounds the DROP records that merge — a record's timestamp is its drop time — and the record before the earliest merged drop is the peak however old, since an idle roster is deduped for hours — baselined at the NEWEST record of the peak count, since an equal-count record can be membership churn (`[A,B,C]→[A,B,D]`); only a record that is itself a drop is a candidate, so a state-only record inside the 48 h lookback can't revive an expired collapse; `_timestamp` reads malformed/non-finite timestamps as age-unknown → skipped, never a `TypeError`), never a single step — the daemon's 2 s tick records a reboot as a 14→13→13→10→…→1 staircase with no qualifying step (#274) — and the fallback reason names the largest windowed drop seen; `exclude_self()` drops only the invoking session (`$TMUX_PANE`, plus baseline records with its identity — a sessionless invoker only its own pane's record) from both sides, and baseline selection discounts only that identity (`self_ids`) — #410: a missing agent whose cwd is inside `self_repo_root()` is listed separately by `split_possibly_self()` ("possibly self, verify"; JSON `possibly_self`), never hidden; `resolve_from_transcripts()` gives a sessionless missing Claude record a session id from `~/.claude/projects/<encoded-cwd>/*.jsonl` — unclaimed by any live agent, baseline record or another slot's (pane, tool, cwd) id in any record, last written at or before the cutoff (`Baseline.collapse_ts` + `CLIFF_MERGE_WINDOW_SECONDS` for a detected collapse, else now) and not before the slot first appeared, marked "(resolved from transcript)" — and lists candidates instead when two are within `TRANSCRIPT_AMBIGUITY_SECONDS`, several sessionless agents (live sessionless ones included) share the encoded transcript dir, or the only one predates the agent; `compute_missing()` diffs baseline vs live by membership (identity = `stable_id` (claude transcript sid / codex thread UUID), else `tool:cwd`); `resume_command()` builds each harness's resume line from its adapter `resume_spec` (claude: `(cd <cwd> && claude --resume <id>)`; codex: `(cd <cwd> && agent-hq codex -- resume <thread-uuid>)`). `run_recover()` is the CLI handler (`--json` for machine output); the chosen baseline + reason are surfaced in the report |
| `automations.py` | Scheduled-automation health (#54): `collect_health()` composes launchd last-exit-code (`launchctl print`), plist `StartCalendarInterval` schedule math (`next/most_recent_fire_time`, daily Hour/Minute only), and wrapper-log mtime/tail into `AutomationHealth` with OK/ERROR/MISSED/UNKNOWN rules (missed = fire time + 15 min grace passed without a log write; optional `failure_pattern`/`success_pattern` config regexes judge the LAST log line → ERROR even when exit is 0 or launchd forgot it after a reboot). Exit 15 on a KeepAlive job isn't an error or reported as a result (`_meaningful_exit_code`): launchd stops a job with SIGTERM to restart it. Any other code stays an error, since without a heartbeat it may be the only sign of a crash loop. For jobs without `stale_after`, a signal (`last terminating signal = Killed: 9`, `LaunchdJob.signal`, #427) counts the same way via `_meaningful_signal`: SIGTERM on a KeepAlive job is a restart, any other signal is ERROR even once it runs again; `AutomationHealth.last_signal`/`signal_name` name it on the row (`error (SIGKILL)`) and in Duet's reason (`killed by SIGKILL`); `_daemon_health` is unchanged. A KeepAlive job with no `stale_after` and no schedule (`_has_schedule`: any `StartCalendarInterval`/`StartInterval`, not just the parsed daily ones) reads STOPPED when not running or not loaded (`_keepalive_stopped`, #426), except in the gap after a restart. KeepAlive is the live plist's `KeepAlive: true` (a conditional dict doesn't count), else a `.plist.disabled`'s, else launchctl's property. `read_launchd_job` returns `loaded=False` for launchctl's exit 113 (not found) and None only when launchctl couldn't be read, which never reads as STOPPED. `_read_plist` also swallows `ExpatError`/`ValueError` so one malformed plist can't 500 the section. `AutomationsCache` (in-memory TTL) is injected via `create_app(automations_cache=…)`. Read-only; config in `automations:` block |
| `pinned_store.py` | `PinnedSessionsStore` — durable set of `Agent.stable_key` values pinned to the top of the agent list (#141), at `~/.config/agent-hq/pinned-sessions.json`. Mirrors `ConfirmedKeyStore` (flock + atomic write, tolerant reads); path injectable via constructor / `_DEFAULT_PINNED_FILE`. Shared by the TUI and web service (the file IS the cross-surface sync). Injected into `create_app(pinned_store=…)` → `app.state.pinned_store` and read directly by the dashboard. Pin toggles write ONLY the current `stable_key` (#205 Codex review — a persistent `tool:cwd:pane_id` entry would transfer to a future unrelated same-pane occupant); `migrate_legacy_pins()` (called each daemon tick + dashboard refresh) re-keys a legacy-form pin on the captured stable id, which also carries a pre-capture codex pin across a later pane renumber. Membership checks stay dual-form via `Agent.is_pinned_in` |
| `ui_prefs.py` | `UiPrefsStore` — durable UI preferences at `~/.config/agent-hq/ui-prefs.json` (#176), same flock/atomic-write/tolerant-read pattern as `pinned_store`. Keys: `tui_sort_mode` (priority\|phase\|repo, restored by the dashboard on launch, persisted on every `s` press), `web_agents_view` (phase\|list\|kanban, restored by bare `GET /agents`, persisted on explicit `?view=` toolbar clicks), `web_theme` (valid: default\|midnight\|warm\|paper\|terminal\|neon\|brutalist\|console\|bubblegum; the ⚙ picker shows only default\|console\|neon\|terminal\|paper\|bubblegum — midnight/warm/brutalist are benched post-bake-off, hidden via the registry's `THEME_PICKER_IDS` but still deep-linkable/persistable; the #220 expressive themes push fonts/shape/density/decoration, not just color; `bubblegum` was adopted from Codex's PR #221 at the bake-off and reworked pink/light; set by the header ⚙ picker via `POST /prefs/theme`, read by `base.html` on every full page render; `WEB_THEMES` must stay in parity with `templates/partials/_theme_registry.html`, pinned by `tests/test_web_theme.py`), and `web_issues_mode` (queue\|triage\|browse — `triage` added #248: restored by bare `GET /issues`, persisted on explicit `?mode=` clicks), and the sticky Live\|History preview modes `tui_preview_mode`/`web_preview_mode` (live\|history, #171 follow-up: TUI persists on `h`, web on toggle clicks via `?persist=1`; each surface remembers its own). Getters return `None` for missing/invalid values; unknown keys survive writes. Writes go through a strict reader that skips (rather than clobbers) when the file exists but can't be read — a corrupt file is still recovered by the write (#192 class, AHQ-014). Injected via `create_app(ui_prefs_store=…)` / `AgentDashboard(ui_prefs_store=…)` |
| `config.py` | Load/validate `~/.config/agent-hq/projects.yaml`. `DispatchConfig` (#205 Stage 1B) gains `default_harness` (validated against `VALID_HARNESSES`) and `harnesses: dict[str, HarnessDefaults]` (`model` free pass-through, `effort` validated against `VALID_EFFORTS`). `DispatchConfig.scratch_cwd` (#345) is the `scratch` chat session's cwd (default `~`). A leftover top-level block of a removed feature (`RETIRED_KEYS`: `companion`, #399) is ignored and listed in `Config.retired_keys`; the loader never logs (short-lived CLIs and MCP servers load it too) — `create_app` emits one warn per web process, `"companion config block ignored"`. Any other unknown key, a leftover `personal:` block included, is ignored silently. `GithubConfig` (#255) carries `graphql_floor` (default 500) — the headroom below which background refreshes are skipped. `AutopilotConfig` (#302, `autopilot:` block; absent → `None`) carries `enabled` (default false — the hard kill switch; the web Pause is the soft one), `per_night` 2, `max_open` 3, `turn_bound` 40 (positive ints), `model` `"opus"` (non-empty, no whitespace) and `repos` (allowlist; each must be a configured repo, empty = nothing eligible). `RepoConfig.base_branch` (#312, optional per-repo `base_branch:`, non-empty string, stored stripped) overrides the GitHub default branch autopilot cuts worktrees from |
| `github.py` | GitHub Priority is read-only legacy input; status writes remain. Query GitHub Projects API via `gh` CLI, caching. Read-only fetchers raise `GitHubFetchError` (rate-limit aware: `rate_limited`/`reset_at` from a memoized `gh api rate_limit` probe) instead of collapsing failures to `[]`/`{}`/`None` (#254) — an empty result now means GitHub genuinely returned nothing. `GitHubCache` keeps the previous-good entry **at its original timestamp** on a failed refresh (projects and closed-keys alike), records the failure, emits an eventlog `warn` (`github fetch failed, serving cached data`), and reports it via `fetch_status()` → `FetchStatus(ok, message, rate_limited, reset_at, stale_since)`, which the web dispatch banner and the TUI toast render through `filters.format_stale_notice`. #255 rewired the fetch layer underneath that contract: boards come from `gh_board` (203 -> 2 points), timestamps and closed state from `gh_index` over REST (the 500-row `gh issue list` calls and the #113 backfill are deleted), every gh call routes through `gh_meter.run_gh`, and `_rate_limit_reset` delegates to `gh_budget`. `GitHubCache` is now genuinely cross-process: reads reload on mtime, writes are a flock'd read-modify-write plus atomic `os.replace`, and each project's fetch is single-flighted on its **own** lock (a global one would serialize the six `asyncio.gather` fetches and regress #90's max-of-N). Background refreshes below `config.github.graphql_floor` are skipped and reported as `FetchStatus.budget_skipped` — deliberately not `rate_limited`, since nothing failed. The gate only works because *background* paths do not force: the web SWR sweep passing `force=True` made it inert on the normal refresh path (the stale-project filter already guarantees a fetch), while the TUI's explicit `r` had to start forcing so a present user is never refused. Index syncs are single-flighted on a per-repo flock, and `_load_from_disk` stats before reading so a peer's write landing mid-read is not recorded as already-seen. Round-2 review (2026-09-10): `_fetch_failure(graphql=…)` only notes exhaustion for GraphQL rejections — the REST index goes through the same funnel, and a core/secondary rejection there stood every board refresh down; the in-lock recheck also consults `is_exhausted()` so waiters queued behind a rejected fetch do not fetch into a dead budget; a failed index sync serves the index's durable rows (`_safe_timestamps_for`, cold `get_closed_keys`) rather than `{}`/empty — the failure is still recorded, the banner still shows; `get_issues_cached` clears a key's skip/failure when a peer's fresh entry arrives, as `_fresh_entry` does; `fetch_status` uses `snapshot_cached` (the TUI reaches it on its main thread); cache locks are `best_effort_file_lock` so an unwritable cache dir degrades to in-memory as on `main` instead of killing the TUI worker; the explicit refresh (web `/issues/refresh`, TUI `r`) forces the closed-keys lookup too |
| `gh_board.py` | Hand-written GraphQL board query (#255) — **the single largest saving in the issue**. `gh project item-list --limit 200` bills **203 points**; this bills **2 per 100-item page**, and board queries were 96% of a refresh cycle. `_normalize` emits the exact `item-list` JSON shape, so `fetch_project_issues`'s parsing is untouched; equivalence verified live over 151 issues with 0 mismatched fields. `items(first: 200)` is rejected outright with `EXCESSIVE_PAGINATION` (the limit is 100, and it does NOT silently truncate), hence `_PAGE_SIZE = 100` + pagination bounded at `_MAX_PAGES`. A 200 response carrying GraphQL `errors` is a failure, not an empty board: `BoardQueryError` carries `.stderr` so #254's `_fetch_failure` reads the rate-limit wording out of it exactly as it would a non-zero gh exit. Raw subprocess/JSON errors propagate unwrapped — `_fetch_failure` already maps those. Owner type is resolved per owner and cached: GraphQL splits `user(login:)` from `organization(login:)` (unlike `item-list --owner`), so an org login is discovered by falling back, not by failing forever — and the fallback catches `CalledProcessError`, because `gh api graphql` **exits 1** on a GraphQL error envelope (verified live 2026-09-10); catching only the exit-0 envelope left org boards unreachable while exit-0 fixtures passed. When both probes fail the *first* error is raised, so a real user's wrong board number is not reported as "Could not resolve to an Organization". Exhausting `_MAX_PAGES`, or being told more pages exist without a cursor, raises rather than returning the truncated prefix as a success — a partial board would replace the previous good one and be stamped fresh. The capped inner connections carry `totalCount` and emit a warn when they drop values: a Priority past the 8th field silently becomes `None` and a 6th label silently vanishes (measured 2026-09-07: real items use 5 of 8 field slots, so the margin is live) |
| `gh_meter.py` | `run_gh(argv, kind=…)` — the single chokepoint for every `gh` invocation (#255). Owns `GH_TIMEOUT`, classifies the call into the REST or GraphQL bucket (`gh api X` → rest unless `X == "graphql"`; everything else → graphql), and appends a row to a rolling `~/.cache/agent-hq/gh-calls.jsonl` (cap 5000, drop-oldest, `O_APPEND` so concurrent processes need no lock on the hot path; amortized trim under a flock, so the file may sit up to `_TRIM_EVERY-1` rows over the cap). A `FileNotFoundError` is deliberately NOT counted — gh never launched, so no budget was spent; rate-limit rejections and timeouts ARE counted, since the request went out. Counts *calls*, not GraphQL points: points are what GitHub bills but are reported per-request nowhere, and a live `rate_limit` delta is both eventually consistent and polluted by every other process. `summarize(hours)` backs `agent-hq gh-stats`; `graphql_by_kind` counts GraphQL-bucket rows per kind so the point estimate is a direct sum (the bucket is on every row — no reconstructing the pairing from two histograms). `run_gh(cwd=…)` lets `phase.py`'s `gh pr view` route through the meter too (`pr-view`, 1 pt per branch per memo expiry). Known accepted loss: a peer's append landing during the amortized trim goes with the old inode |
| `gh_budget.py` | GitHub rate-limit headroom + degrade policy (#255), on **two tiers of trust**. (1) **Observed rejections are ground truth**: `note_rate_limited(reset_at)` records that a real gh call came back rate-limited, and `allow_background_refresh()` then refuses until reset, short-circuiting the meter. (2) **The meter is advisory**: `snapshot()` samples `gh api rate_limit` `_SAMPLES`=5 times and takes **MIN(remaining)**, memoized 60 s. This is not paranoia — measured 2026-08-26 with GraphQL provably dead, **19 of 30 reads reported `used:0/remaining:5000`**, a full budget. GitHub serves the endpoint from replicas with inconsistent counters and it fails in the dangerous direction; a stale replica can only over-report, so MIN is the safe aggregate. Reads are discarded when every bucket shares one `reset` AND shows zero usage — the signature of a replica minting a window at request time (`_is_synthetic`); a false positive there is harmless, since an untouched budget really is full and "unknown" allows exactly as 5000 would. Deliberately NOT done: filtering on `used == 0` (the legitimate top-of-hour state) or checking the reset is ~1 h out (a constant that decays as the window advances). `None` means unknown, and unknown always ALLOWS — a broken probe must never lock the app out. Floor is `config.github.graphql_floor` (default 500), warn is edge-triggered on the descent. A reading whose `graphql_reset` has passed stops gating — the memo would otherwise keep a below-floor sample authoritative well past the reset that refilled it. `snapshot_cached()` is the accessor for anything on the event loop (the ⚙ row renders inside async routes) or the TUI main thread (`fetch_status`): it answers from the memo and probes on a background thread, because `snapshot()` can sit in five sequential subprocesses. Within one window `remaining` can only fall — a lagging replica under-reports usage, so it can only over-report headroom — so a same-window reading with MORE headroom than the remembered one is discarded; a window without a `reset` is never remembered (it could never expire); `exhausted_reset` prefers the GraphQL bucket, since that value is what an observed rejection pins its stand-down to. Hand-test 2026-09-11: the ⚙ row sat on 5000/5000 through an evening of spending — six REST `rate_limit` reads reported GraphQL `used: 0` with a reset minted per call while GraphQL's own `rateLimit` object said `used: 1798`, and `_is_synthetic` could not fire because the REST index keeps core busy. So the **GraphQL bucket is now read from `rateLimit` itself** (`_read_graphql_once`, free, consistent across reads, and the counter that actually rejects board queries); one REST read then supplies core, and the 5-sample REST fallback runs only when the GraphQL read fails (which is what an exhausted budget does to it) |
| `gh_index.py` | Durable per-repo REST issue index (#255) at `~/.config/agent-hq/cache/issue-index.json` — replaces both 500-row `gh issue list` calls and the #113 per-issue backfill. Cross-process coordinated: reads stat the file and reload on mtime change, writes are a flock'd read-modify-write plus atomic `os.replace`, so a peer's write to a different repo is merged, not clobbered. `timestamps_for()` deliberately returns the same shape `_enrich_dates` consumed from the deleted `_fetch_issue_timestamps`; `closed_keys_for()` is cumulative, retiring the "old closed issue pinned to a board slips past the 500 cap" caveat. **Scoped honestly:** the GraphQL lists it replaces cost **2 points each** — ~2% of the old ~1,246-point cycle, but ~54% of what remains once `gh_board` lands, which is the honest argument for it. It earns its place by removing the backfill, cutting latency, and vacating a contended bucket, NOT by fixing the exhaustion. `sync_repo` tracks `full_synced_at` separately from `synced_at`: stamping one field on every sync makes `now - last_full` ~0 forever, so the daily reseed — the only thing that clears a transferred or deleted issue — never fires in production while still passing a single-sync test. The reseed also has to **replace** rather than merge, or the clock fires at a no-op; a sweep truncated at the page cap still merges, since it has not seen enough to say what is missing. `merge` never lets an older row overwrite a newer one — two syncs racing would otherwise resurrect a closed issue while the cursor kept the newer value, leaving nothing to repair it; `mark_closed` stamps the row's `updated_at` to now for the same reason, so an in-flight sync that fetched the issue open cannot merge over the close. Sweeps page `direction=desc`: numbered pages over a mutable `updated` order skip a row that moves behind the boundary mid-sweep, newest-first turns that into a harmless duplicate, and a sweep truncated at the page cap keeps the *recent* rows a board shows rather than the 3,000 oldest. The cursor advances over the PRs it drops (`seen_through`), or a PR-heavy prefix re-seeds from scratch every sync. `_reload_if_changed` holds the instance lock across stat + read + install — a reader preempted between parse and install would otherwise put an old snapshot under a peer thread's just-merged repo and a writer would persist it. Index locks are `best_effort_file_lock`: an unwritable lock dir degrades to unlocked, never to a crash |
| `gh_stats.py` | `agent-hq gh-stats [--hours N] [--json]` (#255) — calls by bucket and kind over a trailing window, plus **estimated GraphQL points** and remaining headroom. Raw call counts would mislead: `gh project item-list` cost 203 points against a 500-row issue list's 2 and the replacement board query's 2/page, so `GRAPHQL_POINT_COSTS` (the measured table) converts counts to points. Kinds are scored only up to the number of GraphQL calls actually recorded, so a REST kind can't inflate the estimate; an unrecognised GraphQL kind scores 1 point, never 0 |
| `filters.py` | Pure repo/status/source filtering helpers (`filter_issues`, `sort_issues`, `available_filter_options`); legacy priority remains only in the default sort tie-break; shared between TUI and web |
| `adapters.py` | Per-harness adapter registry (#205 Stage 1B), keyed by `AgentType`: `HarnessAdapter` protocol (`launch_args`, `repl_command`, `matches_process`, `resume_spec`, and — Stage 2 #206 — `transcript_phase`/`transcript_summary` dispatching to `transcript.py`/`phase.py` vs `codex_transcript.py`), `ClaudeAdapter`/`CodexAdapter`, `get_adapter()` |
| `capabilities.py` | `probe_launcher_capabilities()` runs `~/.claude/skills/worktree/scripts/launch.sh --capabilities`, validates the versioned JSON contract (`contract_version`, advertised `agents`/`flags`), and degrades to `CLAUDE_ONLY` on any failure (missing binary, non-zero exit, timeout, malformed JSON, wrong contract version) — never raises. Result is cached in-process against the resolved launcher path with a TTL (default 300s); `clear_capabilities_cache()` for tests. AHQ never offers a harness/flag the installed launcher doesn't advertise |
| `dispatch.py` | Classify complexity, generate branch names, prompt templates, execute dispatch. Delivery is verified (#183): `/worktree-create` rides claude's argv (never typed into a REPL), readiness = a harness process in the pane's tree (`_wait_for_harness_process`, reusing discovery primitives — no pane-content scraping), timeout/send failures → `DispatchResult(success=False)` with the pane left open. Harness/model/effort selection (#205 Stage 1B) threads through `resolve_dispatch_selection()` (explicit > `config.dispatch.harnesses[harness]` default > none) into the `/worktree-create --harness … --model … --effort …` flag fragment (`adapters.get_adapter(harness).launch_args()`), emitted only when non-default; the controller pane always runs claude regardless of the dispatched harness. That controller session is pinned (#232) to `claude --permission-mode auto --model opus --effort high` (`CONTROLLER_PERMISSION_MODE`/`CONTROLLER_MODEL`/`CONTROLLER_EFFORT`, passed to `_new_split_with_claude` → `ClaudeAdapter.repl_command`) — it deliberately ignores `config.dispatch.permission_mode`, which has had no reader since personal dispatch was removed (#345; the bare `+ New session` chat window has never had one — `open_chat_window` sends no permission flag), and does NOT reach the dispatched worktree agent (that model/mode comes from `/worktree-create`'s own tier table plus any forwarded `--model`/`--effort`) |
| `issues_hub.py` | Issue↔TriageRecord join layer for the `/issues` hub (#247), shared by web, MCP and TUI for rank sorting (#250): `IssueAnnotation` + `annotate` (plain dict lookup — `Issue.key` and `issue_key` share the `owner/repo#number` format), `queue_partition` (pinned/ranked/scoreless — Ranked requires a confirmed record AND a score; partial confirmed records and complete proposals stay in Untriaged), `filter_by_dimensions`/`dimension_filter_options` (effort/fitness chips over the annotated join — `filters.py` stays pure over Issue for the TUI), `priority_hint_for`, `merge_issue_lists` (the one flatten rule for per-project board lists — drop Done, de-dup by key; shared by the web `_load_issues`/`_load_issues_swr` and `triage_cli` so web and CLI seed/tombstone off that set; MCP and TUI only read it), and `ensure_triage_records` (idempotent auto-seed: read-only diff, locked writes only on change; tombstones via `apply_open_issue_set` scoped to repos present in the loaded list) |
| `tools.py` | Shared deferred tools and `ToolContext`, including an optional injected triage store; `list_agents` (#287) takes `format=text|json` — text appends `@name` (`(derived)` for auto names) and the live native status (`busy`/`waiting: <waitingFor>`/`idle`, from a fresh Claude `status_snapshot` only, else omitted; #415: `waiting 3h12m[: <waitingFor>]` when the wait's start is known, Codex included); JSON records carry `native_name`, `native_name_source`, `native_status`, `waiting_for`, `waiting_since` (ISO 8601) and `session_id` (the transcript sessionId the name joins on); `list_issues` uses shared Queue order, dimension/state filters, rank text and structured JSON; `set_triage` writes supplied dimensions as human without confirming (its `autopilot=` boolean sets the #302 nightly opt-in, which still needs a confirmed record), and `confirm_triage` explicitly approves the currently stored complete record; neither writes GitHub |
| `mcp_server.py` | `_guarded` FastMCP boundary; lazy config/cache/triage store and shared merged open-issue set. Tools: `list_agents`, `jump_to_agent`, `kill_agent`, `mute_agent`, `unmute_agent`, `list_issues`, `dispatch_issues`, `open_chat_session`, `set_status`, `refresh_issues`, `set_triage`, `confirm_triage`. `open_chat_session` (#290) is the MCP twin of the web + button: a bare REPL in `scratch` or `repo:<owner/name>` (no issue, no worktree) via `tools.open_chat_session` → `dispatch.open_chat_window`; unlike the web flow it takes explicit `model`/`effort` (never the dispatch config pins) and a `prompt` that rides the harness argv (adapter `chat_command`), then confirms startup with `_wait_for_harness_process` and returns JSON `{pane_id, session_name, harness, started, prompt_delivered, message}`; effort is validated per harness (`chat_efforts`), the prompt sits after `--`, control characters are rejected (the line is typed into a shell). `list_issues` supports repo/status/effort/fitness/state filters and text/JSON output; `list_agents` passes `format` through (#287). `refresh_issues` forces both board fetches and the closed-keys lookup through `_get_all_issues(force=True)`; ordinary reads retain cache TTLs. Missing/non-directory `triage.data_dir` degrades to storeless with one `"mcp triage store unavailable"` warning per process; only triage writes and dimension/state filters require the store. Never creates the data directory. Store reads never seed; only `set_triage`/`confirm_triage` write triage; `set_triage(autopilot=)` (#302) toggles the human-only autopilot opt-in; GitHub Priority stays read-only |
| `remote/` | Remote MCP for claude.ai / Duet (#396 read-only; #397 writes opt-in via `remote.writes`; setup, data exposure and kill switch in `docs/remote-connector.md`). **Health reads (#412):** `REMOTE_ONLY_READ` = `automations_health`, `recent_errors` (`health.py`), always served, no stdio twin, kept out of `check_categorized` like `REMOTE_ONLY_WRITE`. Each tool builds a plain record, runs `redact.py`'s `redact_value` over all of it once, then caps and formats only from the result, so nothing unredacted reaches a formatter and a redactor failure is a fixed tool error. `automations_health(job=)` collapses lines differing only in their timestamps, then keeps the newest 50 entries of `automations.read_tail`'s 64 KB buffer; the whole read is redacted as one text, so a multi-line secret can't straddle a cut, and a value that never closes masks to the end of the text, which the tool reports (`newest_lines_hidden`). Its logger-error note matches a row's `app` or `source` to the job name over the last hour (`scripts/log.sh` sends the job as `app`, leaving `source` as the host; a failed read isn't retried for 60 s) and never changes a status: a stray copy of a job can share the name while the launchd job is healthy. `redact.py` removes tokens, API keys, cookies, URL query strings, account numbers beyond the last four and home directories; everything else in a log line is returned as written, because the tools exist to give full log detail. Tests patch `health._collect_health` and `health._load_errors`: an unpatched `collect_health` reads the real LaunchAgents and runs `launchctl print`. `LogReadError` text can name the admin token's path, so `_load_page` keeps it local (eventlog warn `"remote logger read failed"`, type only). **Writes (#397):** `REMOTE_WRITE` = `dispatch_issues`, `set_triage`, `mute_agent`, `unmute_agent`, registered (with write `ToolAnnotations`) only when `RemoteSettings.writes`/`RemoteConfig.writes` is true. **Named sessions (#429):** `start_session(name)` is also in `REMOTE_ONLY_WRITE`: it opens an entry of `remote.sessions` (`config.RemoteSession`: `prompt`, optional `cwd` (default `dispatch.scratch_cwd`), `model`, `effort`; every value validated at load, since the launch line is typed into a shell) through `dispatch.open_chat_window(name=…)` in the `scratch` tmux session as `claude --name <name> -- <prompt>`. The caller supplies only the name; an unknown one is `not_allowed` and the refusal lists the allowed names. A live Claude session whose `native_name` equals the name is returned instead of launching (code `existing`, ok, checked before the repeat guard and never counted by it). Same-name repeat within `DISPATCH_DEDUPE_S` is refused (`_MAY_HAVE_STARTED`, under `_SESSION_LOCK`), DAEMON_DOWN refuses, an unconfirmed claude is `launch_uncertain`, a missing folder or no tmux window is `start_failed` (in `_RESOLVED_CODES`, so the row keeps the name); the prompt is capped at 800 bytes (a tty keeps ~1024 bytes of a typed line) and `cwd` must be absolute; `_await_session` polls the cache up to `SESSION_LINK_WAIT_S` (12 s) for the pane's `@name` and Remote Control link. The logger event carries `name` only on resolved codes. Tests stub `dispatch.open_chat_window` / `_wait_for_harness_process`. **Issue writes (#414):** `REMOTE_ONLY_WRITE` also holds `create_issue`, `comment_issue`, `close_issue` — remote-only, no stdio twin (so outside `check_categorized`, which is over stdio names; a test pins them disjoint from the stdio names), served on the same switch with explicit annotations (`close_issue` is `destructiveHint`). Configured repos only (case-insensitive; GitHub's/the board's spelling is used for `mark_closed`, `seed_missing` and keys). `create_issue` refuses the reserved labels (`sentinel`, `deep-audit`) and any label the repo lacks (`github.fetch_label_names`, hand-paged REST, `None` → refuse), a normalized-title match against open board issues, and a same-title create in the last 10 min; then lands it on the repo's board with Status Todo, refetches that board only when `gh_budget.allow_background_refresh` passes, and seeds an untriaged record (`seed_missing`); a failure after the create is `partial` (ok, the issue exists). `comment_issue`/`close_issue` run a fail-closed REST open check (`fetch_issue_summary`, now with `is_pr` — PRs refused); close reasons `completed`/`not_planned`/`duplicate` (`duplicate_of` in a configured repo, `--duplicate-of` with no `--reason`), then `cache.mark_closed`. No live-session or sentinel close guard: the client's own confirmation prompt is the gate. Bodies/comments get a Duet footer (`ISSUE_FOOTER`/`COMMENT_FOOTER`), ≤ 20,000 chars, titles ≤ 256 one-line. A gh timeout raises `github.GHTimeoutError` (a `GHError` subclass) → code `uncertain`; the 10-min repeat guard (`_recent_repeat`, `_MAY_HAVE_LANDED` = dispatch's set + `partial`/`uncertain`; comments match on `body_sha256`; a close GitHub refused is `close_failed` and also counts, since gh posts `--comment` before closing) runs under one `_ISSUE_WRITE_LOCK`. Local duplicate checks run before the label lookup; a seed that left no record is `partial`. Titles/bodies/comments stay in `actions.jsonl`; the logger event carries only `repo`/`reason`/`duplicate_of`/`issue_key` for these tools (never labels), and EVERY `_EMIT_FIELDS` entry, #397's included, is deny-by-default against a strict ASCII `_EMIT_SHAPES` pattern (GitHub's owner/repo/number limits), and identifier fields (`_IDENTIFIER_FIELDS`) go out only on a `_RESOLVED_CODES` outcome (an allowlist: a new code drops them), since args are logged before validation. Each runs through `tools._remote_write`: an `attempt` line in `remote/actions.py`'s `ActionLog` (`<STATE_DIR>/actions.jsonl`, 600, trimmed to 2000) is written BEFORE the action and a failed append refuses it (`ACTION_LOG_DOWN`); then a `result` line with the full local outcome, and `eventlog` info `"remote action"` (`min_interval_s=0`) carrying only tool/keys/session/dims/ok and a fixed `code`; a crash is recorded `crashed` and re-raised. Return text never names a pane id or path. `dispatch_issues` (any open issue, no caps; `model` must match `_MODEL_RE`, since it rides an auto-mode controller's prompt) refuses DAEMON_DOWN, an issue whose number is on a live agent's branch in that repo, a leftover worktree on any typed branch prefix (`_worktree_issue_numbers`: one bounded `git worktree list --porcelain` on the issue's repo, fail-closed), and an issue with a "may have started" attempt (`ok`/`launch_uncertain`/`crashed` or no result line yet; the call's own `action_id`, which `_remote_write` passes to `run`, is excluded) in the last `DISPATCH_DEDUPE_S` (600 s, `_DISPATCH_LOCK` serializes); it maps `DispatchResult.launched` to `launch_uncertain` (`ToolResult.detail` carries the DispatchResult). `set_triage` (value/effort/fitness/reasoning only) writes `actor="duet"` and confirms once all three dims are set and none has `ai` provenance (so a partial edit never approves an unseen AI dim; a staged inbox pin, the user's own tap, folds in as usual); store failures return fixed wording (the error text stays local). Mute/unmute resolve an exact `session_metadata.stable_id` or `@name` from the cache and call `state.set_inactive` (a lock-held set, fail-closed; no capture-pane, mute is sticky #303). `list_agents` adds `remote_control_url` (Claude `https://claude.ai/code/…` only), `include_muted` (the header counts unmuted sessions, `· N muted`), and its `session_id` is now the harness-generic `stable_id`. `agent-hq remote serve` runs a SECOND FastMCP on 127.0.0.1:9894 (fixed host) behind GitHub OAuth, reached through its own Cloudflare tunnel `agent-hq-mcp` (`agents.example.com`) and LaunchAgents `com.benralston.agent-hq.remote` / `…remote-tunnel` (`remote/service.py`: `install [--tunnel]`, `uninstall`, `status`, `kill` = tunnel bootout, server bootout, `pkill -f "agent-hq remote serve"`, plists renamed `*.plist.disabled` so a reboot keeps them off, exit 1 if a job is still loaded; `revoke-all`/`allow-client` use the fixed `STATE_DIR`, never the config; a single `revoke-all` also cancels in-flight sign-ins and token exchanges, since every grant write checks the epoch (an upstream fix, vendored at 29ab497); `RemoteFastMCP.call_tool` sends Claude a fixed type-only error, never exception text). `auth.py` + `request_log.py` are VENDORED verbatim from another project (`vendor.py`: upstream repo, commit, sha256, `SUBSTITUTIONS`) — never hand-edit or reformat them (ruff excludes them); `tests/test_remote_vendor.py` reverses the substitutions and checks the hash, the upstream pins the same hashes, `scripts/check-remote-vendor.sh` diffs against its `origin/main`. `asgi.py` holds the unpinned copies (`RegistrationGuard`, `RemoteFastMCP`, `threaded`). `logger_events.py` implements the hooks the vendored files call (`TokenExchangeMonitor`, `UnhandledExceptionHandler`, `report_config_failure`/`report_server_failure`, stamp-file throttled across launchd restarts, `eventlog.emit(sync=True)` before exit) over eventlog role `remote`; `events.EventingRequestLog` forwards `remote token reuse detected` (error), `remote sign-in denied: identity not allowed` and `remote tool failed` (warn), never login/path. `tools.py`: every stdio tool must be in `REMOTE_READ` (`list_agents`, `list_issues`) or `NOT_REMOTE` (every write, `jump_to_agent`, `refresh_issues`) or serve/check-config refuse to start; the remote `list_agents` reads ONLY `cache.read_cache` (60 s) + `apply_live_status` (no tmux/ps/git/capture-pane, no pane id, cwd or summary; daemon down → `DAEMON_DOWN`), `list_issues` reuses `tools.list_issues` over `mcp_server._get_all_issues(force=False)` with an `Issues as of` line and a stale notice. Config: `remote:` block (`config.RemoteSettings`), secret only in the Keychain (`agent-hq-mcp-remote` / `github-client-secret`); state `~/.config/agent-hq/remote/`, logs `~/Library/Logs/agent-hq/remote*.log`; a 5-minute heartbeat line backs the Automations `stale_after: 20m` entry `check-config` prints |
| `screens/dispatch_screen.py` | Issue backlog DataTable, selection/grouping/dispatch; worker reads shared open issues and triage annotations. Default Queue order has Pinned/Ranked/Untriaged sections; SCORE replaces PRI, RANK breakdown appears at width ≥100 with a store; date modes remain flat |

### Authoritative live status (#276)

`runtime_status.py` reads Claude status from the per-session registry
`~/.claude/sessions/<pid>.json` (#289) — the same files `claude agents --json`
reads, at ~1 ms instead of ~0.17 s per call (collector CPU measured 40.6% → 1.0%
of a core). Records of exited pids are dropped (liveness from the filename,
before parsing). When two live records share a `sessionId` — a crashed session's
file revived by pid reuse, shadowing its resumed twin — one `ps` births read
drops the record whose `procStart` (UTC) mismatches. The format is undocumented, so a
missing/unreadable directory, malformed JSON, a live record whose `peerProtocol`
is not `1`, or a live record without `status`/`sessionId` raises `RegistryUnavailable`, emits
the eventlog warn `"claude status registry unavailable, using cli"`, and that
cycle falls back to `claude agents --settings '{"disableAgentView":false}' --json`
(the command-only override does not change saved settings). Live `status` values
`busy`, `waiting`, and `idle` map to Running, Waiting, and Idle, with `waitingFor`
supplying the explanation. Background-job `state` is not a live source.

`agent-hq codex -- <native arguments>` runs the ordinary remote TUI against a
private Unix-socket app server. `codex_relay.py` observes successful root-thread
selection responses and forwards traffic unchanged, including approval requests.
`codex_runtime.py` owns the frontend/backend lifecycle and registration.
Named profiles (`-p` / `--profile`) and `--oss` are rejected: the native remote
frontend drops parts of their configuration. Explicit `-c` overrides are forwarded
to both processes; `--strict-config` is enforced by the native frontend.
The collector reads only that selected thread: active waiting flags map to
Waiting, active without flags to Running, and idle to Idle. `notLoaded`,
`systemError`, unknown variants/flags, or missing connections map to Unknown.

`RuntimeStatusCollector` in `status_collector.py` polls on a 0.5-second cadence
outside discovery/Git/phase work and atomically writes the versioned
`/tmp/agent-hq/runtime-status.json` snapshot. Sources publish independently;
a failed query invalidates its source. The daemon gates Claude queries off when
discovery finds no Claude sessions and re-enables them on discovery. Frontend
discovery uses live receipts plus pane ancestry independently of the collector's
process-generation check, so slow `ps` can invalidate status without selecting
the app-server child. `apply_live_status()` overlays fresh
observations on live consumers, validating PID/session and, for Codex, pane,
selected thread, receipt generation, and socket. Observations and heartbeat
expire after 1.5 seconds; invalid or ambiguous identity becomes Unknown with a
reason. A discovery cache entry cannot restore expired status.

The web watcher checks status separately every 0.25 seconds and reuses
`agent.invalidated` SSE events. Labels and reasons are escaped backend descriptor
data in the shared status-dot partial; JavaScript does not interpret native
states. The two-second native-transition-to-dashboard requirement is checked
with real harnesses (the measured results are not part of this repository).

Hooks and transcript readers still provide phase instructions, summaries, and
history. The phase resolver reads `<phase>` tags through the harness adapter,
with branch-rule fallback. Terminal text, hook events, transcript recency, and
elapsed-time heuristics never override live status. Legacy unwrapped Codex
sessions remain visible as Unknown until restarted through the wrapper.

Built-in new-chat and recovery paths use the wrapper.
The Claude-owned worktree launcher and tmux-resurrect configuration need a
separate migration; this repo does not modify them. Deployment requires installing the new version and
restarting daemon and web/TUI processes (the main-worktree `./run install-global`
workflow restarts the daemon and installed web service).

### Display Modes

**fzf mode** (default): Uses tiered column widths based on terminal width to prevent layout flash:
- `>= 100`: Wide columns
- `>= 80`: Normal columns
- `>= 70`: Medium columns
- `>= 60`: Narrow columns
- `< 60`: Compact mode (combined repo/branch)

**Dashboard mode** (`--dashboard`): Textual TUI with two tabs:

*Agents tab* — responsive layout:
- `>= 100`: All columns + preview panel
- `>= 80`: All columns, preview auto-hidden
- `< 60`: Compact (State, Repo, Branch only)
- All blocking I/O runs in threaded workers (`@work(thread=True)`)
- Keybindings: j/k, gg/G, /, s, m, K, p, h, [/], Enter, Tab, q
- Both tables (Agents, Dispatch) are `SkipRowTable`
  (`screens/_dispatch_common.py`, #261): the DataTable-native movement keys
  (arrows, PageUp/Down, Ctrl+Home/End) skip header/separator rows with the
  same direction-aware, boundary-reversing rule `j`/`k` use — a focused
  table's own bindings beat the screen's, so the skip must live in the widget
- `h` toggles the preview between the live capture tail and transcript-local
  history (#171); scrolling to the top (Ctrl+U / wheel) pages in earlier turns.
  The mode is sticky: it follows the cursor across agents (transcript-less
  agents fall back to live) and persists via `ui-prefs.json`
- Sort mode (`s`) is restored from `ui-prefs.json` on launch and persisted on
  every toggle; opening in phase sort lands the cursor on the agent in the
  TUI's own tmux session (one-shot, top-of-list fallback) (#176)
- Section order in every sort mode (#141, #278): `📌 Pinned` header section
  on top, the sort-mode body, then a `🔇 Muted` header section last — below
  Done in phase sort. `build_pinned_rows` / `build_muted_rows` are mirrors;
  `_rows_for_display` pulls both sets out of the body, and pinned wins for a
  pinned-and-muted agent

*Dispatch tab* — cross-repo issue backlog:
- Shows open issues in Pinned → Ranked → Untriaged sections with SCORE; RANK
  breakdown appears at width ≥100 when triage is configured. `S` cycles Rank → Created → Updated
- Config: `~/.config/agent-hq/projects.yaml` (repos, projects, scripts)
- Space: select issues, b: group into batch, u: ungroup, Enter: dispatch
- F: chip-filter palette (Repo → Src → Status → Eff → Fit, multi-select;
  absent source/dimension groups hide; palette and chip bar share one definition) — shares
  `filters.filter_issues` with the web view; active chips shown in a bar above
  the table, filters intersect with `/` search (#86)
- Dispatch creates worktrees via shell scripts and starts Claude agents
- Keybindings: j/k, gg/G, /, F, Space, b, u, Enter, r, Tab, q

**Web mode** (`agent-hq web serve`): mobile-first FastAPI/htmx dashboard at
`http://<host>:8000/`. Mirrors the TUI's tabs and adds more:

Every page header carries a **⚙ settings menu** (`partials/settings_menu.html`,
#200): a Theme picker (default | console | neon | terminal | paper |
bubblegum, in that order; midnight/warm/brutalist stay valid-but-hidden —
deep links and old prefs work, re-list in `THEME_PICKER_IDS` to restore)
that posts to
`POST /prefs/theme` (`web/routes/prefs.py`) and 303s back to the referring
page's *path* — persisted in `ui-prefs.json` and applied by `base.html` on
every full page render — plus **Log Out**. A whitelisted `?theme=` query param
overrides the stored value for screenshots, `?theme=default` included.
Theme mechanics (#220): the template-side registry
`templates/partials/_theme_registry.html` (ids/picker-subset/files/labels/
schemes/colors) is imported by `base.html` + `settings_menu.html`; `<html>` carries
`data-theme="<id>"` for per-theme scoped decoration rules; `paper` and
`bubblegum` are the light schemes (color-scheme meta flips, the "dark"
class comes off `<html>`, and `ansi.css`'s `html:not(.dark)` block deepens
the fixed ANSI pastels for the light panes); theme files may override the
structural tokens (`--font-display`, `--border-width`,
`--shadow-card/menu/overlay`, radii/spacing/type scale) but must never
lower `--text-input` (16px iOS zoom floor). Bundled woff2 fonts live in
`static/fonts/` (OFL, latin subsets) and are precached with the theme CSS
by `sw.js`.
It also carries a two-step **Restart** action (#215): `POST /system/restart`
(`web/routes/system.py`) bounces the daemon + snapshot cache and — only when
the serving process is the launchd-managed service — kickstarts the web
service after the response flushes; the interstitial polls `/healthz` for a
new `boot_id`, then returns to `/agents`.

- `/agents` — list / phase / kanban views with SSE-driven live updates.
  **Phase** is the default (`?view=phase`); the **Kanban** toolbar button is
  hidden (#127) but the view still works by direct URL (`?view=kanban`) and the
  route/partial/CSS remain, so it can be re-exposed later. An explicit
  `?view=` is persisted to `ui-prefs.json` and a bare `GET /agents` restores
  it (#176). The header's **+**
  button (#169) opens a picker (Scratch | configured repos) that find-or-creates
  the target tmux session, opens a window running the selected harness's bare
  REPL (`dispatch.open_chat_window`, harness picker gated on
  `probe_launcher_capabilities()` — #205 Stage 1B), and jumps to it
  (`GET/POST /agents/new`, registered before the `/agents/{slug}` routes). An
  agent's detail sheet shows a harness-aware native-URI button (claude.ai
  bridge URL, or `codex://threads/<uuid>`) driven by `session_metadata.native_uri`,
  hidden entirely when no URI is available (#205 Stage 1B). Every view
  brackets its body with a `📌 Pinned` section on top and a `🔇 Muted`
  section last (`partials/pinned_section.html` / `muted_section.html`, #141 /
  #278): muted agents leave their phase group / kanban column and sit below
  Done; pinned wins for a pinned-and-muted agent; `POST …/mute` publishes
  `agent.mute_changed`, which the JS treats like `agent.pins_changed` (whole
  `#agents-view` refetch — a card swap can't cross sections); `agent.removed`
  does the same refetch after deleting the card, so a group never keeps a bare
  header (#338). Every view opens with `partials/agents_status.html`, fed by
  `Watcher.health()` (`web/watcher.py` `CacheHealth.notice`): an error banner
  for a missing or unreadable `cache.json`, a stale banner once the daemon's
  last write is over `STALE_AFTER_S` (60 s) old, and `partials/agents_empty.html`
  for loading vs a genuinely empty list. An unreadable read keeps the snapshot
  instead of counting as zero agents, which used to confirm the removal
  debounce for every card (#338); `cache.write_cache` uses a per-write temp
  name so two daemons can't race on one (#317). The detail
  preview wraps to its column (`.pane-preview` is `pre-wrap`, the host is
  `overflow-x: hidden`, #275) — Live and History never scroll sideways.
- `/automations` — **Automations** health tab (#54, htmx-refreshed every 30 s):
  one row per launchd job (status dot, last run, next fire) when `automations:`
  is configured; rows drill down into a 50-line ANSI log tail in the page's
  `#detail-slot` (`web/routes/automations.py`). The shared nav bar scrolls
  horizontally on narrow phones so every tab stays reachable. An
  **Autopilot** card (#302) sits above the job rows (`#autopilot-section`,
  `GET /automations/autopilot` on load + every 30 s,
  `partials/autopilot_panel.html`; empty when no `autopilot:` block or store):
  Disabled/Paused in its header, the last run (launched / skipped / throttled,
  its error line), a Pause/Resume toggle (`POST …/pause`, form `paused` 0|1,
  shown only when enabled), "Waiting on you" (running / launching / PR open,
  with the PR link) and "Needs attention" (launch failed, ended without PR,
  PR closed unmerged, launch uncertain, and a LAUNCHING reservation older than
  1 h) rows with **Retry** (`POST …/retry`, form `key`; 409 when not
  retryable), plus the last run's skip reasons. A corrupt state file renders an
  "Autopilot state file unreadable" row instead of an empty, unpaused card,
  and both POSTs 503; unparseable attempt records get a "fix state.json by
  hand" count line. When enabled, a GET refreshes in-flight outcomes (real
  gh/tmux probes via `app.state.autopilot_refresh`) at most every 300 s
  per web process, throttled by an in-memory monotonic stamp
  (`app.state.autopilot_last_refresh`, set before the probes run) — never
  `refreshed_at`, so a refresh that changes nothing writes nothing; a refresh
  failure is logged, never a 500. The Needs-attention footer says Retry frees
  the slot and the attempt's worktree/branch (named on each row) must be
  removed before the issue can launch again — except a launch failure whose
  row says "cleaned up" (#312 removes those automatically).
  `create_app(autopilot_store=…, autopilot_refresh=…)` injects both (the
  store defaults to `<triage data_dir>`'s when `autopilot:` is configured).
  The issue detail sheet and the Triage inbox card's Flags row carry the
  matching **⏵ Autopilot** toggle (`dim=autopilot` on the existing triage
  POSTs, a human write).
- `/errors` — **Errors** tab (#148), read side of the unified logger
  (`web/routes/errors.py`, NOT `web/errors.py`): rows from `GET /logs`
  grouped by app (newest group first), level chips (default error+alert) +
  window chips (1h/**24h**/7d) + app chips (computed pre-filter so every app
  stays visible), header ↻ force-refresh (fires a `refresh-errors` body
  event; the section's in-partial listener owns the param-carrying URL),
  htmx 60s poll. Default query served
  stale-while-revalidate from `logreader.ErrorsCache` via
  `app.state.errors_refresher`; non-default filters fetch live (blocking);
  `LogReadError` renders an inline retry panel, never a 500. Row click →
  `/errors/row/{id}/context?app=` drill-down into `#detail-slot` (full
  message, pretty `json_extra`, ±10 surrounding same-app rows all-levels —
  a `since_id`-anchored asc fetch finds focus + following rows at any
  depth, the desc window supplies preceding rows). Enable gate: `logs_configured` /
  `errors_cache` injected via `create_app(...)`; unconfigured (no
  `~/.claude/secrets/logger-admin.token`) → setup hint. Sentinel-labeled
  issues (#235) render machine-filed styling in `/issues` cards +
  detail (`issue-card--sentinel`, `.sentinel-badge`, count line) and the TUI
  dispatch table (`[dark_orange]⚑` title prefix in `build_issue_rows`);
  "untriaged" (badge suffix + count line) means no confirmed triage
  record (#247; since the #248 hand-test only the explicit Confirm flips a
  record — a human dimension edit never does; storeless installs
  fall back to the old priority-set rule; one definition:
  `issues_hub.issue_is_triaged`). The web dispatch filter bar adds a
  capability-dynamic "Src" chip group (`⚑ Sentinel`/`Manual`, only when the
  list mixes both) backed by `filter_issues(sentinels=…)` +
  `DispatchState.sentinels`; the TUI palette exposes the same Src group (#250).
- `/issues` — the issues hub (#247, Phase 2 of the #245 redesign;
  `/dispatch` 301s here; its container/list/issue-detail partial GETs 307
  to their twins). Segmented **Queue | Browse | Triage | Hygiene** modes (Triage added
  #248): explicit `?mode=` is persisted to `ui-prefs.json` (`web_issues_mode`),
  bare GETs restore it, default Queue; every partial/POST route carries
  `mode`, and the
  container's `data-mode` drives the JS SSE refetches. Every
  container/list load runs an idempotent triage pass off the event loop
  (`_annotate_issues` → `issues_hub.ensure_triage_records` +
  `annotate`): open issues get untriaged records seeding the old
  Need/Want/Idea label into `priority_hint` (Phase 3's worker reads it),
  closed ones tombstone (Done-but-open tombstones too — accepted;
  revive-on-reopen heals it). **Queue** (spec's "what next"): pinned
  strip → rank-ordered rows (score chip + inline breakdown chips
  `8 · V3 · S · solo · ★`) → "Untriaged" scoreless section (partition is
  by score presence, not record state); the same chip groups as Browse
  in the hub-wide order Repo → Src → Status → Eff → Fit (shared partial
  `_issue_filter_chips.html`, one cookie — a chip toggled in one mode is
  active in all; #248 hand-test), no search/sort;
  inline pin toggle (`POST …/pin`); shares selection/group/batch-dispatch
  with Browse. **Browse** = the old /dispatch: Sort, then the shared chip
  groups (repo / sentinel / status / effort / fitness — the GH priority
  chips are gone),
  free-text search, sort, multi-select, group, dispatch-via-Claude. State
  (filters + selection + groups) lives in a signed `dispatch_state`
  cookie (`agent_hq/web/state.py`; `efforts`/`fitnesses` replaced
  `priorities` — old cookies decode fine, the key is just never read).
  The detail sheet's **dimension editor** (`POST …/triage`, one POST per
  tap: value 1-5 / effort S-M-L / fitness / ★ interest / ⌖ pin) replaced
  the GH priority picker — the web no longer writes the GH Priority field
  (retained only as legacy input; all surfaces now use the local store); status picker
  and "Won't Do" stay ("→ Personal" went with the Personal tab, #345).
  Sentinel "untriaged" (badge + count line) now means "no confirmed
  triage record". `triage_store=` rides `create_app` injection (built
  from `config.triage.data_dir` when present, else None → Queue renders a
  not-configured hint and the editor hides; Browse works storeless). The
  dispatch form's harness/effort selects and model text input (#205 Stage
  1B) are pre-filled from `config.dispatch.default_harness`/
  `harnesses[...]` and gated by `probe_launcher_capabilities()` — codex is
  only offered when the installed `launch.sh` advertises it. **Triage**
  (#248, the AI-proposal inbox): a segmented-control badge (`Triage · N`,
  `N` = count of PROPOSED records, store-only so it paints with the shell —
  hidden at zero, `partials/triage_badge.html`) sits after Browse (Queue → Browse → Triage → Hygiene since #268). Every container/list load's seeding pass also runs
  `_maybe_kick_triage_worker` inline on the event loop (it is a cheap
  `select_targets` check plus a `shutil.which`; only `run_triage` itself
  goes to the threadpool, inside `_run_triage_background`): if
  `select_targets` finds
  any issue needing a proposal and AI triage is available (store configured
  + `triage.ai_enabled` + `claude` on PATH — `_triage_ai_available`), it
  kicks one `run_triage` call through `app.state.triage_refresher`
  (`BackgroundRefresher("triage-worker", cooldown=600.0)` — singleflight,
  and any run that targeted issues but proposed none — errored or not —
  trips the 600 s cooldown, so a broken `claude` or a model that keeps
  returning nothing can't loop paid calls behind an open tab; "Run now"
  passes `force=True` to clear that cooldown for a deliberate retry).
  Completion publishes SSE `triage.proposed`, which
  the JS refetches the container and `#triage-badge` from — but a run that
  targeted nothing stays silent (no publish, no cooldown), so open tabs
  aren't made to refetch for a run that changed nothing. The mode itself
  renders one `triage_card.html` per PROPOSED issue (Confirm all / Run
  now lead the bar so they stay on-screen at phone width, then the shared
  Repo → Src → Status chips — Eff/Fit are omitted because they'd filter
  on the dimensions still awaiting confirmation; newest proposal first), each showing the three dimension chip rows plus
  the AI's one-sentence reasoning under each row. The detail sheet shows
  that same per-dimension reasoning under each picker plus a
  "Proposed by <model>" line; Queue's Untriaged rows carry an "AI
  proposed" pill (`GET /issues/triage/badge` serves just the badge
  partial for the SSE swap). Tapping a non-active chip → `POST
  /issues/triage/{key}/adjust` (a human write; the record's state is
  untouched, so the card STAYS in the inbox with the new chip active until
  Confirm — a tap is a correction, not an approval); the card's Flags row
  (#267) posts `dim=interest|pin` to the same `/adjust` (tap to set, tap
  again to clear — `_TRIAGE_INBOX_DIMS` now includes both). Interest is a
  plain human write; the pin tap goes to `TriageRecord.pin_staged`
  (`_record_for_dim(stage_pin=True)`), which the Queue never reads —
  `queue_partition` still keys on `pin`, so a Queue/sheet pin floats at
  once whatever the state and an AI proposal landing on a pinned issue
  never moves it (Codex review: gating `pin` on state regressed both) —
  and `TriageStore.confirm`/`confirm_many` fold `pin_staged` into `pin`
  under the lock (a direct `pin` write clears a stale staged value;
  `_merge` demotes a staged write onto a non-PROPOSED record to a direct
  pin — a tap landing after a Confirm elsewhere passes the revision guard
  and would otherwise orphan `pin_staged`, and only the store can decide
  that atomically (Codex round 2); the
  card shows the staged value in place of the effective one, the sheet
  adds a "staged from the Triage inbox" note when they differ); the
  worker's `TRIAGE_OUTPUT_SCHEMA` items are closed (`additionalProperties:
  False`) and `parse_proposals` rejects any batch whose proposal carries an
  extra key, naming `interest`/`pin` as human-only flags; "Confirm" on a card →
  `POST /issues/triage/{key}/confirm`; "Confirm all (N)" in the mode bar →
  `POST /issues/triage/confirm-all` (the button's `hx-vals` posts the keys it
  rendered plus a `revisions` JSON map of each key's `proposed_at`; the route
  confirms only that list ∩ the still-PROPOSED, repo-filtered set, and the
  store skips any key whose stored revision moved — so neither a proposal
  that APPEARED nor one the worker REPLACED between paint and tap is
  confirmed unseen; an empty `keys` — pre-deploy markup — falls back to
  confirming everything visible). The per-card Confirm and chip-tap POSTs
  carry the same `proposed_at`; a stale one re-renders the container (the
  replacement card shows) without writing. The detail sheet (any mode)
  carries the same explicit "Confirm triage" button → `POST
  /issues/issue/{key}/confirm` (re-renders the sheet; enabled once all
  three dims are set, so hand triage from Browse is three chips then
  Confirm, and an UNTRIAGED record confirms without an AI pass); "Run
  now" → `POST /issues/triage/run` (disabled with a hint when AI triage is
  unavailable, per `_triage_ai_available`).
  **Hygiene** (#249, Phase 4): a fourth segment with an open-findings badge
  (`#hygiene-badge`, store-only, `GET /issues/hygiene/badge`). Findings are
  grouped Clusters → Duplicates → Mismatches → Stale; each card lists the
  involved issues (tap → detail sheet), the AI's one-sentence reasoning and
  the proposed action. **Accept…** opens a preview modal listing the exact
  steps (`GET /issues/hygiene/{id}/preview`); **Execute** posts
  `POST /issues/hygiene/{id}/accept` with the card's `found_at` (a finding
  the sweep refreshed since the paint re-renders without acting), runs the
  steps off the loop, marks the finding accepted (or stores the failed step
  log — the card shows "Last attempt failed: …") and publishes
  `issue.invalidated` per touched key. While it runs the modal is a live
  checklist (hand-test round): the route records the plan's labels and each
  step's result on the finding (`HygieneStore.record_progress`, live only
  while the claim is held), `agent_hq.js` polls `GET
  /issues/hygiene/{id}/progress` every 700 ms into the step list, and when
  the POST returns it loads `?final=1` (the released log) and swaps
  Cancel/Execute for Done; the `X-Hygiene-Ran: 0|1` response header tells it
  whether anything ran (a stale `found_at` or refused claim must not present
  an older attempt's log as this one's). **Dismiss** (`POST …/dismiss`,
  same `found_at` guard) suppresses the fingerprint forever. Round 4
  (2026-09-13): Execute first refetches the boards (`_load_issues_fresh`,
  `strict=True`) and refuses with "Confirm the current issue state on
  GitHub" when any repo the finding touches is in
  `GitHubCache.failed_keys()` — a global fetch status would refuse for an
  unrelated broken board; every refusal or exception still stores a plan so
  the final checklist has rows; the background sweep runs on a forced board
  load (`_load_issues(app, force=True)`, falling back to the SWR list only
  when the refetch yields nothing) so cache lag never resolves findings;
  `_claude_on_path` memoizes the `shutil.which("claude")` probe per app for
  60 s and both availability predicates share `_ai_available`; a POST that
  never got a response (no `X-Hygiene-Ran` header) shows a reload hint
  instead of a "final" checklist; `hx-vals` payloads are `| tojson`. **Not on board** (#273) leads the sections, sub-grouped by repo with an
  **Add all (N)** button (`POST /issues/hygiene/offboard/add-all`, form
  `repo`, registered before the `{fid:path}` routes: one strict refetch, then
  claim → plan → execute per OPEN off-board finding of that repo,
  best-effort — a failure lands on that card and the loop continues);
  each card links to GitHub (the issue is not in the board cache, so the
  detail sheet cannot open it) and its Accept button reads **Add to
  board…**. After any landed item-add the route force-refetches that board
  once (`_refresh_boards_after_add`) so the issue appears on the next load;
  `_run_claimed_plan` is the shared execute half of Accept and Add all; Add all plans first, re-reads each finding right before claiming (a sweep finishing mid-loop refreshes `found_at`) and claims only findings with an actionable step, so an issue added by hand is never stored as a failed attempt. The
  sweep trigger passes `collect_offboard_inputs` + `fetch_issue_summary`
  (computed in the threadpool — the index sync is a subprocess). A cluster card
  with more than two issues carries a × per issue → `POST …/remove` (form
  `key` + `found_at`), which reshapes the finding in place and shows an
  "edited" pill (natural-language editing is deferred to #266). The sweep is
  use-triggered: every container load runs `_maybe_kick_hygiene_sweep`,
  which kicks one `run_sweep` through `app.state.hygiene_refresher`
  (`BackgroundRefresher("hygiene-sweep", cooldown=600.0)`) when
  `last_swept_at` is missing or over 24 h old and `_hygiene_ai_available`
  (store + `triage.hygiene_enabled` + `claude` on PATH); completion always publishes
  SSE `hygiene.swept` (error included) so the bar leaves "Sweeping…". "Run now" (`POST
  /issues/hygiene/run`, `force=True`) bypasses both the stamp and the
  cooldown. `hygiene_store=` rides `create_app` injection like
  `triage_store`. No launchd/daemon scheduler — deliberate (#249 design).

### GitHub API cost model (#255, measured 2026-08-26)

GraphQL bills in **points**, and the primary bucket bills roughly per
*request* — **not** weighted by node count, which is what issue #255 assumed:

| Call | Points |
|---|---|
| `gh project item-list --limit 200` | **203** |
| `gh_board` query (per 100-item page) | **2** |
| `gh issue list --state all --limit 500` | **2** |
| `gh api repos/{repo}/issues` (REST) | **0** |

So one refresh cycle *was* ~1,246 points, of which the six board queries were
**96%**. At a 300 s TTL a *single* process refreshing continuously cost
~14,600 points/hour against a 5,000/hour budget — one agent-hq was over budget
roughly 3x by itself, before the 9 MCP servers + TUI + web service each ran
their own clock. **This is why the shared cache alone could not have fixed the
exhaustion**: perfect cross-process dedupe still leaves one process 3x over.

Measured after #255: ~52 points/cycle from `gh_board`, ~24 with the REST index
carrying timestamps and closed state, and flat in the number of processes once
the cache coordinates.

Two traps worth remembering, both cost a live run:

- **`gh api rate_limit` reports a full budget on an exhausted one** — 19 of 30
  reads during a provably dead GraphQL window. It is served from replicas with
  inconsistent counters, and a replica that never saw the window mints a fresh
  one at request time. Never gate "has the budget reset?" on it; gate on a real
  successful call. `gh_budget` is the only thing that should read it.
- **Never measure per-call cost with a single before/after `rate_limit` pair.**
  It produced a false "0 points" reading. Size any trial against the budget
  first — the cost per call is exactly what you don't know yet.

## Testing

Functional verification: `.claude/skills/verify-changes/` (#170) is the project's
behavioral-verification toolkit — a decision matrix mapping diff shape to phases
(CLI smoke, tmux-driven TUI, web endpoint sweep, phone-viewport browser). Run it
before merging changes with a runtime surface; `/save-work` defers to it.

Tests use mocked subprocess calls. Run specific tests:

```bash
./run test -k discovery     # Tests matching 'discovery'
./run test --no-cov         # Skip coverage report
```

Test isolation (#133): an autouse fixture in `tests/conftest.py` redirects every
module-level real-path constant (`/tmp/agent-hq/` cache/states/overrides,
`~/.config/agent-hq/`, `~/.cache/agent-hq/`, `~/.claude/`) into pytest tmp, so
the suite never races the live daemon/web service or user state.
`tests/test_isolation.py` pins the contract — when adding a new real-path
constant, register it in both files. Prefer constructor/parameter injection for
new code (`SnapshotStore(path=...)`, `create_app(github_cache=...)`,
`Watcher(cache_file=..., hooks_dir=...)`).

Path redirection doesn't cover **external services**. A second autouse fixture
(#212) blocks mutating `tmux` subcommands (`new-window`, `send-keys`,
`paste-buffer`/`load-buffer`, `resize-pane`, `kill-*`, `new-session`, …) at the
`subprocess.run`/`Popen` boundary and raises; read-only calls (`list-*`,
`capture-pane`, `has-session`) pass through. `MUTATING_TMUX_SUBCOMMANDS` must
list every mutating subcommand the app shells out to
(`grep -rhoE '"tmux", "[a-z-]+"' agent_hq/`). Any test
exercising dispatch must patch the dispatcher itself — reaching real tmux is
always a mocking gap. #212 was one: an unmocked `POST /personal/dispatch`
spawned a real Claude session in the user's `personal` session on every
full-suite run. Note the guard alone won't fail such a test when the caller
swallows exceptions (the personal route logs and continues), so it prevents
damage rather than reporting it — fix the mock too.

`claude -p` is guarded the same way (#248): an autouse fixture in
`tests/conftest.py` raises on any `claude -p` argv reaching a real
subprocess boundary, so a test exercising `triage_worker.run_triage` must
inject a fake `Runner`; recorded real envelopes for that fake live at
`tests/fixtures/claude_p/*.json`. The hygiene sweep (#249) uses the same fake
`Runner` seam and `hygiene_ok.json` / `hygiene_malformed.json`.

`pgrep` is blocked too (#333): `start_daemon` SIGTERMs every daemon
`_pgrep_daemon_pids` finds other than the PID-file owner, and with
`PID_FILE` redirected into tmp the user's live daemon looked like an orphan —
every full suite run stopped it. The guard raises `FileNotFoundError` (like
`gh`), so the pgrep source degrades to `[]`; the cache-writer source is
already contained by the `CACHE_FILE` redirect. Behind it, the autouse
`_guard_daemon_signals` fixture patches `os.kill` to raise `RuntimeError` on a
real signal to an agent-hq process that is neither the test process nor its
direct child, however the PID was found (signal 0 and non-agent-hq targets
pass; request the fixture and set `ps_reader` to fake `ps` in a test).

The worktree launcher is guarded too (#302): the same autouse fixture raises
on any argv whose program ends in `skills/worktree/scripts/launch.sh`
(`subprocess.run` and `Popen`), so a test exercising the autopilot must
inject a fake `Launcher` into `autopilot.run_launcher` /
`run_autopilot_cli(launcher=…)`. The read-only `launch.sh --capabilities`
probe is exempt. The fixture also redirects `autopilot.LAUNCH_SCRIPT` and
`GOAL_TEMPLATE` (plus `autopilot_cli`'s own `GOAL_TEMPLATE` binding) into
pytest tmp; `tests/fixtures/autopilot/dispatch-condition.md` is a copy of the
real goal template.

Remote mode (#396) is guarded the same way: conftest makes `agent_hq.remote.config.keychain_secret` raise (tests inject `keychain_lookup` or patch it), redirects the remote state/log/LaunchAgents/tunnel paths, and blocks any real `pkill`/`killall` and mutating `launchctl` (`bootout`, `bootstrap`, `kickstart`, …) at the subprocess boundary; `remote/service.py` routes every command through `_run`, which tests patch. GitHub OAuth tests pass `build_remote_app(http_client_factory=…)` an `httpx.MockTransport`.

CI (#136, Phase 1 advisory): `.github/workflows/ci.yml` runs build + lint +
full suite on PRs and pushes to main (ubuntu-latest, uv).
