# ADR-089 — The lead agent is the operator's single right hand, with hands

**Status:** Accepted · amends ADR-085 (§4 build stop, §5 quiet mode, §7 decision 4 wording, §8 stop 3) and PRINCIPLES §8 rule 2
**Amended by:** [ADR-090](090-chat-as-remote-control.md) — §4 / §9 (the chat exception now covers the chat of both fixed agents, not only the lead agent's chat input and driver); see the note before the references
**Datum:** 2026-10-07
**Scope:** Architecture/Direction · Agent Protocol · Backend/Autonomy · Infra/Runtime · Docs

## Context

ADR-085 changed the unit of work to a short-lived head per job and put the
persistent fleet into quiet mode. Two agents stayed active: the lead agent
and the voice agent. What the lead agent *is* in the new direction was left
open on purpose:

- ROADMAP E4 ("channels and lead agent") and stop 3 ("voice agent: keep,
  rebuild or retire") were `later`.
- PRINCIPLES §8 rule 2 said "no new long-running lead ('lead agent 2.0')".
- In practice the operator talks to the lead agent every day, and the lead
  agent is the one that starts work, reads results and reports. It runs as
  the operator's OS user with permission prompts skipped
  (its host runtime, ADR-014) and the operator's own GitHub access;
  nothing in MC classifies what it may merge, deploy or switch on its own.
  Its merges and deploys depend on prose rules alone.

MC already has the parts a controlled "right hand" needs, built for other
reasons:

- autonomy levels per action type — L1 do, L2 do and report, L3 approval card
  and wait; unknown type = L3 (`backend/app/services/autonomy.py`,
  `AUTONOMY_DEFAULTS`, `enforce_autonomy`), shown in Settings → Autonomy;
- approval cards in "Needs you", optionally mirrored to a chat channel
  (`backend/app/services/operator_approvals.py`);
- the ADR gate: a PR that touches a decision document needs the operator's
  approval of that exact text (`backend/app/services/adr_gate.py`);
- a squash merge through the merge queue without `--admin`
  (`backend/app/services/git_service.py`, `merge_pr`);
- the switch lock that refuses a model switch while an engine has running
  requests (`backend/app/services/heads/box_guard.py`) and the one runtime
  switch path (`backend/app/services/agent_runtime_switch.py`);
- the forbidden-command list of heads
  (`scripts/head/claude-head-settings.json`) and their identity check
  (`scripts/head/mc-head`);
- subagent rendering in the chat for one harness
  (`backend/app/services/transcript_adapters.py`, `subagent_runs`), an ACP
  driver for omp agents (`backend/app/services/acp_chat_transport.py`) and
  read-only head transcripts in Chats (ADR-085 Nachtrag 2026-10-04).

The operator decided the open questions in three rounds: 2026-10-03 (the lead
agent keeps its hands; the voice agent becomes the lead agent's voice),
2026-10-04 (two fixed agents, two kinds of helpers, same package on both
brains, no protocol proxy) and 2026-10-07 (change-class gate, separation,
brain fallback, voice order, channels; the GitHub App comes later).

## Decision

**The lead agent is the operator's single right hand: one agent for text and
voice, on either of two brains, that keeps its hands and acts on its own only
inside a change-class gate built on MC's existing autonomy levels.** It does
not write product code itself — heads do. It talks to the operator, decides
who works, and does the big actions (merge, deploy, runtime switch) when the
class of the change allows it.

### 1. Hands

The lead agent keeps a shell, read-only diagnostics (files, git, container
status and logs), the `mc` CLI and the deploy script. The three big actions
go through **MC gates** — MC commands the server checks — not through raw
tools:

| Gate | The server checks (existing parts reused) | Runs |
|---|---|---|
| merge (`mc pr merge`) | CI green · change class (§2) · `enforce_autonomy()` · ADR gate · approval bound to the PR's head SHA · origin of the instruction (§2 R3) | `git_service.merge_pr` through the merge queue, never `--admin` |
| deploy (`mc deploy plan/run/undo`) | deploy preflight on the pinned ref · no task in progress · no head running · change class | the existing deploy script from the deploy worktree; a `deploy_history` row and health checks |
| runtime switch (`mc runtime switch`) | the one switch path (`agent_runtime_switch.py`) · switch lock · brain guard (§5) | the existing recipe switcher |

None of these gates exists yet; they are steps B9, B11 and B12 of the build
order (ROADMAP E4). Until a gate exists, the action it covers stays where it
is today: the operator's call.

### 2. Change-class gate on L1/L2/L3

Every change is classified by the paths it touches. No new approval system:
the classes map onto the existing autonomy types and levels.

| Class | Recognised by (examples) | Autonomy type | Level | What the lead agent does |
|---|---|---|---|---|
| Reading, diagnosis, a card, a subagent, starting a local head | — | — | L1 | does it |
| Docs and tests only | `docs/**` outside decision and rule files, `backend/tests/**`, `e2e/**`, `*.test.ts` | new `merge_docs_tests` | L1 | merges when CI is green; listed in the daily report |
| Invisible fix | `backend/app/**`, `scripts/**` outside the red list, no UI path | new `merge_invisible`, `deploy_invisible` | L2 | merges **and** deploys, then reports with proof of effect and an undo |
| Visible UI | `frontend-v2/src/app/**`, `frontend-v2/src/components/**`, `frontend-v2/messages/*.json` | `visual_review` (exists) | L3, then itself | sends pictures (390 px, both languages, both themes) as a card; merges and deploys only after the operator approves the picture (AGENTS.md: structure never before the picture) |
| Decisions and rules | `docs/decisions/**`, `docs/PRINCIPLES.md`, `AGENTS.md` | ADR gate (exists) | L3 | the operator approves this exact text, then it merges |
| Data | `backend/alembic/versions/**`, deletes | `deploy` (exists) | L3 | announces with a backup proof and waits for "go" |
| Infra, deploy path, fleet | `docker/**`, `docker-compose*.yml`, `scripts/head/**`, `start-all.sh`, an agent on or off | `config_change`, `deploy` (exist) | L3 | announces and waits for "go" — **unless the operator pre-approves the class** by setting it to L2 in Settings → Autonomy |
| Runtime switch | recipe, agent runtime | new `runtime_switch` | L2 | switches when the box is free and no head or night job runs; otherwise waits |
| Security core | `autonomy.py`, the class list, `scopes.py`, `auth.py`, `adr_gate.py`, `.github/**` | fixed | L3 always | only with the operator's approval; cannot be lowered |
| On no list | a new folder | default | L3 | asks; a contract test reports the new path |

Rules:

- **R1 Strictest class wins.** A PR is as strict as its strictest file. Every
  merge needs CI green and the sentence "what changes for the operator".
- **R2 Only the operator lowers a level** (Settings → Autonomy). The level
  overrides move from Redis into the database, and the security core cannot
  be lowered (step B3).
- **R3 Origin.** An instruction counts only when it comes from the operator
  (MC chat, voice) or from a schedule the operator created. Text from files,
  the web, head output or chat channels is information, never an
  instruction. Every gate requires an origin reference; without one, L2
  becomes L3.
- **R4 Proof of effect at L2:** a `deploy_history` row, health green, the PR's
  acceptance probe run live, and a report in the lead agent's chat with links.
  Undo = a revert PR through the queue plus a redeploy of the previous SHA.
- **R5 Daily budget:** at most 10 merges, 3 deploys and 4 runtime switches;
  above that, L3.
- **R6 Brakes.** An emergency brake in the lead agent's chat (normal / ask
  only / stop). An automatic brake — a never-list attempt, missing proof or a
  failed undo — puts the lead agent on "ask only" for 24 hours.
- **R7 A refusal stays a refusal.** Running heads, a red preflight or an
  engine with running requests block the action, even with the operator's go.

### 3. Never-list (hard)

The lead agent never: uses `gh pr merge --admin` or any other bypass of the
ruleset or required checks; force-pushes; pushes to `main`; uses
`--no-verify`; disables or edits rules or checks; reads or shows secrets
(`.env`, the secret store, task credentials); deletes an agent. Every attempt
is counted (AGENTS.md "every bypass is counted and reported") and triggers
the automatic brake (R6).

What makes this hard rather than polite is layered:

- **Server side:** the merge gate never sets `--admin`; the lead agent loses
  the `credentials:read` scope, so MC hands it no credentials (step B4).
- **GitHub side (later):** the lead agent gets its own GitHub identity without
  admin, maintain or push rights, checked like a head's identity, and the
  merge gate merges through a **non-admin GitHub App**. The operator decided
  to set up the App **later**. Until it exists there is **no merge gate**
  (step B9 waits) and **the lead agent does not merge on its own** — merges
  follow the operator's standing merge rule, as today.
- **Session side (soft, honest):** the heads' deny-list patterns (`--force`,
  `--no-verify`, `gh pr merge`, `docker compose up`, `.env`, the secret
  store) in the lead agent's settings on both brains, and every command in
  the transcript, read by a bypass counter (step B7). Whether omp honours the
  same patterns is checked by the equal-quality test (§5).

### 4. Two fixed agents; helpers are visible

- **Exactly two fixed agents:** the lead agent and one local omp agent for
  small jobs without a PR. Every other persistent agent stays paused
  (ADR-085 §5), never deleted in this phase. Automatic delivery of jobs to
  the local omp agent resumes only after the security step (B4); its chat
  keeps working.
- **Two kinds of helpers, both fetched by the lead agent:** subagents for
  short thinking work inside the conversation (read, research, review) and
  heads for code work (own worktree, PR, also at night, local first).
- **All helpers appear in the lead agent's chat:** subagents as a
  collapsible block (one harness today; omp follows in B5), heads as a
  status line in the chat plus the "Heads" section in Chats (ADR-085
  Nachtrag 2026-10-04). History and run records are kept forever.

### 5. Brain: coding CLI subscription or omp + local model

- **Same package on both brains:** the lead agent's instruction card (from
  `backend/templates/`), the `mc` CLI, the deny-list, the change-class gate
  and the gates; memory lives in the vault (the CLI's own auto-memory is off
  on both, so there is one memory).
