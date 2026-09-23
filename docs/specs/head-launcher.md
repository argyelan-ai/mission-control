# Head launcher — start one short-lived head per job, any harness on any runtime

Status: draft 2026-09-23, for review. Builds on ADR-085 (head per job) and
needs ADR-086 (see §2) before any code lands.

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

The launcher contradicts four places of ADR-085. They are resolved in writing
before code (PR 0), otherwise we build against our own decision record.

| ADR-085 [code `docs/decisions/085-head-per-job.md`] | Conflict | ADR-086 resolution |
|---|---|---|
| §6.2 "the operator surface is not portable" | operator wants crosswise switching | replaced: every pair gets the same neutral start path and status; vendor features (Remote Control, `/goal`) are extras, never the only way |
| §7 decision 1 "local GPU hosts move from main engine to helper and fallback" | "local first" | replaced: local runtimes are first choice where good enough |
| §3 / §6.1 "configure before building, only a measured gap" | new MC code | gap is measured: Remote Control covers Claude only; omp/openclaude have no vendor UI or remote start. v1 closes exactly that gap |
| §4 frozen task core "no new columns, no 'head' run type" | head needs state | **no DB change at all in v1** — state lives in files (§6.1). Task status is only mirrored with existing values |
| §6.3 "the MC backend knows no harness details" | per-harness start commands | harness commands live only in the host script `mc-head` (one `case` table). The backend knows pair ids and the pair matrix |
| §2 box manager: "refuse a model switch while the engine reports running requests" — not built yet (`grep num_requests\|requests_running backend/app` → 0 hits) [code] | recipe switch could pull the engine from under a running head | built in v1 (it is already allowed by ADR-085) |

## 3. Scope

### v1 (lean — the critic's cut, plus what the operator explicitly asked for)

1. **ADR-086 + this spec + the neutral procedure** (`head-launcher-AGENTS.md`).
2. **Host script `mc-head`** (repo `scripts/head/`, installed to `$MC_HOME/bin`):
   worktree, procedure, one run with time limit, `status.json` + heartbeat,
   PR/run-record detection, stop. Usable without MC from a terminal.
3. **Spool + launchd watcher** on the host: the backend only drops a request
   file; the watcher runs `mc-head`. The backend never gets a shell on the host.
4. **Backend `routers/heads.py`**: pairs, start, list, detail, log tail,
   run record, stop. Restart = start with `restarted_from` + `mode`.
   State is read from files; **no migration**.
5. **Box lock**: max one head per local engine endpoint (lock file), plus the
   recipe switcher refuses a switch while a head holds the box or the engine
   reports running requests.
6. **Pairs in v1** (see §4): omp × local, openclaude × local (after one answer
   proof), claude × local as *experimental*, claude × Claude (after the
   `--bg` end-detection proof).
