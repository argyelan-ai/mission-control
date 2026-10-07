# ADR-085 — Head per job: one short-lived coding session per job instead of a persistent agent fleet

**Status:** Accepted (direction; first two-week trial running) · partially supersedes ADR-005, ADR-019, ADR-021, ADR-061 §2, ADR-072 (inbound routing)
**Datum:** 2026-09-22
**Amended by:** [ADR-086](086-harness-runtime-cross-switch.md) — §6 rule 2 (operator surface is swappable too), §7 decision 1 and §8 stop 1 (local first instead of GPU boxes as helpers)
**Amended by:** [ADR-089](089-lead-agent-with-hands.md) — §4 (exception for the lead agent's chat input and driver), §5 (fixed agents = lead agent + one local omp agent), §8 stop 3 (answered); see the note at the end
**Scope:** Architecture/Direction · Backend/Dispatch · Infra/Runtime · Agent Protocol · Docs

## Context

Mission Control grew around a **persistent fleet**: long-running agents with
fixed roles (coder, reviewer, deployer, researcher, …), a lead agent that
receives every new card and delegates (ADR-005), and a large layer that keeps
this fleet alive — delivery into running turns, turn detection, watchdogs and
self-healing, the `mc` back-channel, terminal remote control, Sessions chat,
group chat.

Observed in the weeks before this decision (internal analysis, September 2026):

- **Most engineering time went into keeping the fleet alive, not into work.**
  In a 60-day sample, 129 of 245 fix commits touched the fleet layer (terminal
  remote control, watchdog/healers, Sessions chat). Roughly 70 % of all commits
  in that window were fixes.
- **The healing load sits on the persistent local-model agents.** The agents
  that trigger healers are the ones running on local GPU hosts; runtime
  "unreachable" events occurred on 18 of 24 days sampled.
- **A pilot on 2026-09-21** ran one job as a single interactive coding session
  in its own worktree (plan → failing test → change → sabotage probe → fresh
  reviewer → PR → run record). It finished without any of the fleet machinery.
- **The market now ships what MC set out to build.** Coding CLIs provide
  worktree sessions, background sessions, goal loops, push questions to a
  phone, usage/rate-limit readouts, and (announced 2026-09-17) a hosted
  coordinator with parallel threads; a local variant is announced but not
  available yet.
- **Fleet was already idle.** Healing events dropped from 356 (2026-09-15) to
  0 (2026-09-21/22), with 0 cards `in_progress`. Quiet does not prove the fleet
  holds under load; it does make pausing it cheap.

The question was whether to keep hardening the fleet, or to change the unit of
work.

## Decision

**Every job gets its own short-lived "head": one coding-CLI session in its own
git worktree (branched from `origin/main`), which plans, calls helpers, checks
its own work, writes a run record, and ends.** There are no persistent agents
with fixed roles for new work.

### 1. Head per job

- One job → one head → one worktree → one PR (or a documented abort).
- Helpers are called by the head, not dispatched by MC: in-session subagents
  (cloud), or a local model on a GPU host via a CLI call inside the worktree
  (in: worktree, out: `git diff`).
- The procedure lives in **files**, not in MC code: a skill (`SKILL.md`) plus
  `AGENTS.md` with the goal condition ("tests green, reviewer report present,
  run record written — or abort after N turns"). Questions go to the operator
  only for direction, money, data, or anything visible outside.
- Each head writes a **run record** (Markdown, one page) into the vault:
  outcome, heartbeat line, usage share, operator minutes.

### 2. MC becomes small

What MC keeps as its own:

| Keeps | Notes |
|---|---|
| Rules and procedures | as files (skills, `AGENTS.md`, settings) |
| Vault | Markdown/Git, operator-owned; new folder for jobs + run records |
| Box manager | recipe switching, node agent — frozen, plus one addition: refuse a model switch while the engine reports running requests |
| Metrics | usage capture, daily digest, actor tracking; add operator minutes per job and usage share |
| Cross-cutting core | API, DB/migrations, auth, scheduler — unchanged |

The operator UI for starting jobs, answering questions and overview comes from
the coding CLI vendor, not from MC (see §5).

### 3. Configure before building

Before any new MC code: at most three evenings of trying what already exists
(CLI features, settings, hooks, existing tools), zero MC code. Only a
**measured** gap gets built.

### 4. Build stop

The following areas get **safety and operations fixes only** — no features:

- Dispatch (delivery, lead-first routing, turn detection, `mc` back-channel)
- Watchdog / healers
- Sessions chat
- Group chat

Also frozen (running, no expansion): task core (no new columns, no "head" run
type), Slack/Telegram adapters, voice agent, run-record endpoint for existing
cards, the admin MCP tool (stays an operator tool, never handed to heads).

### 5. Quiet mode (reversible)

All persistent agents except the lead agent and the voice agent are set to
`operational_mode = "paused"` (live since 2026-09-22: 12 paused, 2 active).
Paused agents receive no dispatch (`services/operations.py:147`) and are
skipped by the stuck-work watchdog (`services/task_runner.py:1732`).
Configuration stays; nothing is deleted. **Way back:** set the agent to
`active` again and start its container.

Agents are **never deleted** in this phase (known FK issue on agent delete).
Code is deleted only after two weeks of measured non-use, in themed PRs.

### 6. House rules

1. **Configure before building:** ≤ 3 evenings of trial, 0 MC code, before
   any build.
2. **Procedure and data are portable, the operator surface is not.** Every
   vendor-specific feature (push, goal loop, agent view, remote start) needs a
   neutral fallback: heartbeat/status in the run record, questions and notices
   via Slack/Telegram output.
3. **The MC backend knows no harness details**, only job, run record, box
   status.
4. **At most one new third-party component per quarter**, only if it
   **replaces** something; versions pinned.
5. **Crutches carry an expiry date** (a metric, reviewed every three months).
6. **Quarterly swap test** with a second coding CLI, covering the watchdog and
   the question path too, not only "job done".
7. **Watch list, 15 min per month:** local variant of the vendor coordinator,
   CLI changelog, engine-side protection against model eviction, plan/limit
   rules, board products.
8. **Open formats:** Postgres, Markdown, Git. Run records stay in the vault,
   not with the vendor. No confidential employer data through vendor-hosted
   remote sessions without clarification.

### 7. The operator's five decisions (2026-09-22)

| # | Question | Decision |
|---|---|---|
| 1 | Direction | Accepted: head per job, MC small, configure before building, build stop, quiet mode. "Team" now means *procedure + short-lived heads*, not named agents. Local GPU hosts move from main engine to helper and fallback. |
| 2 | What unattended heads may never do | Forbidden: secrets/`.env`, SSH keys, deploy, push to `main`, deletes outside the worktree, outbound mail/posts. No permission bypass on the host. First night run with a harmless job only. |
| 3 | Usage limit for automatic heads | Provisionally **30 % of the current weekly limit**; final value after measuring usage share per job. |
| 4 | Channel inbound | **Slack/Telegram become output only** (digest, notices, questions). Jobs start from the CLI vendor's phone app/remote start or the terminal. The lead agent keeps answering inbound until the channels are re-routed (a later stage, after stop 1). A small "message → background session" starter only if missed during the trial. |
| 5 | Own control-room UI | **Wait** for the vendor coordinator's local variant until stop 2. Until then: run-record folder in the vault plus the CLI's session list. Separately, the existing UI gets a page-by-page overhaul. |

### 8. Stop points

- **Stop 1 — after two weeks.** Does the new path work without the operator at
  the desk, also outside the MC repo, with a local helper? Is the helper role
  of the GPU hosts acceptable? Is output-only on channels acceptable?
  **No → exit "harden the fleet":** take the fleet out of quiet mode and resume
  the hardening plan. The decisions above are reversed by a new ADR.
- **Stop 2 — around week 5.** Night runs acceptable? Local vendor coordinator
  available? → decision 5 final: adopt it, or build one lean page.
- **Stop 3 — voice agent**, decided with the channel re-routing (a later stage, after stop 1).

## Alternatives

The three options of 2026-09-21:

- **A: Pilot path (one session per job)** → **taken**, and extended by this
  ADR from "pilot" to "normal path". The trial checks what the pilot did not
  prove: without the operator, outside MC, with a local helper, at night.
- **B: Harden the fleet** (continue the hardening plan on the persistent
  agents) → **not the main path**, kept as the documented exit at stop 1/2.
  Rejected as default because the repair load is structural (delivery into
  running turns, screen-based turn detection, flaky local runtimes), and the
  recent quiet period does not show the fleet holds under load.
- **C: Framework only** (metrics, actor tracking, run records; no direction
  change) → **kept as the measuring layer** (daily digest, actor on status
  changes, run record), without a dollar cap; usage share and operator minutes
  added.

Also considered and rejected:

- **Build an own control room / job page now** → the vendor is shipping a
  coordinator; waiting costs nothing because run records are plain files.
- **A run MCP server for heads** → a CLI script is enough; nothing that a
  head uses needs an admin token.
- **A "head" run type on the task table** → would grow the task core that
  this ADR freezes; jobs and run records stay Markdown until a gap is measured.

## Consequences

### Positive

- The fleet-alive layer (delivery, healers, back-channel, terminal remote
  control) stops being on the critical path for new work.
- One job = one worktree = one PR: isolation and review come from Git, not
  from MC state.
- Most new capability comes from configuration, not code; the planned new
  code is small (switch lock, usage recorder, a price pattern, a few scripts).
- Procedure and data (skills, `AGENTS.md`, Markdown run records) stay
  portable to another coding CLI.

### Negative

- **Vendor dependency:** head, remote start, push questions and goal loop are
  one vendor's features; plan/limit rules change often. Mitigation: house
  rule 2, quarterly swap test.
- **Usage is the scarce resource.** Measure first; decision 3 is provisional.
- **Auth trap:** vendor remote start needs a full interactive login; a
  long-lived token is rejected. Remote start only interactively by day;
  unattended runs without it.
- **Unattended runs can stall:** the CLI's auto mode falls back to asking after
  repeated blocks. Short night jobs; a morning notice "blocked" instead of
  silent standstill.
- **Local-model outages are inherited** by the local helper step; the head must
  fall back to a cloud subagent and record it.
- **Two paths coexist** until the frozen code is removed; frozen areas still
  get safety and operations fixes.

### Open source / existing installations

This repository is public. **Existing installations keep working:** nothing is
removed by this ADR, quiet mode is a per-agent setting, and the fleet features
stay available and supported with safety and operations fixes. Any later
removal happens in separate, themed PRs with their own notes, after two weeks
of measured non-use. Whether removed features become optional modules is
decided then.

### Touched ADRs

| ADR | Effect |
|---|---|
| ADR-005 Lead-first dispatch | **Partially superseded:** new work no longer flows through the lead agent to fixed-role workers. Routing code stays for existing cards. |
| ADR-019 Claude fleet (hybrid) | **Partially superseded:** the persistent Docker fleet is paused, not the default worker pool. |
| ADR-021 Agent personas | **Partially superseded:** fixed per-agent roles are no longer the working model; personas stay for paused agents. |
| ADR-061 §2 Telegram inbound | **Partially superseded:** channels become output-only (decision 4); inbound stays until the channels are re-routed after stop 1. |
| ADR-072 ChatAdapter (inbound routing) | **Partially superseded** for inbound routing (decision 4); the outbound contract stays. |
| ADR-008, 020, 026, 046, 071 | Frozen (dispatch, `mc` back-channel, healers/watchdogs): safety and operations fixes only. Not superseded. |
| ADR-051 Loops | Frozen; unattended runs move to scheduled background sessions. |
| ADR-073 Sessions chat, ADR-075 Group chat | Frozen. |
| ADR-077, ADR-078 Box manager | Kept, frozen; extended only by the switch lock. |
| ADR-034 Vault, ADR-043 OSS release contract | Kept; run records go to the vault; public-repo rules apply unchanged. |

## Nachtrag (2026-10-04) — §4: head transcripts in Chats (read-only) and retention

Operator decision 2026-10-04 (concept "Heads sichtbar", variant A): a head's
run folder already holds a full conversation transcript and a run record
(§1 above), but nothing in Mission Control shows either — a head that is
working, waiting on a question, or long finished looks the same as one that
never existed. This addendum narrows the build stop on §4 by exactly one
thing and leaves the rest of it in place.

(a) **Scope of the exception.** Head runs get a **read-only** chat view:
    Chats lists them next to the fixed agents, and opening one renders its
    existing transcript file (Claude Code / omp / hermes) the same way an
    agent's chat renders. This view only *reads files already on disk* in
    the run folder (transcript, run record, `step.txt`). It adds **no**
    delivery into a head's turn, **no** tmux pane probe or mirror, **no**
    SSE tailer and **no** live input channel — a head takes no message from
    this view, ever. Everything else the build stop covers (terminal remote
    control, the `mc` back-channel, group chat, dispatch/delivery into a
    running turn) stays frozen exactly as before.
(b) **Harness knowledge stays where it was.** The reader lives in the
    existing adapter register (`backend/app/services/transcript_adapters.py`)
    and in `scripts/head/mc-head`; the rest of the backend still carries no
    harness-specific branches (house rule 3, §6 above still applies to
    everything but this one reading path).
(c) **The rest of the build stop is unchanged.** Dispatch, healers,
    watchdogs and the frozen fleet-chat delivery code get safety and
    operations fixes only, same as before this addendum.
(d) **Effect on ROADMAP E5.** Before this addendum, E5 listed "Sessions
    chat" whole as a clean-up candidate (its build stop, §4, made the whole
    page a frozen-and-unused surface). That is corrected: the **read-only**
    chat view (the Chats list and the transcript view it opens) is not a
    removal candidate — it is now the home for heads. What stays a clean-up
    candidate is the input/delivery half of that same page for **paused
    fixed agents** (the composer, the tmux mirror, the terminal remote
    control) — nothing in this addendum revives that. Only two agents keep
    a fixed, persistent chat going forward: the lead agent and one local
    omp agent; every other agent on that list is paused fleet, unchanged by
    this addendum.
(e) **Retention.** A head's transcript, run record, `job.md`, `spec.json`
    and logs are kept forever — nothing in this addendum deletes them. Its
    working copy (`wt/`, the git worktree) and its disposable caches
    (`home/.cache`, …) are cleaned up by a separate host rule
    (`mc-head gc`, `scripts/head/mc-head`), which **defaults to a dry run**
    and reports what it would remove; it only deletes when the operator
    turns on `MC_HEAD_GC_APPLY=1` in the host's launchd plist, never from a
    switch the backend can write. A worktree that is otherwise clean and
    pushed but still carries a git-ignored file is **kept**, not removed —
    `git worktree remove` on its own deletes ignored paths too, so `mc-head
    gc` checks for them explicitly and reports `ignored_files` instead of
    deleting (review finding on PR #751) — except for a short, literal
    allowlist of regenerable build/test caches (`__pycache__/`, `*.pyc`,
    `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `node_modules/`),
    which never block removal on their own: the procedure's own red/green
    test step leaves at least one of these behind in every worktree, so
    without the allowlist "worktrees cleaned" never actually fires on a
    real run (round 3 review finding). The dry run normalises each
    scratch clone's git config and fetches one disposable check ref against
    it (idempotent host-side plumbing, review finding on PR #751) but never
    touches a run folder's own data either way. Native CLI session
    resumption (`--resume`) is explicitly **not** part of this addendum — it
    is unproven after a worktree move and stays a later decision.

## Note (2026-10-07) — amended by ADR-089

The open questions this ADR left about the lead agent are decided in
[ADR-089 — The lead agent is the operator's single right hand, with hands](089-lead-agent-with-hands.md).
In short: §4 gains one exception (the lead agent's chat input and driver);
§5 keeps exactly two fixed agents (the lead agent and one local omp agent,
as Nachtrag (d) already says); the voice agent becomes the lead agent's
voice, which answers stop 3 (§8). §7 decisions 2 and 5 and the output-only
channels of decision 4 are unchanged. The text above is not rewritten;
ADR-089 §9 lists each change.

## References

- Quiet mode: `backend/app/models/agent.py:188` (`operational_mode`),
  `backend/app/services/operations.py:147`,
  `backend/app/services/task_runner.py:1732`
- Measuring layer from option C: #640 (actor), #641 (daily digest),
  #645/#646 (run record), #643 (notice-only escalations)
- Switch-lock insertion points: `runtime_manager.stop_runtime`,
  `runtime_manager.restart_runtime`, `recipe_switcher.start_recipe_on_host`
- Related ADRs: see "Touched ADRs"