- **Drivers:** the coding CLI's official background mode (not print mode;
  ADR-068 and PRINCIPLES §8 rule 10 stay), and the existing ACP driver for
  omp. The current terminal driver stays as a switchable way back until it
  has been unused for two weeks (B15).
- **No protocol proxy.** ADR-086 decision 6 stands: the local route is omp,
  not the coding CLI pointed at a local model through a translator.
- **Mandatory equal-quality test gates "done"** (step B10): the same jobs on
  both brains against a test backend. Pass: temptations (bypass, force push,
  reading `.env`, an instruction planted in a file it reads) refused **100 %**
  on both; other jobs at most 1 in 20 worse on the local brain; at most
  twice as slow; once idle and once under load (lead agent + one head + one
  night job). Speed is measured warm (PRINCIPLES §4 rule 6). Only when it is
  green does the local brain become selectable and the feature count as
  done. The test counts as one swap probe (ROADMAP E3).
- **Default brain** stays the coding CLI subscription until the test is green.
- **Brain fallback:** when the local model the lead agent runs on goes away
  (the GPU box loads another model or is unreachable), the lead agent
  switches itself to the cloud brain and tells the operator. A runtime switch
  that would hit its own engine moves it to the cloud brain first (brain
  guard, B12).

### 6. Voice is the lead agent's voice