7. **UI**: pair picker + "Run as head" in New task, head state card in the
   task detail (#655), Restart with…, runs list, busy badge on `/runtimes`,
   warning in the recipe switcher. EN + DE via i18n.
8. **Guard rails**: forbidden list in three layers (§9), Claude quota as a
   **count limit** (§10), minimal environment, own config dirs/profiles.
9. **Needs you without a back-channel**: the head writes a question file and
   exits (code 3); "Answer & continue" is a restart with the answer appended.

### Later (with trigger)

| Later | Trigger |
|---|---|
| `head_runs` table (history queries, reports) | > 200 runs or a report needs SQL; files stay the source of truth |
| omp × Claude | operator decides key vs. subscription (cost / terms); then one answer proof |
| claude × Ollama cloud, openclaude × Ollama cloud as heads | one answer proof each |
| kimi, opencode, codex/acpx | sandbox proven (kimi runs unattended only with `--yolo`); quarterly swap test (ADR-085 §6.6) |
| Quota in percent | the CLI status line writes `rate_limits` to a file (statusline script, PR 4) |
| Two local heads per box | a measurement with the bench standard shows acceptable speed with 2 clients |
| Night / scheduled runs | 5 attended runs without a forbidden-list hit and the sandbox proven |
| Live output stream, ACP driver for heads | never via the frozen sessions-chat code; only if a measured need appears |

## 4. Harness × runtime matrix

Sources: `backend/app/services/harness_compat.py` [code] — harnesses `claude,
openclaude, omp, kimi` (`:25`), protocol sets per harness (`:74-87`:
claude→anthropic, openclaude/omp/hermes→openai, grok→grok, kimi→kimi), one
protocol per runtime derived from `runtime_type` (`runtime_protocol`, `:89`),
and the reason text "Claude Code x OpenAI comes in v2 via proxy" (`:132-142`).
Host CLIs [cli]: claude 2.1.281, openclaude 0.1.8, omp 16.2.13 (kimi, hermes,
grok, opencode installed; codex, acpx not installed — feasibility review).

**The fleet matrix is stricter than reality.** A vLLM engine also serves the
Anthropic route `/v1/messages` (probe: 400 on empty body, not 404) and a
Claude Code run against it executed the Write tool in 17 s [probe]. The fleet
matrix (ADR-056) stays unchanged for the paused fleet; heads get their own
`head_pair_status()` next to it (§6.6).

| Harness ↓ · Runtime → | Local engine (e.g. GLM-5.3-Flash on the local GPU box) | Ollama cloud | Claude (Anthropic subscription) |
|---|---|---|---|
| **omp** | **works — default.** `omp -p --model <provider>/<model>` solved a mini job in 38 s [probe]. Flags `-p`, `--profile`, `--append-system-prompt`, `--max-time`, `--approval-mode` exist [cli]. Config rendered from the runtime row by `render_omp_host_models_yml` (`host_provisioning.py:112`) [code] | works with limitation: allowed by matrix; key goes into the head's omp profile (0600) and is readable by the head (residual risk); unproven as head → later | **does not work in v1**: omp matrix is openai-only (`harness_compat.py:77`); host omp has no anthropic provider [probe]; needs an API key (pay per token) or a subscription login in a third-party CLI (terms/billing) — operator decision |
| **openclaude** | works with limitation: allowed by matrix; `-p`, `--append-system-prompt`, `--permission-mode`, `--bare` exist [cli]; head run **unproven**; host 0.1.8 vs container 0.7.0 → update host first, then answer proof | works with limitation: allowed by matrix, unproven → later | does not work: matrix openai-only; out of scope |
| **claude** (Claude Code) | works with limitation — **experimental**: no proxy needed, `ANTHROPIC_BASE_URL` = engine without `/v1`, `ANTHROPIC_MODEL` = model id, dummy auth token, own empty config dir → uses **no Claude quota** [probe]. Limits: CLI warns `unrecognized_model`, assumes a 200k window while the engine serves 250k, tool use proven on a mini job only. Fleet matrix says "incompatible" (false negative) | works with limitation: route exists (405 on GET) [probe], inference untested → later | works with limitation: `claude --bg` (background session, `claude agents`/`stop`) [cli]; **no `-p`** with the subscription (billing rule of the head procedure). End detection of `--bg` after `/goal` **unproven** → gate for this pair |
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
 Host (launchd)  com.mc.head-starter  ── WatchPaths spool/ ──▶  mc-head start|stop <run_id>
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

`$MC_HOME` defaults to `~/.mc`. One folder per run:

```
$MC_HOME/heads/<run_id>/
  spec.json        written by backend, read-only for the head
  job.md           task title, description, acceptance criteria (+ previous-run context on restart)
  procedure.md     head-launcher-AGENTS.md with placeholders filled
  head.env         0600, only provider env (no MC token, no Claude OAuth token)
  status.json      written only by the mc-head wrapper (atomic tmp + mv)
  heartbeat        touched every 30 s while the process lives
  step.txt         written by the head on every step change ("4/7 sabotage probe")
  question.md      written by the head when it needs the operator (then exit 3)
  head.log         stdout/stderr of the harness
  wt/              the git worktree
  bin/             guard shims (§9)
```

`spec.json`:
```json
{"run_id": "…", "task_id": "…|null", "repo_full_name": "owner/name",
 "base_branch": "main", "branch": "head/2026-09-23-short-ab12",
 "harness": "omp", "runtime_slug": "…", "model": "…", "base_url": "…",
 "box_key": "host:port|null", "time_limit_s": 7200,
 "restarted_from": "…|null", "mode": "fresh|continue",
 "created_by": "user-uuid", "created_at": "…"}
```
A test asserts `spec.json` and `head.env` carry no key matching
`KEY|TOKEN|SECRET|PASSWORD` except the provider key rule of §9.

`status.json` (host → MC contract):
```json
{"run_id": "…", "phase": "starting|running|exited", "pid": 123,
 "external_ref": null, "tmux": "head-ab12cd34",
 "started_at": "…", "exited_at": null, "exit_code": null,
 "last_output_at": "…", "pr_url": null, "run_record_path": null,
 "question": false, "reason": null}
```

Listing runs = scanning `$MC_HOME/heads/*/spec.json` (tens to hundreds of
folders; cached per request). Runs survive task deletion by design (no FK).

### 6.2 Task binding (existing columns only)

- The UI creates the task through the existing `POST …/tasks` with
  `defer_dispatch=true` and without `assigned_agent_id`
  (`routers/tasks.py:207, :618`) [code] — no second create path.
- `POST /heads` then sets `run_control="manual_hold"` on the task
  (`models/task.py:135`) [code]. Dispatch refuses such tasks
  (`services/operations.py:133`) and the watchdog/healers skip them
  (`task_runner.py:472, :590, :1459, :1703`, `watchdog/task_monitor.py:1110, :1446, :1752`)
  [code]. **Zero lines change in dispatch or healers.**
- Status mirror (existing values, written by the heads sync job with a
  `TaskEvent(actor_label="head")`, never through `task_lifecycle`, which would
  trigger review hand-off):

| Head state | Task status | Extra |
|---|---|---|
| starting / running | `in_progress` | – |
| needs_you | `waiting` | comment `comment_type=blocker` with the question — the #655 state card already shows "needs you" for `waiting` (`lib/taskDetail/stateCard.ts:37`) [code] |
| passed | `done` | `pr_url` set on the task; merge stays the operator's 3-stage acceptance |
| failed | `failed` | comment with the reason |
| stopped | `aborted` | – |

### 6.3 Start on the host

1. Backend validates (§6.6), writes the run folder, drops
   `spool/<run_id>.start.json` = `{"action": "start", "run_id": "…"}`.
   The watcher reads **only** `action` and `run_id` and reads everything else
   from `spec.json`; unknown actions are ignored and logged.
2. `mc-head start <run_id>`:
   1. Clone: `$MC_HEAD_CLONES/<repo-name>` (default `~/Workspace/heads`, the
      same place the Remote Control head script uses); missing → `gh repo clone`.
      These clones are dedicated to heads, so repo-level config is safe.
   2. `git fetch origin && git worktree add -b <branch> <run>/wt origin/<base>`.
      Not `git_service.create_task_worktree` (`services/git_service.py:463`)
      [code]: it runs in the container with the container's git token and
      branches `task/<slug>`.
   3. Guards: `core.hooksPath` on the clone → `pre-push` rejects
      `refs/heads/main|master` and non-fast-forward; shim folder first on PATH (§9).
   4. Box lock (local runtimes): `mkdir $MC_HOME/heads/locks/<box_key>` —
      atomic; exists → `status.json` `exited` with `reason=box_busy`, exit.
   5. Start inside `tmux new-session -d -s head-<id8>` so the operator can
      `tmux attach` and take over. Environment: `env -i` with only `HOME`,
      `PATH` (shims first), `LANG`, `TERM` and `head.env`.
3. Harness table — the only place with harness commands:

| Pair | Command in `wt/` |
|---|---|
| omp × local | `omp --profile mc-head-<id8> --model <provider>/<model> -p --max-time <s> --approval-mode <mode> --append-system-prompt <run>/procedure.md "$(cat <run>/job.md)"` — profile rendered by `render_omp_host_models_yml`, never the operator's own profile |
| openclaude × local | `openclaude -p --bare --permission-mode acceptEdits --append-system-prompt "$(cat procedure.md)" "$(cat job.md)"` + `OPENAI_BASE_URL/OPENAI_MODEL` from `head.env` |
| claude × local (exp.) | `CLAUDE_CONFIG_DIR=<run>/claude-config claude -p --bare --permission-mode acceptEdits --append-system-prompt-file procedure.md "$(cat job.md)"` + `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, dummy `ANTHROPIC_AUTH_TOKEN`. `-p` is fine here: no subscription is involved |
| claude × Claude | `CLAUDE_CONFIG_DIR=<head config dir> claude --bg --permission-mode auto --append-system-prompt-file procedure.md "/goal <goal condition>"` — never `-p` |

   `--bare` keeps the operator's own `CLAUDE.md` files out of the head
   (openclaude help: "skip … CLAUDE.md auto-discovery") [cli]; the repo's own
   `AGENTS.md`/`CLAUDE.md` is passed explicitly with `--add-dir` if present
   [assumption: verify `--bare` + `--add-dir` loads it].
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
| exited, code 3, `question.md` present | `needs_you` |
| exited, code 0, `pr_url` present, run record `Status: passed` | `passed` |
| exited otherwise | `failed` with reason (`exit_<n>`, `time_limit`, `no_pr`, `run_record_missing`, `engine_not_ready`, `box_busy`) |
| stop requested and exited | `stopped` |

  `passed` is never claimed by the head alone — all three pieces of evidence
  are required.
- **Sync job** `heads_sync` (scheduler, 60 s): for active runs only, derive the
  state and mirror the task (§6.2). No restart, no kill, no approvals. The
  same derivation runs on every `GET /heads/{id}`.
- **Result detection** (wrapper, after exit): `gh pr list --head <branch>
  --json url`; run record = `$MC_HOME/vault/jobs/*/run-record.md` whose
  frontmatter has `head_run: <run_id>`; `question.md` present?
- **Stop**: spool `<run_id>.stop.json` → `mc-head stop`: omp/openclaude/claude
  × local → SIGTERM to the process group, 10 s, SIGKILL; claude × Claude →
  `claude stop <bg-id>`. Worktree and commits stay. Lock released.
- **Restart with …**: stops the old run if active, then a new run with
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
   `services/heads/pairs.py`; reuses `harness_compat.runtime_protocol` and
   adds the Anthropic-route probe for local engines, cached 10 min).
2. Runtime enabled; local engine: `GET {endpoint}/models` lists
   `model_identifier` (else 409 `engine_not_ready`). Several runtime rows can
   share one endpoint — only the row whose model is currently served is
   offered.
3. Task has a repo (heads need one); max one active run per task (409 `head_active`).
4. Box free (409 `box_busy`); Claude pair: quota count (409 `quota_limit`).
5. Env from `build_runtime_env(runtime, session, Agent(harness=h))` with a
   transient, never-saved agent (`routers/internal.py:29`) [code]. The claude
   branch needs `ANTHROPIC_BASE_URL` + dummy token for the local pair (small,
   tested addition; fleet path unchanged).

### 6.7 Box occupancy and switch lock

- Box key = engine `host:port` from the runtime endpoint, so two runtime rows
  on the same engine share one lock.
- v1 rule: **one head per local box**. Second start → 409 `box_busy`, picker
  shows the pair as busy with the running task's title.
- Recipe switcher: `start_recipe_on_host` (`services/recipe_switcher.py:969`)
  [code] refuses with a new reason when (a) a head lock exists for the box or
  (b) the engine reports running requests (`vllm:num_requests_running` from
  `/metrics` where the engine exposes it; no metric → rule (a) only). The
  runtime watcher's auto-recovery must not restart an engine held by a head
  [assumption: find its restart call during the build; if it is inside frozen
  healer code, the lock check is a safety fix per ADR-085 §4].
- Note: the first ~15 minutes after an engine start are slow (caches empty);
  the run record notes the engine start time so a slow head is not
  misread as broken.

## 7. API (`/api/v1/heads`, new router)

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/heads/pairs?repo_id=` | viewer | all harness × runtime pairs: `status`, `reason_code`, `locality` (local/cloud), `live`, `busy_by` (task id/title), `quota` (Claude pairs), `default_pair` (first live local runtime with omp, else openclaude, else claude × local, else claude × Claude) |
| POST | `/heads` | operator | `{task_id, harness, runtime_slug, restarted_from?, mode?: "fresh"\|"continue", answer?}` → 201 `{run_id, state}` |
| GET | `/heads?task_id=&active=&box=` | viewer | runs (task detail runs list, `/runtimes` occupancy) |
| GET | `/heads/{run_id}` | viewer | spec + derived state + `step`, `silent_s`, `pr_url`, `question` |
| GET | `/heads/{run_id}/log?tail=200` | viewer | last lines of `head.log`, secrets masked |
| GET | `/heads/{run_id}/run-record` | viewer | run-record markdown from the vault (the existing `/tasks/{id}/run-record` stays untouched — frozen) |
| POST | `/heads/{run_id}/stop` | operator | stop request |
| GET | `/heads/occupancy` | viewer | `{box_key: {runtime_slugs, run: {run_id, task_id, title, pair, since}}}` |

Errors are codes (`pair_blocked`, `engine_not_ready`, `box_busy`,
`quota_limit`, `head_active`, `repo_required`, `spool_unavailable`); the
frontend renders them via i18n.

## 8. UI

Design: `DESIGN.md` (no gradients, primary = flat accent surface,
`DESIGN.md:229`) [code]. All strings in `messages/en.json` + `de.json`,
namespace `heads`.

### 8.1 New task → pair picker + Run as head

`components/shared/CreateTaskModal.tsx`: a "Head" section below the repo
selector and a second primary button. Side fix in the same PR: the footer
buttons are hard-coded English ("Cancel", "Create task",
"Cmd+Enter = create", `:442-466`) and the submit button uses a
`linear-gradient` (`:459`) [code] — both against the rules; moved to i18n and
a flat accent surface.

```
┌─ New task ─────────────────────────────────────────────────────────┐
│ Title        [ Fix flaky retry test in upload worker            ] │
│ Description  [ …                                                 ] │
│ Repo         [ owner/mission-control                          ▾ ] │
│                                                                    │
│ HEAD ───────────────────────────────────────────────────────────── │
│ Pair         [ ● omp × GLM-5.3 Flash · local · live           ▾ ] │
│              Runs once in its own worktree, opens a PR, never      │
│              merges. Local — uses no Claude quota.                 │
│                                                                    │
│ Cmd+Enter = create · Esc = close     [Cancel] [Create task] [▶ Run as head] │
└────────────────────────────────────────────────────────────────────┘

 Pair list (opened):
 ┌──────────────────────────────────────────────────────────────┐
 │ LOCAL                                                        │
 │ ● omp × GLM-5.3 Flash                               default  │
 │ ● OpenClaude × GLM-5.3 Flash                     unproven   │  (selectable once proven)
 │ ● Claude Code × GLM-5.3 Flash                experimental   │
 │ ○ omp × Qwen 3.8 27B            engine not running (grey)    │
 │ ◐ omp × GLM-5.3 Flash      busy: "Fix flaky retry…" (grey)   │
 │ CLOUD                                                        │
 │ ● Claude Code × Claude Opus        quota 1 / 2 heads today   │
 │ ○ omp × Claude       needs operator decision (grey, tooltip) │
 │ ○ Kimi Code × Kimi         not supported as head (grey)      │
 └──────────────────────────────────────────────────────────────┘
```
- Default = `default_pair`; the last choice per viewer is remembered in
  `localStorage` (convenience only, wrapped in try/catch).
- No repo → "Run as head" disabled with "A head needs a repo".
- "Run as head" = create task (`defer_dispatch`) → `POST /heads` → open the
  task detail.

### 8.2 Task detail — head state card

`lib/taskDetail/stateCard.ts` `deriveStateCard` gets `headRun | null`; when a
run exists it wins over the task-status card.

```
running
┌──────────────────────────────────────────────────────────────────┐
│ ● Running · omp × GLM-5.3 Flash                     12 min       │
│   Step 4/7 · sabotage probe                                      │
│   Last sign of life 40 s ago                                     │
│   [Stop]  [Restart with …]  [Open log]  [Copy tmux attach]       │
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

passed
┌──────────────────────────────────────────────────────────────────┐
│ ✓ Passed · OpenClaude × GLM-5.3 Flash               34 min       │
│   PR #712 open — your review decides the merge                   │
│   [PR #712 ↗]  [Run record]  [Restart with …]                    │
└──────────────────────────────────────────────────────────────────┘

failed / stopped
┌──────────────────────────────────────────────────────────────────┐
│ ✕ Failed · Claude Code × GLM-5.3 Flash                           │
│   Time limit reached after 2 h. Branch kept (3 commits).         │
│   [Restart with …]  [Open log]  [Run record]                     │
└──────────────────────────────────────────────────────────────────┘
```

Restart with … (popover, same picker component):
```
┌─ Restart with … ─────────────────────────────────────────┐
│ Pair  [ ● Claude Code × Claude Opus   quota 1/2 today ▾ ] │
│ ◉ Continue on this branch (keeps 3 commits)              │
│ ○ Start fresh from main                                  │
│                                  [Cancel] [Restart]      │
└──────────────────────────────────────────────────────────┘
```

- `TaskFactRow`: fact "Head · omp × GLM-5.3 Flash".
- Summary tab: **Runs** list — the crosswise switch becomes visible:
```
 RUNS
 1  omp × GLM-5.3 Flash            failed  · time limit      2 h 00
 2  Claude Code × GLM-5.3 Flash    needs you → answered       18 min
 3  Claude Code × GLM-5.3 Flash    passed  · PR #712          16 min
```
- Polling only while a run is active (10 s), otherwise off.

### 8.3 `/runtimes` — occupancy

```
 ┌─ GLM-5.3 Flash (local) ─────────────────────────────┐
 │ ● serving · 1 head running                          │
 │   omp × GLM-5.3 Flash · "Fix flaky retry…" · 12 min │
 │   [Open task]  [Stop head]                          │
 └─────────────────────────────────────────────────────┘

 Recipe switcher, box held by a head:
 ┌──────────────────────────────────────────────────────┐
 │ ⚠ A head is working on this box ("Fix flaky retry…"). │
 │   Switching now would cut it off. Stop the head first.│
 │   [Open task]                        [Switch] (disabled)│
 └──────────────────────────────────────────────────────┘
```
Badge on the runtime card (`app/runtimes/page.tsx`, `RuntimeRegister`
`:703`) [code] and a "Heads on this runtime" section in the detail panel.

### 8.4 What changes for the operator

- New: pair picker and "Run as head" in New task; head state card with pair,
  step, sign of life, Stop / Restart with … / Answer; runs list; busy badge
  on `/runtimes`; recipe switch blocked while a head holds the box.
- Fixed on the way: New-task footer in German/English, flat primary button.
- Unchanged: board, fleet dispatch button, sessions chat, group chat, the
  existing run-record view for fleet cards.

## 9. Security — forbidden list

The head runs on the host with the operator's user. Honest weighting:

| Layer | What | Strength |
|---|---|---|
| 1 Procedure | "Never" list in `head-launcher-AGENTS.md`: secrets / `.env` / `~/.ssh`, push to `main`, force-push, merge, deploy, docker / ssh / sudo, outbound mail/posts, live-DB writes, deletes outside the worktree | instruction only |
| 2 Tool guards | `pre-push` hook (main/master, force); shim folder first on `PATH` blocking `docker`, `ssh`, `scp`, `sudo`, `launchctl`, `kubectl`; `gh` shim allows only `pr create/view/list`, `repo view`, `auth status`; `env -i` (no inherited secrets); claude: `permissions.deny` in the head's config dir; omp: `--approval-mode` (value decided by proof) | guard rail — bypassable with absolute paths |
| 3 Sandbox | `sandbox-exec` profile (`/usr/bin/sandbox-exec` exists [cli]): deny read of `~/.ssh`, `**/.env*`, `$MC_HOME/secrets`, other Claude config dirs; deny write outside `wt/`, run folder, vault `jobs/`, temp, harness caches | process-level [assumption: works on current macOS incl. network + `gh` keychain; proof with sabotage `cat ~/.ssh/<key>` → denied] |
| Server | branch protection on `main` of every head repo | strongest against push/merge — operator confirms once per repo |

Rules:
- omp and openclaude run **only inside the sandbox** once it is proven; until
  then v1 runs are **attended only** (started from the UI by the operator,
  harmless jobs, no scheduled/night runs).
- `head.env` never contains the MC token or a Claude OAuth token. A cloud
  provider key lives only in the head's omp profile (0600) — readable by the
  head because omp must read it; named as residual risk in ADR-086.
- Log endpoint masks secrets (same masking as the chat tailer).
- kimi stays out (unattended only with `--yolo`).
- Public repo: `mc-head`, docs and defaults use `$HOME` / `$MC_HOME`, "local
  GPU box", no host names, IPs or personal names; `scripts/privacy-scan.py`
  before every push.

## 10. Quota (Claude pairs)

- Policy (ADR-085 §7 decision 3): automatic heads together use at most
  **30 % of the weekly limit**.
- MC cannot measure this today: no `rate_limits`/`seven_day` source in
  `backend/app`, and `services/run_record.py` states Anthropic spend is
  invisible [code]. A percent check in v1 would be a fake.
- **v1 enforcement:** count limit — max **2 claude × Claude heads per day**
  and **1 at a time** (setting `heads_claude_daily_max`), shown as
  "quota 1 / 2 heads today". Unattended starts do not exist in v1.
- **PR 4:** statusline script in the head's Claude config dir writes
  `rate_limits` (5 h / 7 d) to `$MC_HOME/heads/_quota.json`; then the
  gate becomes: sum of 7-day deltas of all Claude heads this week ≥ 30 points
  → 409 `quota_limit`; unknown → 409 with "start anyway" (attended only).
- Local pairs, including claude × local, never count against the quota —
  local first is also the cost guard.

## 11. PR cut

| PR | Content | Traffic light |
|---|---|---|
| 0 | ADR-086 (cross switch, local first, launcher allowed), this spec, `head-launcher-AGENTS.md` | green |
| 1 | `scripts/head/mc-head.sh` + launchd plist template + guards (hook, shims, sandbox profile) + shell tests with a fake harness; pairs omp × local, openclaude × local, claude × local | yellow — new host script, opt-in install |
| 2 | backend: `services/heads/` (pairs, spool, derive state, sync job), `routers/heads.py`, `build_runtime_env` claude-local branch, recipe-switch lock | yellow — new routes, reversible |
| 3 | UI: picker, Run as head, state card, restart, runs, `/runtimes` occupancy, switcher warning, i18n, footer fix | yellow |
| 4 | claude × Claude via `--bg` + statusline quota file + percent gate | yellow |

## 12. Tests and acceptance

TDD, every guard with a sabotage probe (break it → test red → restore → green).

Backend (`pytest`):
1. `head_pair_status`: omp × local ok; claude × local `experimental` only when
   the route probe says the Anthropic route exists; kimi/hermes/grok
   blocked/not offered. Sabotage: drop the route probe → claude × local
   turns blocked → test red.
2. Start validation: blocked pair 422; engine without model 409
   `engine_not_ready`; second run on same task 409; box held 409; Claude count
   reached 409; `spec.json`/`head.env` without token keys; omp profile path
   never the operator's profile.
3. `derive_head_state` with fixture folders: exit 0 + PR + passed → passed;
   exit 0 without PR → failed `no_pr`; exit 3 + question → needs_you; stale
   heartbeat + live pid → running + silent (not failed).
4. Task binding: `manual_hold` set → `check_dispatch_allowed` false; a healer
   pass over an `in_progress` head card changes nothing and creates no
   approval. Sabotage: skip `manual_hold` → red.
5. Recipe switch refused while a head lock exists / engine reports running
   requests. Sabotage: remove the check → red.
6. Spool: watcher contract — only `action` + `run_id`, unknown action ignored.

Host script (shell tests, fake `omp`/`claude` stubs that write a file and exit
0/1/3): status transitions, time limit, lock, `pre-push` rejects main,
shims block `docker`/`ssh`, `gh pr merge` blocked.

Frontend (`vitest`): `deriveStateCard` head variants; picker default = local,
blocked rows with reason, busy row; i18n key parity en/de.

**Answer proof per pair (acceptance, live, attended):** for each pair a mini
job in a scratch repo runs to an open PR with a run record in the vault
(`type: run-record`, `Status: passed`, not in `_rejected/`), and the task card
shows `passed` with the PR chip. Then one crosswise restart on the same
branch: omp × local → claude × local (continue mode) → PR updated.
Config inspection does not count.
Negative proofs: engine stopped → start refused with `engine_not_ready`;
recipe switch during a head → refused; head tries `git push origin main` →
hook rejects, run record notes "blocked"; sandbox: reading `~/.ssh` denied.

Acceptance criteria v1:
- [ ] three pairs proven (omp × local, openclaude × local, claude × local) + one crosswise restart
- [ ] claude × Claude proven or explicitly left disabled with reason
- [ ] zero diff in dispatch, healer, sessions-chat, group-chat files
- [ ] no DB migration
- [ ] all UI strings EN + DE; no gradient in touched components
- [ ] privacy scan clean; no personal names/paths/IPs in code, docs, commits, PR text

## 13. Way back

- Feature switch `heads_enabled` (default off until PR 3 is accepted): off →
  picker hidden, `POST /heads` 404, sync job idle.
- Uninstall host side: `launchctl bootout` the watcher, remove
  `$MC_HOME/bin/mc-head`. Running heads: `mc-head stop <id>`.
- Data: run folders and branches stay (plain files and git); delete with
  `git worktree remove` + folder removal by the operator — never automatic.
- Tasks: head cards are ordinary tasks with `manual_hold`; clearing
  `run_control` returns them to normal behaviour.
- No migration → no downgrade needed.

## 14. Open points (prove during the build)

1. `claude --bg`: does the session end after `/goal` is met, and what does
   `claude agents` print (end detection)?
2. `sandbox-exec` with omp/openclaude/claude: network, `gh` keychain, harness caches.
3. omp `--approval-mode` in `-p`: which value runs unattended without `yolo`?
4. claude × local: tool use stable on a longer job; window setting (engine
   250k vs. assumed 200k); `ANTHROPIC_SMALL_FAST_MODEL` value.
5. openclaude on the host: update from 0.1.8, then the first head run.
6. `--bare` + repo `AGENTS.md`: does the repo's own file still load?
7. Runtime watcher auto-recovery vs. a held box — where the restart call lives.
8. Vault watcher sees run records written from the host (expected yes).
9. Vault frontmatter validator accepts the extra keys `head_run` and `task`
   (`task` is documented in the head procedure; `head_run` is new).
