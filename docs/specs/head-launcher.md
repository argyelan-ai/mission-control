# Head launcher — start one short-lived head per job, any harness on any runtime

Status: draft 2026-09-23, revised after three reviews (principle,
feasibility, risk — see "Review notes" at the end). Builds on ADR-085 (head
per job) and needs ADR-086 (see §2) before any code lands.

Evidence tags used below:
**[code]** = read in this repo at `19cffe2c` (file:line) ·
**[cli]** = checked with `--help` / `--version` on the host ·
**[probe]** = route probe or live run reported by the feasibility review on
2026-09-23, not re-run while writing this spec ·
**[assumption]** = must be proven during the build.

---

## 1. Goal

The operator picks a **harness × runtime pair** on "New task" (local runtime
pre-selected), presses **Run as head**, and MC starts one short-lived coding
session ("head"): one process, one job, its own git worktree from
`origin/main` of the chosen repo, the harness-neutral procedure
(`head-launcher-AGENTS.md`) as its instructions. The task detail view shows
the pair, the state (running / needs you / passed / failed / stopped), a sign
of life, **Stop** and **Restart with …** (another pair). The result is a pull
request plus a run record. `/runtimes` shows which runtime/box is busy.

Principle behind it (operator, 2026-09-23): harness and runtime are switchable
at any time and **crosswise**; local AI is a main component ("local first
where it is good enough"), not a fallback.

Non-goals: a new persistent agent, any change to the frozen dispatch /
healer / sessions-chat / group-chat code, a back-channel into a running
head, auto-restart, merge or deploy by a head.

## 2. Relation to ADR-085 — ADR-086 first

The launcher contradicts several places of ADR-085. They are resolved in writing
before code (PR 0), otherwise we build against our own decision record.

| ADR-085 [code `docs/decisions/085-head-per-job.md`] | Conflict | ADR-086 resolution |
|---|---|---|
| §6.2 "the operator surface is not portable" | operator wants crosswise switching | replaced: every pair gets the same neutral start path and status; vendor features (Remote Control, `/goal`) are extras, never the only way |
| §7 decision 1 "local GPU hosts move from main engine to helper and fallback" | "local first" | replaced: local runtimes are first choice where good enough |
| §3 / §6.1 "configure before building, only a measured gap" | new MC code | gap is measured: Remote Control covers Claude only; omp/openclaude have no vendor UI or remote start. v1 closes exactly that gap |
| §4 frozen task core "no new columns, no 'head' run type" | head needs state | **no DB change at all in v1** — state lives in files (§6.1). Task status is only mirrored with existing values |
| §6.3 "the MC backend knows no harness details" | per-harness start commands | harness commands live only in the host script `mc-head` (one `case` table). The backend knows pair ids and the pair matrix |
| §2 box manager: "refuse a model switch while the engine reports running requests" — not built yet (`grep num_requests\|requests_running backend/app` → 0 hits) [code] | recipe switch could pull the engine from under a running head | built in v1 (it is already allowed by ADR-085) |
| §7 decision 2 "no permission bypass" | omp has no per-command allow/deny list (`omp --help`: `--approval-mode always-ask\|write\|yolo`, `--auto-approve`) [cli]; an unattended `-p` run needs auto-approve | amended: a permission bypass is allowed **only inside the proven sandbox** (§9); before the sandbox proof: scratch repo only |
| (new) fleet matrix `harness_compat.incompat_reason` says Claude Code × local engine "comes in v2 via proxy" | false negative — no proxy needed when the engine serves `/v1/messages` | known deviation: heads use the new shared `runtime_protocols()` (§6.6); the paused fleet adopts the same building block later without a rebuild |

## 3. Scope

### v1 (lean — cut again after the risk review)

1. **ADR-086 + this spec + the neutral procedure** (`head-launcher-AGENTS.md`).
   The neutral file is the **single source** of the procedure core; the
   operator's Claude skill `head-procedure` becomes a thin wrapper that points
   to it and only adds the Claude extras (`/goal`, subagents, statusline) —
   operator-side step recorded in PR 0 (the skill is not in this repo).
2. **Host script `mc-head`** (repo `scripts/head/`, installed to `$MC_HOME/bin`):
   input validation, worktree, procedure, one run with time limit,
   `status.json` + heartbeat, PR/run-record detection, stop, restart.
   Usable without MC from a terminal.
3. **Spool + launchd watcher** on the host (`WatchPaths` + `StartInterval 30`):
   the backend only drops a request file; the watcher runs `mc-head`. The
   backend never gets a shell on the host.
4. **Backend `routers/heads.py`**: pairs, start, list, detail, log tail,
   run record, stop, restart. State is read from files; **no migration**.
5. **Box lock** per GPU box (host ids, not `host:port`), with owner pid so a
   dead wrapper never leaves a box "busy" forever; recipe switch / runtime
   stop / runtime restart refuse to **displace** an engine a head uses.
6. **Pairs in v1** (see §4): **omp × local** (default) and **claude × local**
   (*experimental*, only on runtimes whose engine passes the Anthropic-route
   probe). Two harnesses on the same local engine = crosswise switching with
   0 % Claude quota. openclaude and claude × Claude move to "Later".
7. **UI**: pair picker + "Run as head" in New task (mobile: one main button),
   head state card in the task detail (#655), Restart with…, runs list,
   busy badge on `/runtimes`, warning in the recipe switcher. EN + DE via i18n.
8. **Guard rails**: own weak GitHub identity for heads (§9 — hard gate),
   sandbox, positive allow list for Claude, shims, hook, minimal environment,
   own config dirs/profiles. No Claude-quota pair in v1 → no quota code (§10).
9. **Needs you without a back-channel**: the head writes `question.md` and
   ends; the state comes **only from files** (a model cannot set the harness
   exit code). "Answer & continue" is a restart with the answer appended.

### Later (with trigger)

| Later | Trigger |
|---|---|
| `head_runs` table (history queries, reports) | > 200 runs or a report needs SQL; files stay the source of truth |
| openclaude × local | host update 0.1.8 → 0.7.x, then one answer proof; adds no new crosswise value while claude × local works |
| claude × Claude (subscription) | v1 accepted by the operator; start path = interactive `claude` (no `-p`) in the tmux session, end = process exit + run record; `--bg` + `/goal` only as an extra once its end detection is proven; quota gate of §10 built with it |
| omp × Claude | operator decides key vs. subscription (cost / terms); then one answer proof |
| claude × Ollama cloud, openclaude × Ollama cloud as heads | one answer proof each |
| kimi, opencode, codex/acpx | sandbox proven (kimi runs unattended only with `--yolo`); quarterly swap test (ADR-085 §6.6) |
| Quota in percent | with claude × Claude: the CLI status line writes `rate_limits` to a file (statusline script) |
| Two local heads per box | a measurement with the bench standard shows acceptable speed with 2 clients |
| Night / scheduled runs | 5 attended runs without a forbidden-list hit and the sandbox proven. **Built** as the night shift (ROADMAP E2, `services/heads/night_shift.py`) behind `night_shift_enabled` (default off) — the operator switches it on in Settings once this trigger holds; `mc-head` still refuses real repos without the sandbox and the heads' own GitHub identity |
| Live output stream, ACP driver for heads | never via the frozen sessions-chat code; only if a measured need appears |

## 4. Harness × runtime matrix

Sources: `backend/app/services/harness_compat.py` [code] — harnesses `claude,
openclaude, omp, kimi` (`:25`), protocol sets per harness (`:74-87`:
claude→anthropic, openclaude/omp/hermes→openai, grok→grok, kimi→kimi), one
protocol per runtime derived from `runtime_type` (`runtime_protocol`, `:89`),
and the reason text "Claude Code x OpenAI comes in v2 via proxy" (`:132-142`).
Host CLIs [cli]: claude 2.1.281, openclaude 0.1.8, omp 16.2.13 (kimi, hermes,
grok, opencode installed; codex, acpx not installed — feasibility review).

**The fleet matrix is stricter than reality.** A **vLLM** engine also serves
the Anthropic route `/v1/messages` (probe: 400 on empty body, not 404) and a
Claude Code run against it executed the Write tool in 17 s [probe — vLLM
only, run **without** `--bare`]. Whether the **EXL3** engine (the GLM-5.3
Flash motor proven with omp) serves `/v1/messages` is **open** — the probe
runs per runtime, never per engine family. The route probe becomes one shared
function `runtime_protocols(runtime) -> set[str]` (§6.6); heads use it now,
the paused fleet matrix (ADR-056) stays unchanged and can adopt it later.

| Harness ↓ · Runtime → | Local engine (e.g. GLM-5.3-Flash on the local GPU box) | Ollama cloud | Claude (Anthropic subscription) |
|---|---|---|---|
| **omp** | **works — default.** `omp -p --model <provider>/<model>` solved a mini job in 38 s [probe]. Flags `-p`, `--profile`, `--append-system-prompt`, `--max-time`, `--approval-mode` exist [cli]. Config rendered from the runtime row by `render_omp_host_models_yml` (`host_provisioning.py:112`) [code] | works with limitation: allowed by matrix; key goes into the head's omp profile (0600) and is readable by the head (residual risk); unproven as head → later | **does not work in v1**: omp matrix is openai-only (`harness_compat.py:77`); host omp has no anthropic provider [probe]; needs an API key (pay per token) or a subscription login in a third-party CLI (terms/billing) — operator decision |
| **openclaude** | later: allowed by matrix; `-p`, `--append-system-prompt`, `--permission-mode`, `--bare` exist [cli]; head run **unproven**; host 0.1.8 vs container 0.7.0 → update host first, then answer proof | works with limitation: allowed by matrix, unproven → later | does not work: matrix openai-only; out of scope |
| **claude** (Claude Code) | works with limitation — **experimental, v1**: no proxy needed, `ANTHROPIC_BASE_URL` = engine without `/v1`, `ANTHROPIC_MODEL` = model id, placeholder `ANTHROPIC_API_KEY` (with `--bare` auth is "strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings" [cli]), own empty config dir → uses **no Claude quota** [probe, vLLM, without `--bare`]. Offered only where `runtime_protocols()` contains `anthropic`. Limits: CLI warns `unrecognized_model`, assumes a 200k window while the engine serves 250k, tool use proven on a mini job only. Fleet matrix says "incompatible" (false negative) | works with limitation: route exists (405 on GET) [probe], inference untested → later | later: interactive `claude` in tmux (subscription, **no `-p`**); `claude --bg` + `/goal` only as an extra, its end detection is unproven |
| **kimi** | does not work: protocol fixed to `kimi` (`harness_compat.py:80`), config.toml provider not wired (`:65-73`) | does not work (same) | does not work. Also: unattended only with `--yolo` = permission bypass, forbidden (ADR-085 §7 decision 2) |
| **hermes** | not offered: singleton host bridge, not a one-shot process | not offered | not offered |
| **grok** | not offered: fixed to xAI cloud, `-p` banned fleet-wide (ADR-068) | – | – |
| **opencode** | later: not in the matrix, unverified | later | later |

Status codes the API returns per pair: `ok`, `experimental`,
`blocked` + `reason_code` (`protocol_mismatch`, `harness_not_supported`,
`needs_operator_decision`, `unproven`, `engine_not_ready`, `box_busy`,
`quota_limit`). Text comes from i18n, never from the backend
(`incompat_reason` returns German text today, `:132` [code] — not reused for heads).

A pair moves from `unproven`/`experimental` to `ok` only by an **answer
proof** (§12), recorded in `docs/specs/head-launcher-proofs.md`.

## 5. Architecture overview

```
 Browser (New task / Task detail / Runtimes)
        │  REST
        ▼
 Backend (container)  ── writes ──▶  $MC_HOME/heads/spool/<run_id>.start.json
   routers/heads.py                  $MC_HOME/heads/<run_id>/{spec.json, job.md, procedure.md, head.env}
   services/heads/*    ◀── reads ──  $MC_HOME/heads/<run_id>/{status.json, heartbeat, step.txt,
                                                             question.md, head.log}
        ▲                            $MC_HOME/vault/jobs/<date>-<short>/run-record.md
        │ (same path inside the container: docker-compose.yml:303 mounts ${HOME}/.mc 1:1 [code])
        │
 Host (launchd)  com.mc.head-starter  ── WatchPaths spool/ + every 30 s ──▶  mc-head start|stop|restart <run_id>
                                                                 │ git worktree, tmux, harness
                                                                 ▼
                                                   harness process (omp / openclaude / claude)
                                                                 │ push branch, gh pr create
                                                                 ▼
                                                        GitHub PR (never merged by the head)
```

Why this shape:
- **No shell for the container.** `_ssh_host` (`cli_terminal.py:1158`) [code]
  would let the backend run anything on the host. A spool file with fixed
  fields is an allowlist; the watcher pattern exists already
  (an existing wake watcher script under `scripts/` + its launchd plist). That one is
  host-specific — the new watcher is generic.
- **No token for the head.** The head never calls the MC API; it writes files
  in its run folder, the backend reads them.
- **Harness knowledge only in `mc-head`** (ADR-085 §6.3).

## 6. Components

### 6.1 Data model — files, no migration

`$MC_HOME` defaults to `~/.mc` on the host. **Inside the container the
backend never uses `$HOME` / `Path.home()`**: it derives the heads root from
settings the same way `vault_path` does (`HOME_HOST/.mc`, `config.py:596`)
[code] → new setting `heads_root = HOME_HOST/.mc/heads`. The 1:1 mount
(`docker-compose.yml:303`) makes host and container paths identical. A test
asserts no `Path.home()` in `services/heads/`.

One folder per run:

```
$MC_HOME/heads/<run_id>/
  spec.json        written by backend, read-only for the head
  job.md           task title, description, acceptance criteria (+ previous-run context on restart)
  procedure.md     head-launcher-AGENTS.md with placeholders filled
  head.env         0600, provider env + the head's own weak GH_TOKEN (§9); no MC token, no Claude OAuth token
  head-settings.json  claude only: allow/deny lists (§9)
  .wrapper/        NOT writable by the head (sandbox deny):
    status.json      written only by the mc-head wrapper (atomic tmp + mv)
    heartbeat        touched every 30 s while the process lives
    lock-owner       pid + start time of the wrapper holding the box lock
  step.txt         written by the head on every step change ("4/7 sabotage probe")
  question.md      written by the head when it needs the operator (then it ends)
  head.log         stdout/stderr of the harness
  omp-sessions/    omp only: the harness session (--session-dir) — the token harvester reads usage from it
  claude-config/   claude only: CLAUDE_CONFIG_DIR — the token harvester reads usage from projects/**/*.jsonl
  wt/              the git worktree
  bin/             guard shims (§9)
```

`spec.json`:
```json
{"run_id": "…", "task_id": "…|null", "repo_full_name": "owner/name",
 "base_branch": "main", "branch": "mc-head/2026-09-23-short-ab12",
 "harness": "omp", "runtime_slug": "…", "model": "…", "base_url": "…",
 "box_keys": ["<host-uuid>", "…"], "recipe_slug": "…|null", "time_limit_s": 7200,
 "restarted_from": "…|null", "mode": "fresh|continue",
 "created_by": "user-uuid", "created_at": "…"}
```
A test asserts `spec.json` carries no key matching `KEY|TOKEN|SECRET|PASSWORD`
and `head.env` only the allowed ones (§9: provider key for cloud runtimes,
placeholder `ANTHROPIC_API_KEY`, the head's `GH_TOKEN`).

**`mc-head` never trusts `spec.json`**: the backend container mounts
`~/.mc` read-write, so a compromised backend could plant values that become
shell arguments on the host. Every field is checked against a fixed pattern
before use — `repo_full_name` `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`, `branch`
`^mc-head/[a-z0-9-]+$` (not `head/`: on a case-insensitive disk the tracking ref
`refs/remotes/origin/head/…` collides with `origin/HEAD` — build finding), `run_id` uuid, `harness` one of the `case` table,
`base_url` `^https?://[A-Za-z0-9.-]+:[0-9]+(/[A-Za-z0-9/._-]*)?$`, `model`
`^[A-Za-z0-9._/:-]+$`, `mode` `fresh|continue` — always quoted, never `eval`.
Any mismatch → `status.json` `exited`, `reason=spec_invalid`.

`status.json` (host → MC contract):
```json
{"run_id": "…", "phase": "starting|running|exited", "pid": 123,
 "external_ref": null, "tmux": "head-ab12cd34",
 "started_at": "…", "exited_at": null, "exit_code": null,
 "last_output_at": "…", "pr_url": null, "run_record_path": null,
 "question": false, "reason": null}
```

Listing runs = scanning `$MC_HOME/heads/*/spec.json` (tens to hundreds of
folders; cached per request). Runs survive task deletion by design (no FK):
`delete_task` (`routers/tasks.py:2213`) has no status guard and is **not
touched**. Instead `heads_sync` checks "task missing" → it writes nothing,
the run gets the display flag `task_deleted`, and the run stays visible (and
stoppable) in the runs list on `/runtimes`.

### 6.2 Task binding (existing columns only)

- The UI creates the task through the existing `POST …/tasks` with
  `defer_dispatch=true` and without `assigned_agent_id`
  (`routers/tasks.py:207, :618`) [code] — no second create path — and calls
  `POST /heads` **immediately**. `POST /heads` sets `inbox → in_progress`
  **and** `run_control="manual_hold"` in one transaction, so no operator
  `PATCH` from `inbox` can lift the hold (`routers/tasks.py:1493-1519`
  clears `manual_hold` only when `old_status == "inbox"`) [code]. The gap
  between the two calls is harmless today: the card has no agent and the
  scanners filter on `assigned_agent_id` (`watchdog/task_monitor.py:2403, :2558`).
- Dispatch refuses held tasks (`services/operations.py:133`) and the
  watchdog/healers skip them (`task_runner.py:472, :590, :1459, :1703`,
  `watchdog/task_monitor.py:1110, :1446, :1752, :2032`) [code] — also the
  review scanner (`:1086` then `:1110`). **Zero lines change in dispatch or healers.**
- Status mirror (existing values, written by the heads sync job with a
  `TaskEvent(actor_label="head")`, never through `task_lifecycle`, which would
  trigger review hand-off). The production guard is the Postgres trigger
  `validate_task_transition` (`alembic/versions/0159_task_waiting_status.py:33-43`),
  mirrored by `VALID_TRANSITIONS` (`task_status.py:38-48`) [code]; the SQLite
  test engine does not run the trigger, so every mirror write goes through
  one helper `mirror_path(from, to)` = shortest path in `VALID_TRANSITIONS`,
  each hop checked with `is_valid_transition()`:

| Head state | Target task status | Path from the usual current status |
|---|---|---|
| starting / running | `in_progress` | `inbox → in_progress` · `waiting → in_progress` (answer) · `failed → inbox → in_progress` (restart) · `blocked → in_progress` · `review → in_progress` |
| needs_you | `waiting` | `in_progress → waiting`; comment `comment_type=blocker` with the question — the #655 state card shows "needs you" for `waiting` (`lib/taskDetail/stateCard.ts:37`) [code] |
| passed | `review` | `in_progress → review` · `waiting → in_progress → review`; `pr_url` set on the task. **Not `done`**: the PR is open, merge is the operator's 3-stage acceptance; the operator moves the card to `done` after merging |
| failed | `failed` | `in_progress → failed` · `waiting → blocked → failed`; comment with the reason |
| stopped | `blocked` | `in_progress → blocked` · `waiting → blocked` (same as the fleet's `stop_task_run`, `services/operations.py:225`) [code] — there is **no** transition into `aborted` |

  The hold stays set in every state; the card leaves head control only when
  the operator clears `run_control`.

### 6.3 Start on the host

1. Backend validates (§6.6), writes the run folder, drops
   `spool/<run_id>.start.json` = `{"action": "start", "run_id": "…"}`.
   The watcher reads **only** `action` and `run_id` and reads everything else
   from `spec.json`; unknown actions are ignored and logged.
2. `mc-head start <run_id>`:
   0. Validate `spec.json` (§6.1) — nothing else runs before this.
   1. Clone: `$MC_HEAD_CLONES/<repo-name>` (default `$MC_HOME/heads/clones`,
      no personal path in the public repo); missing → `gh repo clone` with
      the head's `GH_TOKEN`. These clones are dedicated to heads, so
      repo-level config is safe: `credential.helper` is set to
      `!gh auth git-credential` only, and the run uses
      `GIT_CONFIG_GLOBAL=/dev/null` so the operator's keychain helper is
      never reached.
   2. `git fetch origin && git worktree add -b <branch> <run>/wt origin/<base>`.
      Not `git_service.create_task_worktree` (`services/git_service.py:463`)
      [code]: it runs in the container with the container's git token and
      branches `task/<slug>`.
   3. Guards: `core.hooksPath` on the clone → `pre-push` rejects
      `refs/heads/main|master` and non-fast-forward; shim folder first on PATH (§9).
      Both are guard rails only — the server-side gate is the head's own
      non-admin identity (§9).
   4. Box lock (local runtimes), one per host id in `box_keys`:
      `mkdir $MC_HOME/heads/locks/<host-id>` — atomic; the wrapper writes
      pid + start time into `.wrapper/lock-owner` and a copy into the lock
      folder. Lock exists and its owner pid is alive with the same start
      time → `status.json` `exited`, `reason=box_busy`. Owner dead → the lock
      is stale: remove it and take it. Always released on wrapper exit (trap).
   5. Start as a detached process group (tmux only with `MC_HEAD_TMUX=1`).
      Build finding: inside tmux, omp (Bun) could not reach the local engine's
      LAN address while curl in the same session could — macOS Local Network
      privacy treats the tmux server as the responsible process. v1 pairs run
      with `-p`, so there is nothing to take over; `head.log` is the view. Environment: `env -i` with only `HOME`,
      `PATH` (shims first), `LANG`, `TERM` and `head.env`. Inside
      `sandbox-exec` (§9) once the sandbox is proven.
3. Harness table — the only place with harness commands:

| Pair | Command in `wt/` |
|---|---|
| omp × local (v1) | `omp --profile mc-head-<id8> --model <provider>/<model> -p --session-dir <run>/omp-sessions --max-time <s> --auto-approve --append-system-prompt <run>/procedure.md "$(cat <run>/job.md)"` — profile rendered by `render_omp_host_models_yml`, never the operator's own profile. `--auto-approve` is a permission bypass → allowed **only inside the sandbox** or on the scratch repo (ADR-086) |
| claude × local (v1, exp.) | `CLAUDE_CONFIG_DIR=<run>/claude-config claude -p --bare --settings <run>/head-settings.json --append-system-prompt-file procedure.md "$(cat job.md)"` + `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, placeholder `ANTHROPIC_API_KEY` (not `ANTHROPIC_AUTH_TOKEN`: under `--bare` only `ANTHROPIC_API_KEY` or `apiKeyHelper` count [cli]). `-p` is fine here: no subscription is involved. Tool permissions come only from the allow/deny list in `head-settings.json` (§9) — `acceptEdits` alone would refuse every Bash call (tests, git, `gh pr create`) in `-p` |
| openclaude × local (later) | `openclaude -p --bare --settings <run>/head-settings.json --append-system-prompt "$(cat procedure.md)" "$(cat job.md)"` + `OPENAI_BASE_URL/OPENAI_MODEL` |
| claude × Claude (later) | interactive `claude` (no `-p`) in the tmux session with `CLAUDE_CONFIG_DIR=<head config dir>`, `--settings`, `--append-system-prompt-file`; end = process exit + run record. `--bg` + `/goal` only as an extra after its end-detection proof |

   `--bare` keeps the operator's own `CLAUDE.md` files out of the head
   ("skip … CLAUDE.md auto-discovery") [cli] — important because the
   worktree lives under `$HOME` and parent-folder discovery would otherwise
   find the operator's home `CLAUDE.md`. The repo's own `AGENTS.md`/`CLAUDE.md`
   is passed explicitly with `--add-dir` if present [assumption: verify
   `--bare` + `--add-dir` loads it]. For omp, whether it reads rule/context
   files from parent folders is **open** (§14); `--no-rules` / `--no-skills`
   exist [cli] and are used unless the repo's own `AGENTS.md` would be lost.
4. Time limit: macOS has no `timeout`; omp has `--max-time` [cli], the
   wrapper enforces the limit for the others (SIGTERM, 10 s, SIGKILL to the
   process group). Defaults 2 h local, 1 h cloud.

### 6.4 Procedure / AGENTS.md

- Source in the repo: `docs/specs/head-launcher-AGENTS.md` (English,
  harness-neutral; the Claude-specific parts of the head procedure — `/goal`,
  subagents, statusline — are marked optional).
- Handed over via `--append-system-prompt` (all three v1 harnesses support
  it [cli]), **not** written into the worktree: an `AGENTS.md` in `wt/` ends
  up in the PR diff.
- Why not a skill: omp/kimi/hermes/grok have `cli_skills=False`
  (`harness_compat.py:296` `HARNESS_CAPABILITIES`) [code].
- Placeholders filled by the backend: run folder, test/lint/privacy commands
  from the repo row, vault job folder, time limit, pair.

### 6.5 Status, heartbeat, stop, restart, result

- **Heartbeat** = `heartbeat` mtime (process alive) + `last_output_at`
  (mtime of `head.log`) + `step.txt` (where the head is). No watcher acts on
  it. "Silent for > 15 min" is a *display* state, not an error.
- **Derived state** — one pure function
  `derive_head_state(spec, status, heartbeat_mtime, run_record_text, question_exists, now)`:

| Evidence | State |
|---|---|
| `phase` starting | `starting` |
| `phase` running and heartbeat < 90 s | `running` (+ `silent_s`) |
| `phase` running but heartbeat > 90 s and pid gone | `failed`, reason `process_vanished` |
| stop requested and exited | `stopped` |
| exited, `question.md` present, no PR | `needs_you` — **any** exit code: the model cannot set the harness exit code (`exit 3` in a Bash tool call only ends that subshell; `omp -p` / `claude -p` still exit 0) |
| exited, `pr_url` present (found by the wrapper), valid run record `Status: passed` | `passed` |
| scratch repo with a local origin (below): exited, `scratch_branch_pushed` true (checked by the wrapper), valid run record `Status: passed` | `passed`, reason `scratch_branch_pushed`, `pr_url` null |
| exited otherwise | `failed` with reason (`exit_<n>`, `time_limit`, `no_pr`, `run_record_missing`, `engine_not_ready`, `box_busy`, `spec_invalid`) |

  `passed` is never claimed by the head alone. The PR URL is found by the
  wrapper itself (`gh pr list --head <branch>`), `status.json` lives in
  `.wrapper/` which the head cannot write, and a run record counts only when
  its frontmatter `head_run` equals the run id **and** its mtime lies inside
  the run window (vault `jobs/` is writable by other containers today).
- **Scratch repo with a local origin.** A repo listed in
  `heads/scratch-repos` whose clone's `origin` is a local bare repo that
  resolves inside `heads/scratch-origin/` (plain path or `file://`) can never
  have a GitHub PR. For such a run only: the sandbox gets write access to
  exactly that bare repo (profile parameter `SCRATCH_ORIGIN`, empty for every
  real repo), the job text tells the head to push without `gh pr create`,
  and the wrapper sets `scratch_branch_pushed` when the head branch exists
  on that origin with commits beyond the base branch. The backend honours
  the flag only for a repo still listed in `heads/scratch-repos`. Real repos
  are unchanged: they still need a PR.
- **Sync job** `heads_sync` (scheduler, 60 s): for active runs only, derive the
  state and mirror the task (§6.2, via `mirror_path`). Task missing → no
  write, flag `task_deleted`. No restart, no kill, no approvals. The
  same derivation runs on every `GET /heads/{id}`.
- **Result detection** (wrapper, after exit): `gh pr list --head <branch>
  --json url`; run record = `$MC_HOME/vault/jobs/*/run-record.md` whose
  frontmatter has `head_run: <run_id>`; `question.md` present?
- **Stop**: spool `<run_id>.stop.json` → `mc-head stop`: SIGTERM to the
  process group, 10 s, SIGKILL. Worktree and commits stay. Lock released.
- **Restart with …**: one spool action `restart` carrying old and new
  `run_id`. `mc-head restart` stops the old run, **waits** for
  `phase=exited` and the released box lock, and only then starts the new
  one — never two harness processes in the same `wt/`, never a false
  `box_busy` against its own predecessor. Backend: `POST /heads/{id}/restart`
  answers 202; a plain `POST /heads` for the same task stays 409
  `head_active` while the old run is not exited. The new run has
  `restarted_from` and a mode:
  - *Continue on this branch* (default): same worktree/branch; `job.md` gets
    a "previous run" block (pair, `git log origin/main..HEAD`, run record so
    far, question + answer). Git and the run record are the harness-neutral
    memory — chat histories do not transfer.
  - *Start fresh from main*: new branch and worktree.
- **Answer & continue** = Restart with the same pair, mode continue, answer
  appended. There is no message into a running process.

### 6.6 Validation before start (backend)

1. Pair status ≠ `blocked` (`head_pair_status`, new
   `services/heads/pairs.py`). It uses the **shared** new module
   `services/runtime_protocols.py`: `runtime_protocols(runtime) -> set[str]`
   = `harness_compat.runtime_protocol(runtime)` plus `anthropic` when the
   engine answers the `/v1/messages` probe (400/405 = route exists, 404 = no),
   cached 10 min per runtime. The fleet matrix stays untouched (§2).
2. Runtime enabled; local engine: `GET {endpoint}/models` lists
   `model_identifier` (else 409 `engine_not_ready`). Several runtime rows can
   share one endpoint — only the row whose model is currently served is
   offered.
3. Task has a repo (heads need one); max one active run per task (409 `head_active`).
4. Box free (409 `box_busy`). (Claude-quota pairs are not in v1, §10.)
5. Env from `build_runtime_env(runtime, session, Agent(harness=h))` with a
   transient, never-saved agent (`routers/internal.py:29`) [code]. The claude
   branch needs `ANTHROPIC_BASE_URL` + placeholder `ANTHROPIC_API_KEY` for
   the local pair (small, tested addition; fleet path unchanged).

### 6.7 Box occupancy and switch lock

- Box keys = the **host ids** of the runtime from
  `recipe_switcher.runtime_host_ids()` (`services/recipe_switcher.py:523`)
  [code] — for a duo (TP2) recipe all member boxes. Not `host:port`: the same
  box can appear under several names/addresses, and an exclusive recipe on
  another port still displaces the engine (`recipe_claims_box` `:282`,
  `boxes_freed_by_start` `:607`) [code].
- v1 rule: **one head per local box**. Second start → 409 `box_busy`, picker
  shows the pair as busy with the running task's title.
- One helper `heads.box_guard.check_displacement(session, host_ids, action)`
  is called from **every** path that can end an engine: `start_recipe_on_host`
  (`recipe_switcher.py:969`), `POST /runtimes/{id}/stop` (`routers/runtimes.py:868`)
  and `POST /runtimes/{id}/restart` (`:929`) [code]. It refuses (new reason
  `head_on_box`) only a **displacement**: switching to another recipe, or
  stopping/restarting an engine that currently answers, while a live head
  lock (owner pid alive) holds one of the host ids. Rule (b): also refuse
  when the engine reports running requests (`vllm:num_requests_running` from
  `/metrics`) — vLLM only; for EXL3 and engines without the metric
  **rule (a) only**.
- Auto-recovery stays allowed: when the engine is dead, starting the **same**
  recipe again is not a displacement. Evidence: the runtime watcher's
  autostart goes through `recipe_switcher.start_recipe_on_host` for duo
  recipes (`runtime_watcher.py:1216`) and through `runtime_manager.start_runtime`
  for single boxes (`runtime_watcher.py:1228`) [code]; the guard lets both
  through for a dead engine. `runtime_watcher.py` is runtime code, not a
  frozen healer.
- Other users of the same engine (a persistent agent on the same model) are
  not counted by the lock; the picker shows a hint when
  `num_requests_running > 0` where the metric exists.
- Note: the first ~15 minutes after an engine start are slow (caches empty);
  the run record notes the engine start time so a slow head is not
  misread as broken.

## 7. API (`/api/v1/heads`, new router)

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/heads/pairs?repo_id=` | viewer | all harness × runtime pairs: `status`, `reason_code`, `locality` (local/cloud), `live`, `busy_by` (task id/title), `engine_in_use` (other requests running), `default_pair` = **always the best local pair** (omp × the best local runtime, live or not). If that engine is not live, `default_pair.startable=false` + `reason_code=engine_not_ready`; the UI greys out "Run as head" and links to `/runtimes`. **Never** a silent fallback to a cloud pair — a cloud pair is only chosen actively |
| POST | `/heads` | operator | `{task_id, harness, runtime_slug, answer?}` → 201 `{run_id, state}`; moves the task `inbox → in_progress` + `manual_hold` in one transaction |
| POST | `/heads/{run_id}/restart` | operator | `{harness, runtime_slug, mode: "fresh"\|"continue", answer?}` → 202 `{run_id}`; one spool `restart` action (§6.5) |
| GET | `/heads?task_id=&active=&box=` | viewer | runs (task detail runs list, `/runtimes` occupancy) |
| GET | `/heads/{run_id}` | viewer | spec + derived state + `step`, `silent_s`, `pr_url`, `question` |
| GET | `/heads/{run_id}/log?tail=200` | viewer | last lines of `head.log`, masked with the existing `log_redaction.redact_secrets` (`backend/app/log_redaction.py:53`) [code] — no import from the frozen chat tailer |
| GET | `/heads/{run_id}/run-record` | viewer | run-record markdown from the vault (the existing `/tasks/{id}/run-record` stays untouched — frozen) |
| POST | `/heads/{run_id}/stop` | operator | stop request |
| GET | `/heads/occupancy` | viewer | `{box_key: {runtime_slugs, run: {run_id, task_id, title, pair, since}}}` |

Errors are codes (`pair_blocked`, `engine_not_ready`, `box_busy`,
`head_active`, `repo_required`, `spool_unavailable`, `heads_disabled`); the
frontend renders them via i18n.

## 8. UI

Design: `DESIGN.md` (no gradients, primary = flat accent surface,
`DESIGN.md:229`) [code]. All strings in `messages/en.json` + `de.json`,
namespace `heads`.

### 8.1 New task → pair picker + Run as head

`frontend-v2/src/components/shared/CreateTaskModal.tsx`: a "Head" section
below the repo selector. Side fix in the same PR: the footer is hard-coded
English ("Cancel", "Create task", "Cmd+Enter = create", `:442-466`) and the
submit button uses a `linear-gradient` (`:459`) [code] — both against the
rules; moved to i18n and a flat accent surface.

Desktop:
```
┌─ New task ─────────────────────────────────────────────────────────┐
│ Title        [ Fix flaky retry test in upload worker            ] │
│ Description  [ …                                                 ] │
│ Repo         [ owner/mission-control                          ▾ ] │
│                                                                    │
│ HEAD ───────────────────────────────────────────────────────────── │
│ Pair         [ ● omp · GLM local · live                       ▾ ] │
│              Runs once in its own copy of the repo, opens a PR,    │
│              never merges. Local — costs no Claude quota.          │
│                                                                    │
│ Cmd+Enter = create · Esc = close   [Cancel] [Only create task] [▶ Run as head] │
└────────────────────────────────────────────────────────────────────┘

 Pair list (opened) — only startable pairs by default, plain words:
 ┌──────────────────────────────────────────────────────────────┐
 │ ● omp · GLM local            costs no Claude quota · default │
 │ ● Claude Code · GLM local    costs no Claude quota · test    │
 │ Show more (3)                                                │
 └──────────────────────────────────────────────────────────────┘
 "Show more" — greyed rows, each with one plain sentence:
   omp · Qwen local        "The model is not running. Start it on Runtimes."
   omp · GLM local         "Busy with 'Fix flaky retry…'."
   omp · Claude            "Not possible yet — needs an operator decision."
```
Mobile (≤ 640 px): once a repo is chosen, **one** main button
"Run as head" (full width); "Only create task" becomes a secondary text
button below it; the keyboard hint is shown only on `(hover: hover)`
devices. Without a repo the main button is "Create task".

- Default = `default_pair` (always local, §7). Engine not running → the
  pair stays selected, "Run as head" is disabled with "The local model is
  not running — start it on Runtimes" + link. A cloud pair is never
  pre-selected.
- The last choice per viewer is remembered in `localStorage` (convenience
  only, wrapped in try/catch) — but only if it is still startable.
- Hint when the engine serves other requests right now: "The model is also
  working for someone else — may be slower".
- No repo → "Run as head" disabled with "A head needs a repo".
- "Run as head" = create task (`defer_dispatch`) → `POST /heads` → open the
  task detail.

### 8.2 Task detail — head state card

`lib/taskDetail/stateCard.ts` `deriveStateCard` gets `headRun | null`; when a
run exists it wins over the task-status card. Layout rule: **state + one
main action on top** (running → Stop · needs you → Answer · passed → Open
PR · failed/stopped → Restart with …); everything else under "Details"
(log, run record, `tmux attach`), collapsed; "Copy tmux attach" is hidden on
mobile.

```
running
┌──────────────────────────────────────────────────────────────────┐
│ ● Running · omp · GLM local                         12 min       │
│   Step 4/7 · sabotage probe                                      │
│   Last sign of life 40 s ago                                     │
│   [Stop]                                          Details ▾       │
│     Restart with … · Open log · Copy tmux attach (desktop)       │
└──────────────────────────────────────────────────────────────────┘
   (> 15 min without output: "Silent for 22 min" in the warn tone — still running)

needs you
┌──────────────────────────────────────────────────────────────────┐
│ ? Needs you · omp × GLM-5.3 Flash                                │
│   "The job says 'remove the old endpoint' but two callers still  │
│    use it. Recommend: deprecate now, remove later. Without an    │
│    answer: nothing is removed."                                  │
│   [ your answer …                                             ]  │
│   [Answer & continue]   [Restart with …]                         │
└──────────────────────────────────────────────────────────────────┘

passed  (task card moves to "review", not "done")
┌──────────────────────────────────────────────────────────────────┐
│ ✓ Passed · Claude Code · GLM local                  34 min       │
│   PR #712 open — your review decides the merge                   │
│   [Open PR #712 ↗]                                Details ▾       │
└──────────────────────────────────────────────────────────────────┘

failed / stopped
┌──────────────────────────────────────────────────────────────────┐
│ ✕ Failed · Claude Code · GLM local                               │
│   Time limit reached after 2 h. Branch kept (3 commits).         │
│   [Restart with …]                                Details ▾       │
└──────────────────────────────────────────────────────────────────┘
```

Restart with … (popover / bottom sheet on mobile, same picker component):
```
┌─ Restart with … ─────────────────────────────────────────┐
│ Pair  [ ● Claude Code · GLM local                     ▾ ] │
│ ◉ Continue on this branch (keeps 3 commits)              │
│ ○ Start fresh from main                                  │
│                                  [Cancel] [Restart]      │
└──────────────────────────────────────────────────────────┘
```

- `TaskFactRow`: fact "Head · omp · GLM local".
- Summary tab: **Runs** list — the crosswise switch becomes visible:
```
 RUNS
 1  omp · GLM local            failed  · time limit      2 h 00
 2  Claude Code · GLM local    needs you → answered       18 min
 3  Claude Code · GLM local    passed  · PR #712          16 min
```
- Polling only while a run is active (10 s), otherwise off.

### 8.3 `/runtimes` — occupancy

v1: only a **busy badge** on the runtime card (no own section):
```
 ┌─ GLM-5.3 Flash (local) ─────────────────────────────┐
 │ ● serving   [◐ Head working: "Fix flaky retry…"]    │   ← badge links to the task
 └─────────────────────────────────────────────────────┘

 Runs without a task (card deleted) — small list under the runtimes grid:
   omp · GLM local · task deleted · 12 min   [Stop]

 Recipe switcher / runtime Stop / Restart, box held by a head:
 ┌──────────────────────────────────────────────────────┐
 │ ⚠ A head is working on this box ("Fix flaky retry…"). │
 │   Switching now would cut it off. Stop the head first.│
 │   [Open task]                        [Switch] (disabled)│
 └──────────────────────────────────────────────────────┘
```
Badge on the runtime card (`app/runtimes/page.tsx`, `RuntimeRegister`
`:703`) [code]. A "Heads on this runtime" section in the detail panel is
"Later".

### 8.4 What changes for the operator

- New: pair picker and "Run as head" in New task (local pre-selected, only
  startable pairs visible by default); head state card with pair, step, sign
  of life and one main action (Stop / Answer / Open PR / Restart with …);
  runs list; busy badge on `/runtimes`; recipe switch and runtime
  stop/restart refuse to cut off a working head.
- Board: a finished head card lands in **Review** (PR open), not in Done.
- Fixed on the way: New-task footer in German/English, flat primary button,
  mobile footer without wrapping.
- Unchanged: board layout, fleet dispatch button, sessions chat, group chat,
  the existing run-record view for fleet cards.

## 9. Security — forbidden list

The head runs on the host with the operator's user. Honest weighting:

| Layer | What | Strength |
|---|---|---|
| 1 Procedure | "Never" list in `head-launcher-AGENTS.md`: secrets / `.env` / `~/.ssh`, push to `main`, force-push, merge, deploy, docker / ssh / sudo, outbound mail/posts, live-DB writes, deletes outside the worktree | instruction only |
| 2 Tool guards | `pre-push` hook (main/master, force); shim folder first on `PATH` blocking `docker`, `ssh`, `scp`, `sudo`, `launchctl`, `kubectl`; `gh` shim allows only `pr create/view/list`, `repo view`, `auth status`; `env -i` (no inherited secrets); `GIT_CONFIG_GLOBAL=/dev/null` | guard rail — bypassable (absolute paths, `git push --no-verify`, `git -c core.hooksPath=`) |
| 2b Claude allow list | `--settings <run>/head-settings.json` (under `--bare` only managed settings and `--settings` apply reliably [cli]): `permissions.allow` is a **positive list** — `Bash(git add/commit/status/diff/log …)`, `Bash(git push origin mc-head/*)`, `Bash(gh pr create/view/list …)`, the repo's test/lint/privacy commands from the repo row; `permissions.deny`: `Read(~/.ssh/**)`, `Read(**/.env*)`, `Bash(docker/ssh/sudo/curl …)`. Everything else is refused in `-p` | the strongest per-command filter of all harnesses — Claude only |
| 2c omp | **no per-command filter exists** (`--approval-mode always-ask\|write\|yolo`, `--auto-approve` [cli]); `always-ask` blocks a `-p` run → `--auto-approve` | none — omp relies on layers 2 + 3 |
| 3 Sandbox | `sandbox-exec` profile (`/usr/bin/sandbox-exec` exists [cli]): deny read of `~/.ssh`, `**/.env*`, `$MC_HOME/secrets`, the login keychain, other harness config dirs; deny write outside `wt/`, `step.txt`, `question.md`, `head.log`, vault `jobs/`, temp, harness caches — **`.wrapper/` (status, heartbeat) is not writable** | process-level [assumption: works on current macOS incl. network; proof with sabotage `cat ~/.ssh/<key>` → denied] |
| 4 Server identity | heads push and open PRs with their **own weak GitHub identity** (fine-grained token of a non-admin account or a GitHub App: `contents:write` + `pull_requests:write` on the head repos), set as `GH_TOKEN` in `head.env`; the operator's admin token is never reachable (keychain denied by the sandbox, `GIT_CONFIG_GLOBAL=/dev/null`). Plus a ruleset rule "restrict updates" on `main` **without bypass for that identity** | the only layer that holds against push/merge |

**Live finding (2026-09-23, read-only):** `gh api repos/<owner>/mission-control/branches/main/protection`
→ 404 "Branch not protected"; the only ruleset on `main` (`main-merge-queue`:
`required_status_checks, merge_queue, deletion, non_fast_forward`) has
`bypass_actors: OrganizationAdmin, bypass_mode: always`, and the operator's
token is an admin. A head running with the operator's credentials could
therefore bypass hook and shims and push to `main` or run `gh pr merge
--admin`. Hence layer 4 is a **hard gate**: no head runs against a real repo
before it exists and the sabotage probe (`git push --no-verify origin
HEAD:main` and `gh pr merge --admin` from inside a head → both refused by
GitHub) is recorded. Creating the identity and the ruleset rule is an
operator action (GitHub settings), not code.

Rules:
- Before the sandbox is proven, heads run only against a **scratch repo**
  and only attended. The sandbox is the gate for **every** harness and
  **every** repo, scratch included (reviews 2026-09-24): omp runs with
  `--auto-approve`, and Claude's allow list still runs code the head wrote
  itself (`pytest`, `npm test` …) — outside the sandbox that code could read
  secrets, reach the Docker socket, list a real repo in `heads/scratch-repos`
  or fake `.wrapper/status.json`. `mc-head` refuses with `sandbox_required`;
  the launchd template sets `MC_HEAD_SANDBOX=1`.
- The clone's `.git/` is writable by the head (its commits land there), and
  the wrapper runs git in that clone **outside** the sandbox. Before every
  wrapper step `mc-head` therefore rewrites `.git/config` from a fixed key
  set (validated `origin` URL: GitHub for exactly this repo, or — scratch
  only — a local path), removes `commondir` / `config.worktree` /
  `info/attributes`, and passes `-c core.fsmonitor=false`,
  `-c core.hooksPath=<wrapper hooks>` and `-c protocol.ext.allow=never` with
  `GIT_CONFIG_NOSYSTEM=1` on every call. The scratch result check never runs
  git inside the head-writable origin: it fetches from it (upload-pack) into
  the sanitized clone.
- A scratch repo's local origin (`heads/scratch-origin/<name>.git`) is fully
  writable by that scratch run by design (`SCRATCH_ORIGIN`, scratch only).
- On a real repo `mc-head` also checks the heads' identity once a day (and
  whenever `heads/gh-token` changes): no admin/maintain right on the repo,
  and a GitHub rule on the base branch that blocks direct pushes
  (`pull_request`, `required_status_checks`, `merge_queue` or `update`).
  Otherwise the run ends with `gh_identity_unsafe`. `GH_TOKEN` sits in the
  head's environment, so this — not the shims — is what holds.
- Claude heads run with `HOME=<run>/home`: every `~/` deny rule is rendered
  a second time with the real home as an absolute rule, and Edit/Write are
  limited to the worktree and the head's own status files.
- Files the head can write (`step.txt`, `question.md`, `head.log`,
  `run-record.md`) are never read or written through a symlink, neither by
  the wrapper nor by the backend.
- v1 runs are **attended only** (started from the UI by the operator, no
  scheduled/night runs).
- `head.env` never contains the MC token or a Claude OAuth token; allowed
  are the head's own `GH_TOKEN`, the placeholder `ANTHROPIC_API_KEY` and, for
  cloud runtimes (later), the provider key — which lives in the head's omp
  profile (0600) and is readable by the head; named as residual risk in
  ADR-086.
- The vault `jobs/` folder is writable by agent containers today; the run
  record therefore counts only with matching `head_run` and a timestamp
  inside the run window (§6.5).
- Log endpoint masks secrets with `log_redaction.redact_secrets`.
- kimi stays out (unattended only with `--yolo`).
- Public repo: `mc-head`, docs and defaults use `$HOME` / `$MC_HOME`, "local
  GPU box", no host names, IPs or personal names; `scripts/privacy-scan.py`
  before every push.

## 10. Quota (Claude pairs)

**v1: there is no pair that uses Claude quota** (omp × local and claude ×
local both run on the local engine). So v1 builds no quota code; the 30 %
limit is honoured by construction. The design below is built together with
claude × Claude ("Later").

- Policy (ADR-085 §7 decision 3): automatic heads together use at most
  **30 % of the weekly limit**.
- MC cannot measure this today: no `rate_limits`/`seven_day` source in
  `backend/app`, and `services/run_record.py` states Anthropic spend is
  invisible [code]. A percent check in v1 would be a fake.
- **First step with claude × Claude:** count limit — max **2 claude × Claude
  heads per day** and **1 at a time** (setting `heads_claude_daily_max`),
  shown as "quota 1 / 2 heads today".
- **Then:** statusline script in the head's Claude config dir writes
  `rate_limits` (5 h / 7 d) to `$MC_HOME/heads/_quota.json`; then the
  gate becomes: sum of 7-day deltas of all Claude heads this week ≥ 30 points
  → 409 `quota_limit`; unknown → 409 with "start anyway" (attended only).
- Local pairs, including claude × local, never count against the quota —
  local first is also the cost guard.

## 11. PR cut

| PR | Content | Traffic light |
|---|---|---|
| 0 | ADR-086 (cross switch, local first, launcher allowed, bypass only inside the sandbox, fleet-matrix deviation), this spec, `head-launcher-AGENTS.md`; note: operator slims the `head-procedure` skill to a wrapper | green |
| 1 | `scripts/head/mc-head.sh` (validation, worktree, lock with owner, start/stop/restart, result detection) + launchd plist template (`WatchPaths` + `StartInterval`) + guards (hook, shims, sandbox profile, `head-settings.json` template) + shell tests with a fake harness; pairs omp × local, claude × local. **Gate before any real repo:** own GitHub identity + sabotage proof (§9) | yellow — new host script, opt-in install |
| 2 | backend: `services/runtime_protocols.py`, `services/heads/` (pairs, spool, derive state, mirror path, sync job, box guard), `routers/heads.py`, `build_runtime_env` claude-local branch, guard calls in `start_recipe_on_host` and runtimes stop/restart | yellow — new routes, reversible |
| 3 | UI: picker, Run as head (mobile: one button), state card, restart, runs list, `/runtimes` badge, switcher warning, i18n, footer fix | yellow |
| later | openclaude × local; claude × Claude (interactive, then quota gate §10) | – |

## 12. Tests and acceptance

TDD, every guard with a sabotage probe (break it → test red → restore → green).
The concrete test per step is in the build plan at the end; the acceptance
below is what the operator sees.

**Answer proof per pair (acceptance, live, attended):** for each v1 pair a
mini job in a **scratch repo** runs to an open PR with a run record in the
vault (`type: run-record`, `Status: passed`, not in `_rejected/`), and the
task card shows `passed` (board column Review) with the PR chip. The
claude × local proof runs with **exactly** the command of the harness table
(with `--bare`, `--settings`, placeholder `ANTHROPIC_API_KEY`), not the
command of the feasibility test. Then one crosswise restart on the same
branch: omp × local → claude × local (continue mode) → PR updated.
Config inspection does not count.
Negative proofs: engine stopped → start refused with `engine_not_ready`;
recipe switch / runtime stop during a head → refused; head tries
`git push --no-verify origin HEAD:main` and `gh pr merge --admin` → refused
by GitHub; sandbox: reading `~/.ssh` denied; head writes `.wrapper/status.json`
→ denied.

Acceptance criteria v1:
- [ ] two pairs proven (omp × local, claude × local) + one crosswise restart
- [ ] own GitHub identity in place, push-to-main and admin-merge probes refused
- [ ] zero diff in dispatch, healer, sessions-chat, group-chat files and in `delete_task`
- [ ] no DB migration
- [ ] all UI strings EN + DE; no gradient in touched components; 375 px without horizontal scroll
- [ ] privacy scan clean; no personal names/paths/IPs in code, docs, commits, PR text

## 13. Way back

- Feature switch `heads_enabled` (default off until PR 3 is accepted): off →
  picker hidden, `POST /heads` 404, sync job idle, box guard inactive.
- Uninstall host side: `launchctl bootout` the watcher, remove
  `$MC_HOME/bin/mc-head`. Running heads: `mc-head stop <id>`.
- Data: run folders and branches stay (plain files and git); delete with
  `git worktree remove` + folder removal by the operator — never automatic.
- Tasks: head cards are ordinary tasks with `manual_hold`; clearing
  `run_control` returns them to normal behaviour.
- No migration → no downgrade needed.

## 14. Open points (prove during the build)

1. ~~`claude --bg` end detection~~ → moved to "Later" with claude × Claude;
   v1 does not depend on it.
2. `sandbox-exec` with omp/claude: network, `gh` with `GH_TOKEN` (no
   keychain), harness caches. **Gate** for omp on a real repo.
3. ~~omp `--approval-mode` without yolo~~ — **closed**: no such value exists
   (`omp --help`) [cli]; omp runs with `--auto-approve` inside the sandbox (§9).
4. claude × local: tool use stable on a longer job; window setting (engine
   250k vs. assumed 200k); `ANTHROPIC_SMALL_FAST_MODEL` value.
5. ~~openclaude host update~~ → "Later".
6. `--bare` + repo `AGENTS.md`: does the repo's own file still load via `--add-dir`?
7. ~~Runtime watcher auto-recovery vs. a held box~~ — **closed**: autostart
   calls `start_recipe_on_host` for duo recipes (`runtime_watcher.py:1216`)
   and `start_runtime` for single boxes (`:1228`) [code]; the guard refuses
   only displacement of a live engine, recovery of a dead one stays allowed (§6.7).
8. Vault watcher sees run records written from the host (expected yes).
9. ~~Frontmatter validator and extra keys~~ — **closed**: `validate_frontmatter`
   checks only the required fields `id, type, agent, date` and the type
   (`helpers/vault_frontmatter.py:30, :42-49`, `run-record` is a valid type
   `:24`) [code]; extra keys pass. Path ownership is checked only under
   `agents/`, not `jobs/` (`vault_watcher.py:152-164`, per review). A fixture
   test runs the AGENTS.md template through the validator.
10. claude × local under `--bare`: does the placeholder `ANTHROPIC_API_KEY`
    authenticate against the local engine (the feasibility run used no
    `--bare`)? If not: `apiKeyHelper` via `--settings`.
11. Does omp read rule/context files from parent folders of `wt/` (the run
    folder lives under `$HOME`)? If yes: `--no-rules`, and pass the repo's
    own `AGENTS.md` explicitly.
12. Does the EXL3 engine serve `/v1/messages`? Decides whether claude × GLM
    is offered at all; the probe answers it per runtime.
13. launchd `WatchPaths` on files written from the container: measure the
    start latency from the UI click to `phase=starting` (the 30 s interval is
    the safety net).
14. **Night shift — known limits** (ROADMAP E2, decided 2026-09-24):
    - Only cards nobody else works on can be marked: an inbox card nothing
      holds (the mark holds it, `hold_reason = "night shift"`) or a card on
      `manual_hold` (409 `task_busy` otherwise). Right before the start the
      card must still be on hold; a card the fleet or the operator took
      meanwhile is skipped as `task_moved` and listed in the morning report.
    - A lane counts **heads only**. A box whose engine serves fleet agents
      right now (`pairs.engine_in_use`) still gets a night head — same as a
      click by day. Counting it as busy would keep night heads off any box a
      persistent agent is bound to; revisit with a measurement of the slowdown.
    - A local runtime without a linked host (no box keys) is one lane of its
      own (`runtime:<slug>`), held while any head runs on it.
    - Cloud share: floor of the night's marks, but at least one cloud start per
      night while the share is above 0 (0 = no cloud, 100 = no limit).
    - With `HEADS_ENABLED=false` the night-shift API answers 404, so a mark
      cannot be removed there; a card held by a mark (`hold_reason = "night
      shift"`) is released with the normal hold release on the task.
    - MC is the operator's channel (decided 2026-09-24): the morning report
      and the blocked notices show on Home ("Last night" card, one line per
      job, "Answer" for a head that needs you; it stays until dismissed or
      until the next window starts). Slack / Telegram get a copy only with
      `night_shift_send_to_channels` on (Settings → Night shift, default
      off) and a configured report channel.
    - Morning report with the channels on: at most once and at least once per
      night — a claim left by a crash is taken over after 10 min, an
      undelivered report is sent again every 5 min up to 3 attempts, then it
      stays in MC. Messages are cut below 3 500 characters ("… and N more").
      With the channels off it is only stored (`state: stored`), never sent.

## Review notes (2026-09-23)

Three reviews (principle, feasibility, risk) with 28 findings. Code/CLI
claims were re-checked before accepting (`task_status.py:38-48`,
`recipe_switcher.py:282/:523/:607/:969`, `runtime_watcher.py:1216/:1228`,
`routers/runtimes.py:868/:929`, `routers/tasks.py:1493-1519/:2213`,
`task_monitor.py:1086/:1110/:2403/:2558`, `config.py:596`,
`vault_frontmatter.py`, `log_redaction.py:53`, `claude --help` (`--bare`),
`omp --help`, launchd wake-watcher plist, GitHub branch protection + ruleset
read-only).

**Accepted and worked in** (section): `--bare` auth via `ANTHROPIC_API_KEY`
(§4, §6.3, §14.10) · local default without cloud fallback (§7, §8.1) ·
interactive fallback for claude × Claude (Later) · shared
`runtime_protocols()` (§4, §6.6, §2) · neutral file as single source (§3) ·
task transitions via the trigger table: stopped → `blocked`, restart from
`failed` via `inbox`, `waiting` exits via `in_progress`/`blocked` (§6.2) ·
box keys = host ids incl. duo, guard in all three engine-ending paths (§6.7)
· recovery of the same recipe allowed, point 7 closed (§6.7, §14) · restart
as one spool action with wait (§6.5) · deleted task handled by `heads_sync`
without touching `delete_task` (§6.1) · EXL3 route open, rule (a) only (§4,
§6.7) · `HOME_HOST` path (§6.1) · point 9 closed · `WatchPaths` +
`StartInterval` (§5) · hold set atomically with `in_progress` (§6.2) ·
`log_redaction` (§7) · **own weak GitHub identity as hard gate** (§9) · omp
has no per-command filter, sandbox is the gate (§9, §2) · Claude positive
allow list via `--settings` (§6.3, §9) · needs-you from files only, no exit 3
(§6.5, AGENTS.md) · lock with owner pid, stale cleanup (§6.3) · v1 cut to
omp × local + claude × local (§3) · mobile: one main button, plain-words
picker, details collapsed (§8) · head cannot write `.wrapper/`, run record
time window (§6.1, §6.5, §9) · `spec.json` validated in `mc-head` (§6.1) ·
passed → `review`, clones under `$MC_HOME/heads/clones` (§6.2, §6.3).

**Corrected, not rejected:**
- Feasibility #3 / risk #5 say auto-recovery runs through
  `start_recipe_on_host`. True only for duo recipes; single boxes go through
  `runtime_manager.start_runtime` (`runtime_watcher.py:1228`), which the
  guard does not wrap. Result is the same rule (recovery allowed), so the
  finding is accepted with the corrected evidence.
- Risk #10 (parent `CLAUDE.md` for claude × Claude): that pair is not in v1;
  for the v1 claude pair `--bare` already skips discovery. The same risk was
  added for omp as open point 11.

**Rejected / deferred:**
- Risk #6 part "claude × Claude is already covered by Remote Control": not
  used as the reason — Remote Control is a Claude-only extra and the
  principle forbids it as the only way. The pair is deferred for scope
  (quota code, unproven end detection), and comes back with the neutral
  interactive start path.
- Principle #4 "fleet adopts the building block": the fleet stays frozen;
  only the module is built so it *can* be adopted later — no fleet change in v1.

## Build plan (v1)

Order = dependency order. Every step: red test first, then code, then a
sabotage probe (break the guard → test red → restore). One step ≈ one
commit. Work only in the assigned worktree; no push to `main`, no merge, no
deploy; `python3 scripts/privacy-scan.py` before every push.

### Part 0 — decisions and gates (no code)

0.1 ADR-086 + spec + `head-launcher-AGENTS.md` (question flow without exit
    code; frontmatter fields `id, type, agent, date`). Test: fixture run
    record from the template passes `validate_frontmatter`.
0.2 Operator actions (outside the repo): create the heads' GitHub identity
    and the ruleset rule; slim the `head-procedure` skill. Test: recorded
    sabotage probe (§9) in `head-launcher-proofs.md`.

### Part A — Backend and host (PR 1 + PR 2)

A1 `mc-head` input validation. Test (shell): malicious `spec.json`
   (`$(touch /tmp/pwned)` in `repo_full_name`, `branch=main`) → `spec_invalid`,
   no file created. Sabotage: drop the branch pattern → red.
A2 `mc-head start`: clone under `$MC_HOME/heads/clones`, worktree from
   `origin/<base>`, `GIT_CONFIG_GLOBAL=/dev/null`, credential helper, hook,
   shims, tmux, `env -i`. Test with a fake harness: `status.json` goes
   `starting → running → exited`; `pre-push` refuses `main`; shim blocks
   `docker`/`ssh`; `gh pr merge` blocked. Sabotage: remove hook → push test red.
A3 Box lock with owner. Test: second start → `box_busy`; lock of a dead pid
   → taken over; trap releases on SIGTERM. Sabotage: skip the pid check →
   stale-lock test red.
A4 Time limit + stop + restart. Test: fake harness that ignores SIGTERM →
   SIGKILL after 10 s; `restart` starts the new run only after old
   `phase=exited` and lock free (never two pids in `wt/`). Sabotage: drop the
   wait → overlap test red.
A5 Result detection: `gh pr list --head`, run record by `head_run` + time
   window, `question.md`. Test: stub exits **0** and writes only
   `question.md` → `question=true`; run record outside the window ignored.
A6 Harness table + `head-settings.json` template + sandbox profile. Test:
   the claude settings deny `cat ~/.ssh/x` and `rm -rf ~` (fake run);
   sandbox denies write to `.wrapper/`. Live gate: answer proof omp × local
   on the scratch repo.
A7 launchd plist template (`WatchPaths` + `StartInterval 30`) + spool
   reader (only `action`, `run_id`; unknown action logged). Test: unknown
   action ignored; malformed file ignored.
A8 `services/runtime_protocols.py`. Test: vLLM stub answering 400 on
   `/v1/messages` → `{openai, anthropic}`; 404 → `{openai}`; cached.
   Sabotage: skip the probe → claude × local blocked → red.
A9 `services/heads/pairs.py` (`head_pair_status`, `default_pair`). Test: no
   live local engine → default is still omp × local with `startable=false`,
   never a cloud pair; kimi/hermes/grok not offered. Sabotage: add cloud
   fallback → red.
A10 `heads_root` from settings. Test: no `Path.home()` under `services/heads/`
   (grep test); path equals `HOME_HOST/.mc/heads`.
A11 `derive_head_state` (pure). Test with fixture folders: every row of the
   §6.5 table, incl. question with exit 0 → `needs_you`, stale heartbeat +
   live pid → running + silent, head-written `pr_url` ignored.
A12 `mirror_path` + `heads_sync`. Test: every head-state change from every
   reachable task status produces a path where each hop is in
   `VALID_TRANSITIONS`; task deleted → no write, no exception. Sabotage:
   put `aborted` back as the stop target → red.
A13 Box guard `check_displacement` + calls in `start_recipe_on_host`,
   `POST /runtimes/{id}/stop`, `/restart`. Test per path: live engine + live
   lock → refused; dead engine, same recipe → allowed; duo recipe locks both
   hosts. Sabotage: remove the call in one route → that test red.
A14 `routers/heads.py` + `build_runtime_env` claude-local branch. Tests:
   blocked pair 422, `engine_not_ready`, `head_active`, `box_busy`,
   `repo_required`, `heads_disabled` 404; `POST /heads` sets `in_progress` +
   `manual_hold` in one step; `check_dispatch_allowed` false and a healer
   pass changes nothing; `spec.json` without token keys; omp profile never
   the operator's; log tail masked (fake token). Sabotage: skip
   `manual_hold` → dispatch test red.
A15 Live gate (attended, scratch repo): answer proofs omp × local and
   claude × local with the exact table commands; negative proofs of §12.

### Part B — Frontend (PR 3)

B1 i18n namespace `heads` (EN + DE) + footer fix in `CreateTaskModal`
   (i18n, flat accent, no gradient). Test: key parity en/de; no
   `linear-gradient` in the touched file.
B2 API client + types for `/heads*`. Test: error codes map to i18n keys.
B3 Pair picker component. Test: startable pairs first, the rest behind
   "Show more" with one plain sentence each; default local even when the
   engine is down (button disabled + Runtimes link); remembered choice
   ignored when no longer startable. Sabotage: pre-select a cloud pair → red.
B4 "Run as head" flow in New task (create → `POST /heads` → open detail;
   mobile: one main button, keyboard hint only on hover devices). Test:
   375 px without horizontal scroll, footer on one column; no repo →
   disabled with reason.
B5 `deriveStateCard` head variants + state card (one main action, details
   collapsed, tmux hidden on mobile). Test: each state → correct main action;
   silent > 15 min → warn tone, still running.
B6 Restart with … (popover / bottom sheet, continue/fresh, answer field for
   needs-you). Test: sends `mode` + pair; disabled while restart pending.
B7 Runs list + `TaskFactRow` fact + polling only while active. Test:
   polling stops at a final state.
B8 `/runtimes` busy badge, "task deleted" runs with Stop, switcher/stop
   warning on `head_on_box`. Test: badge links to the task; switch button
   disabled with the head's task title.
B9 Browser check (local dev build, desktop + 375 px, light + dark) and the
   operator's "what you can click" list for the PR.