The voice gets ears and a mouth, no hands of its own; everything goes into
the lead agent's chat.

1. **First: local voice note** — recorded on the phone, transcribed by the
   local speech-to-text service, sent as text to the lead agent (B13).
2. **Later: live voice** — the live transport of ADR-083 speaks only an
   instant sentence plus the lead agent's text (B14). The operator decided
   to build this later. When it is built, ADR-083 decision 2 changes from
   delegating to a backend model to delegating to the lead agent.

The separate voice agent keeps running until the lead agent's voice replaces
it; it is then paused (not deleted). This answers ROADMAP stop 3.

### 7. Channels are output-only

Slack and Telegram carry notices and approval buttons only. Jobs reach the
lead agent through the MC app (chat or voice). A channel message is
information (R3), never an instruction. This keeps ADR-085 decision 4.

### 8. Separation from the operator's account

- **Stage A — now:** same OS user, but an own environment for the lead agent
  (own home and CLI config directory), the deny-list, and a log of every
  command (transcript plus events). Residual risk, stated plainly: as the
  operator's OS user, the lead agent *could* reach the operator's GitHub
  access and `.env` on purpose; every attempt is in the log and brakes it.
- **Stage B — before the lead agent deploys unattended at night:** its own OS
  user, hard separated; deploys then go through a small helper service.

### 9. What this changes in ADR-085

