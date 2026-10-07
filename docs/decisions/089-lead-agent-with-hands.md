# ADR-089 — The lead agent is the operator's single right hand, with hands

**Status:** Accepted · amends ADR-085 (§4 build stop, §5 quiet mode, §7 decision 4 wording, §8 stop 3) and PRINCIPLES §8 rule 2
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

**Single source:** the class table lives in code,
`backend/app/services/change_classes.py` (step B2), and its contract test
`backend/tests/test_change_classes.py` fails when a tracked file is on no
list. The table below shows the classes with examples; where the two
differ, the code is the rule — a change to it is itself a security-core
change.

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

## References

- Change classes (single source of §2): `backend/app/services/change_classes.py`,
  contract test `backend/tests/test_change_classes.py`
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