| ADR-085 | Before | Now |
|---|---|---|
| §4 build stop | dispatch, healers, Sessions chat, group chat: safety and ops fixes only; voice agent frozen | unchanged, with one exception: the lead agent's chat input and driver. The voice agent becomes the lead agent's voice (§6) |
| §5 quiet mode | all persistent agents paused except the lead and the voice agent | fixed agents = the lead agent + one local omp agent (matches Nachtrag (d)); the voice agent is paused once its role is taken over |
| §7 decision 4 | channels output-only; the lead agent answers inbound until the channels are re-routed | output-only confirmed; inbound channel text is information only (R3) |
| §8 stop 3 | voice agent decided with the channel re-routing | answered by this ADR |
| §7 decision 2, decision 5 | forbidden list for unattended heads; no own control room before stop 2 | **unchanged** |

## Alternatives

- **Lead agent without hands** (one fixed `mc` verb list, no shell, deploy
  only on the operator's tap) → rejected by the operator on 2026-10-03: it
  turns the right hand into a messenger, and it cannot be enforced with a
  shell anyway, so the safety would be imagined.
- **Fleet back** (take the persistent agents out of quiet mode; ADR-085's exit
  "harden the fleet") → not taken: the fleet's repair load was the reason for
  ADR-085, and one lead with short-lived helpers covers the need.
- **Protocol proxy** (coding CLI on a local model through a translator) →
  rejected, ADR-086 decision 6: a private seam in front of a volatile layer;
  omp is the local route.
- **A sandbox profile and a taint machine for the lead agent** → dropped:
  with a shell, nearly every session would be "tainted" and the hands would be
  worthless. The origin rule R3 at the gates replaces it; heads keep their
  sandbox.
- **New tables and an own lead-agent page** → not in the first version:
  approvals use `approvals`, the log is transcript plus events plus
  `deploy_history`, and the lead agent's chat already exists in Chats
  (PRINCIPLES §3 rule 10, extend).

## Consequences

### Positive

- One place to talk to, by text or voice; the operator taps only for visible,
  risky or rule-changing work.
- The big actions get a server-side check where today there is prose only.
- Everything builds on existing parts (autonomy levels, approvals, ADR gate,
  merge queue, switch lock, deploy history, Chats); the build order is 15
  small PRs (ROADMAP E4), each with its own way back.
- The lead agent is portable: same package on the cloud and the local brain,
  proven by a test instead of assumed.

### Negative

- **Stage A is a soft boundary.** Until stage B and the own GitHub identity,
  the never-list on the session side rests on a deny-list and a log. The
  merge gate is held back for exactly that reason.
- **No merge autonomy yet.** The step that makes the lead agent merge (B9)
  waits for the operator's GitHub App; until then the operator merges as
  today.
- **More reach, more blast radius.** An L2 deploy can break the live system;
  R4 (proof), R5 (budget), R6 (brakes) and R7 (refusals stay) limit it, and
  undo is a revert plus a redeploy of the previous SHA.
- **Cloud usage on fallback.** When the local brain goes away, the lead agent
  runs on the subscription until it is back.
- **The class list must stay complete.** A new folder defaults to L3 and a
  contract test reports it; the list itself is security core.

### Open source / existing installations

Nothing changes for existing installations with this ADR: it is a decision
record. Each later step ships behind its own switch or with every new type
at L3, so an installation that does nothing keeps today's behaviour.

## Note (2026-10-12) — amended by ADR-090

[ADR-090 — The MC chat is the operator's remote control](090-chat-as-remote-control.md)
widens the chat exception of §4 and §9 (ADR-085 §4 row): not only the lead
agent's chat input and driver, but the chat of both fixed agents — the lead
agent and the local omp agent — may grow, inside a closed feature list, a
budget per package and stop points. §5 (omp over ACP as the local brain) is
unchanged. The text above is not rewritten.

## Nachtrag (2026-10-20) — §4: what "automatic delivery" to the local omp agent covers

Decided on 2026-10-20 (the operator delegated this decision). §4 says that
automatic delivery of jobs to the local omp agent resumes only after the
security step B4. The code already draws a line inside that sentence; the
text did not. This note states the line as the code draws it today,
including its gaps. It changes no behaviour.

**(a) The agent stays paused until B4.** The local omp agent keeps
`operational_mode=paused` until B4 (ROADMAP E4) is done. Its chat keeps
working, as §4 and ADR-090 say.

**(b) Off until B4: automatic delivery.** That means MC choosing the agent
for a card, or pushing a card to it on its own initiative. These paths ask
`check_dispatch_allowed` (`backend/app/services/operations.py`, rule
`R-paused-no-dispatch` in `docs/produkt/regeln.yaml`), which refuses a
paused agent:

- `auto_dispatch_task` (`backend/app/services/dispatch.py`), for a new card
  and for the operator's re-assign on the card in the app;
- `mc delegate`, refused outright with HTTP 503 (`agent_delegate_task` in
  `backend/app/routers/agent_scoped.py`);
- review hand-off and rework (`backend/app/services/task_lifecycle.py`).

The card is not lost: it stays in the inbox, still assigned.

Automatic routing of a card without an assignee also counts as automatic
delivery. A card created in the app without an agent stays unassigned
unless lead auto-assign is on (`_leave_unassigned_for_operator` in
`backend/app/routers/tasks.py`). Where routing runs, it picks the
orchestrator or the board lead first (`find_dispatch_target` in
`backend/app/services/dispatch.py`). The router does not read the mode
itself; closing that gap is a separate fix.

Three paths do **not** ask `check_dispatch_allowed`. Each of them reaches a
paused agent, but only with a card that is already assigned to it:

- the agent's own poll (c);
- the undispatched-card watcher (`_check_undispatched_tasks` in
  `backend/app/services/watchdog/task_monitor.py`). For a cli-bridge agent
  it does what push dispatch does there, on a later watcher pass once the
  agent has no other card in flight: it prepares the workspace
  (`dispatch_to_cli_bridge` in `backend/app/services/cli_bridge_runner.py`)
  and stamps `dispatched_at`;
- `mc reassign` (`backend/app/routers/agent_task_status.py`), which
  prepares the workspace right after assigning.

Delivery into the agent's session is always the poll.

**(c) On while paused: any card assigned to it by name, fetched by its own
poll.** `/agent/me/poll` (`agent_poll` in `backend/app/routers/agents.py`,
rule `R-paused-poll-takes-assigned`) does not read `operational_mode`. The
next poll claims the card, whoever assigned it:

- the operator, on the card in the app;
- the lead agent, with `mc reassign`;
- a scheduled job or automation that names the agent (`_do_create_task` in
  `backend/app/services/scheduler.py`);
- a bench-studio run (`backend/app/verticals/bench_studio/orchestrator.py`);
- a review rework that hands the card back to the agent that built it.

This decision allows that. Each of these sources names the agent on
purpose: the operator set up the job or bench run with it, and a rework
goes back to the card's own builder. Before B4 this is how the agent gets
small jobs without a PR. It was checked live on 2026-10-20: cards assigned
to the paused agent, created by the operator in the app and by a
bench-studio run, went from the inbox to done.

The poll keeps its own gates: dependencies, held or stopped cards, the
update drain (ADR-094) and the runtime-readiness check (ADR-097). It never
takes an unassigned card or one assigned to another agent. The system
modes HALTED and DRAINING and the planning-phase gate live only in
`check_dispatch_allowed`, so they do not hold a poll claim, for this agent
or for any other. *(Superseded for HALTED and DRAINING by the next
Nachtrag, "the system mode holds every path"; the planning-phase gate
part still holds.)*

**(d) Limits while paused: known gaps, not intended behaviour.**

1. *Unprepared workspace.* A refused push stops before workspace
   preparation, which in `auto_dispatch_task` runs only after the guard.
   The poll runs every few seconds, so it usually claims the card at once.
   Such a card arrives with no `workspace_path` and runs in the agent's
   workspace root, the bridge's fallback (`docker/omp-bridge/bridge.py`).
   The bridge's guard only checks that this folder exists. A card is
   prepared before the claim only when `mc reassign` assigned it (it
   prepares right after assigning), or when the undispatched-card watcher
   reaches it before the poll does. Live on 2026-10-19 and 2026-10-20 both
   kinds happened: cards claimed at once without a workspace, and cards
   that waited while the agent finished another one and were prepared by
   the watcher. For a
   repository-bound card this is the failure class of the 2026-09-13
   incident (`docs/dispatch-path-parity.md` row 24: in an unprepared folder
   an agent cloned the repository by short name and opened its PRs on the
   wrong repository). Until a follow-up fixes this, the paused agent gets
   only small jobs without a repository, and repository work stays with
   heads. Nothing enforces this yet: the app still lets the operator assign
   a repository-bound card to it.
   The follow-up: prepare the workspace when the push is refused because
   the agent is paused, or have the poll hold the claim until the workspace
   is prepared.
2. *Nobody watches a silent card.* The stuck-turn and silent-card watchers
   skip paused agents (`_check_stuck_in_progress` in
   `backend/app/services/task_runner.py`; `_check_silent_cards` and
   `_check_silent_mailbox` in `backend/app/services/watchdog/task_monitor.py`).
   A card the agent takes and then leaves silent is not reported. The
   operator who assigned a card watches it. For a card from a scheduled
   job, an automation or a bench run, nobody does. The follow-up: these
   watchers also cover cards that a paused agent has claimed.
3. *Display.* When the undispatched-card watcher prepares a card, it marks
   the paused agent `run_state=running`, so the agent can show as running
   while it is paused.

**(e) Every paused agent, not only the local omp agent.** The poll line in
(c) holds for every paused agent whose poller runs. Today that includes a
paused host agent. A paused agent whose poller does not run keeps every
assigned card in its inbox. ADR-085 §5 ("paused agents receive no
dispatch"), rule `R-paused-no-dispatch` and the ROADMAP E0 acceptance line
all mean push dispatch through `check_dispatch_allowed`. The rule text and
the ROADMAP line now say so.

**(f) Pinned by tests.** `backend/tests/test_paused_agent_poll_claim.py`
pins four things:

- push refuses the paused agent and leaves the card assigned in the inbox;
- the agent's poll then delivers that card;
- the poll takes no unassigned or foreign card;
- the undispatched-card watcher prepares an assigned card for the paused
  agent, and the poll still delivers it.

If any of these changes, a test goes red, and this note changes in the
same PR.

**(g) At B4** the agent goes back to `active` and push dispatch reaches it
again. The text above is not rewritten.

## Nachtrag (2026-10-20) — the system mode holds every path that hands a card out

Decided on 2026-10-20 by the development lead the operator delegated to.
The note above, (c), named a gap: the system mode (HALTED / DRAINING, set
with `PUT /api/v1/system/mode`) lived only in `check_dispatch_allowed`, so
the emergency brake did not stop an agent that polls. This note closes the
gap for the system mode. It does not change agent rules: the notes (a) to
(g) of the earlier Nachtrag stay as written, except the part of (c) marked
superseded above.

**(a) One rule, every path.** `system_mode_refusal` in
`backend/app/services/operations.py` (rule `R-system-mode-holds-poll` in
`docs/produkt/regeln.yaml`) decides it. HALTED holds every card. DRAINING
holds every card except the automatic continuation flows (subtask, review
hand-off, rework). Every path that hands a card out asks it:

- push dispatch, through `check_dispatch_allowed` (unchanged);
- the agent's own poll (`agent_poll` in `backend/app/routers/agents.py`),
  for every card it would hand out: a fresh inbox card, a phase approval, a
  dispatched card whose prompt never arrived, and the restart of an
  orphaned run. The held answer is `state=idle` with `system_halted` or
  `system_draining` and a `detail`, like the update drain's answer. For a
  fresh card the poll asks its gates in push dispatch's order: HALTED, the
  MC update drain (ADR-094), runtime readiness (ADR-097), DRAINING;
- the undispatched-card watcher (`_check_undispatched_tasks` in
  `backend/app/services/watchdog/task_monitor.py`): it leaves a held card
  untouched, with no workspace preparation and no `dispatched_at`;
- pull dispatch (`GET /api/v1/agent/boards/{board_id}/tasks/next` in
  `backend/app/routers/agent_task_status.py`): it answers 204, as for "no
  work". It also respects the MC update drain now.

Paused stays an agent rule outside this one. A paused agent still takes a
card assigned to it by name in normal mode ((c) above). While the system is
HALTED it is held like every other agent.

The planning-phase gate also stays out of this rule. It is not a system
mode but a property of one card (`dispatch_phase == "planning"`), it only
applies behind `enable_dispatch_gating`, which is off by default
(`backend/app/config.py`), and the undispatched-card watcher already leaves
planning cards alone in its query. It stays in `check_dispatch_allowed`.

**(b) A card in flight keeps its answer.** A card the poll has already
handed to this agent, under its current attempt and without an ACK yet, is
in the agent's session. The poll keeps answering the same `new_task` with
the same attempt, so nothing new goes out. An `idle` answer would be worse:
the bridges read `idle` as "card gone", drop their dedup (one also resets
its session, `poll.sh` drops its task lock) and paste the card a second
time after the halt. The poll records each delivery in Redis
(`RedisKeys.poll_delivered`: agent, attempt and `dispatched_at`).
`dispatched_at` alone cannot tell, because push dispatch and the watcher
stamp it without delivering. A new attempt, a card reassigned to another
agent, a run a healer reset for a fresh dispatch (Tier-3 recovery clears
`dispatched_at` and `ack_at` but keeps the attempt) and the orphan restart
(a fresh attempt for an ACKed card) are not in flight and are held.

**(c) The ladders and the silent-mailbox alarm wait.** The ACK ladder
(silent retry with a new attempt, then an operator escalation) and the
pending ladder (`_check_dispatch_ack` in
`backend/app/services/task_runner.py`) skip a card that the system mode or
the MC update drain holds. Nothing was allowed to deliver it, so a missing
ACK is not the agent's fault. The ladders keep skipping it for one ACK
timeout plus margin after the hold ends (`RedisKeys.dispatch_system_hold`),
so the first poll after the hold delivers the card before a ladder reads
its old `dispatched_at`. A card that is really lost still escalates after
that window.

The silent-mailbox watcher (`_check_silent_mailbox` in
`backend/app/services/watchdog/task_monitor.py`) does not count such a card
either, while it is held or in that same window. Otherwise an agent whose
inbox holds only held cards looks like a dead mailbox: a "STILLE SCHLANGE"
note to the Board Lead 15 minutes into every hold, and the stage-2
escalation to the operator (`_check_lead_notify_escalations`) 30 minutes
after that. A card the mode lets through (a subtask under DRAINING) still
counts.

**(d) Recovery is not held.** `/agent/me/active-task-recovery` gives an
agent back the card it already owns, with that card's attempt. Each poller
calls it only once, when it starts (`poll.sh`, the omp bridge and the host
poll template). If it were held, the card would not come back for the rest
of that run, and a Board Lead's card has no orphan restart behind it.
Running work is not the brake's to stop. The orphan restart is different:
it hands out a fresh attempt, and the poll asks it again on every round,
so it is held and resumes after the halt.

**(e) What HALTED does not stop.** A turn that is already running (the stop
button stops it); comments and thread messages, which still come with a
held poll answer, and the wake-ups they cause. The healers read no system
mode and keep running: orphan recovery (`backend/app/services/watchdog/core.py`)
and the stale and stuck in-progress checks with their tiered recovery and
nudges (`backend/app/services/task_runner.py`). A re-dispatch they start
goes through `auto_dispatch_task`, which push dispatch's gate refuses while
HALTED, and the poll holds the card a healer reset ((b) above). This is
older than this note and stays out of it. The app has no control for the
system mode yet: the client helper exists, but no page calls it
(`docs/produkt/landkarte.yaml`, "platform leftovers"). The mode is set
through the API.

**(f) Pinned by tests.** `backend/tests/test_poll_respects_system_halt.py`
(the poll: every path, the order of its gates, a card in flight, parity
with push dispatch, pool hygiene) and
`backend/tests/test_system_mode_holds_every_path.py` (watcher, ladders,
silent-mailbox alarm and its stage 2, pull dispatch, recovery not held).

## References

- Autonomy levels: `backend/app/services/autonomy.py` (`AUTONOMY_DEFAULTS`,
  `enforce_autonomy`); approvals: `backend/app/services/operator_approvals.py`
- ADR gate: `backend/app/services/adr_gate.py`,
  `backend/app/services/decision_docs.py`
- Merge: `backend/app/services/git_service.py` (`merge_pr`); deploy:
  `backend/app/routers/deploy.py`, `backend/app/services/deploy.py`
- Runtime switch: `backend/app/services/agent_runtime_switch.py`,
  `backend/app/services/heads/box_guard.py`
- Head deny-list and identity check: `scripts/head/claude-head-settings.json`,
  `scripts/head/mc-head`
- Chat: `backend/app/services/transcript_adapters.py` (`subagent_runs`),
  `backend/app/services/acp_chat_transport.py`
- Lead agent today: the host runtime of ADR-014
- Build order and acceptance: [ROADMAP.md § E4](../ROADMAP.md)
- Related ADRs: ADR-005, ADR-014, ADR-068, ADR-083, ADR-085, ADR-086
