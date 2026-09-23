# Mission Control — Principles

The rules this project builds by. Each rule is short on purpose; the reasoning
lives in the linked decision records. When a rule and the code disagree, the
code is the bug — or the rule needs a new ADR, not a quiet exception.

Where the project stands on these rules: [ROADMAP.md](ROADMAP.md).
The decision behind the current direction:
[ADR-085 — Head per job](decisions/085-head-per-job.md).

---

## 1 · Why MC exists now

**North star: an autonomous team the operator leads, not babysits.** The
operator sets direction, approves what matters and reads results; the system
does the work and keeps itself honest. Every restart, nudge or manual state
flip the operator has to do is a defect, not a routine.

"Team" no longer means a roster of named, long-running agents. It means **a
written procedure plus short-lived sessions that follow it** (ADR-085 §7,
decision 1). MC exists to hold what only this operator has: their rules,
their knowledge, their hardware and their evidence.

## 2 · The direction

1. **A head per job.** Every job gets one short-lived coding-CLI session in
   its own git worktree, branched from `origin/main`. It plans, calls
   helpers, checks its own work, opens a PR, writes a run record and ends
   (ADR-085 §1).
2. **MC becomes small.** MC keeps rules and procedures (as files), the vault,
   the GPU box manager, metrics and the cross-cutting core (API, DB, auth,
   scheduler). Job start, questions and overview come from the coding CLI
   (ADR-085 §2).
3. **Configure before building.** Before any new MC code: at most three
   evenings of trying what already exists, with zero MC code. Only a measured
   gap gets built (ADR-085 §3).
4. **Build stop on the fleet layer.** Dispatch, watchdog/healers, Sessions
   chat and group chat get safety and operations fixes only — no features
   (ADR-085 §4).
5. **Reversible first.** Persistent agents are paused, not deleted. Code is
   deleted only after two weeks of measured non-use, in themed PRs
   (ADR-085 §5).

## 3 · Build rules for a fast-moving AI market

1. **Know which layer you are building in.** Stable, and worth own code:
   the operator's jobs and intent, approval and autonomy rules, knowledge
   (vault), evidence and usage, and management of the operator's own GPU
   boxes. Volatile, never rebuilt: models, inference engines, agent loops and
   harnesses, sandboxes — MC only configures, starts, stops and measures them.
2. **Build on open seams.** `SKILL.md` and `AGENTS.md` for procedures,
   Markdown and Git for knowledge and run records, Postgres for state, an
   OpenAI-compatible API to local engines, CLI calls to harnesses. No private
   protocol where a standard exists.
3. **Swap test.** Before adopting a vendor feature, answer: what happens if
   it changes or disappears tomorrow? Procedure and data must stay portable;
   the operator surface may not be, so every vendor-only feature needs a
   neutral fallback (heartbeat and status in the run record, notices via chat
   output) (ADR-085 §6 rule 2).
4. **The backend knows no harness details** — only job, run record and box
   status (ADR-085 §6 rule 3).
5. **At most one new third-party component per quarter, and only if it
   replaces something.** Versions pinned (ADR-085 §6 rule 4).
6. **Crutches carry an expiry.** Every workaround names the metric that ends
   it and is reviewed every three months (ADR-085 §6 rule 5).
7. **Quarterly swap probe.** Run one real job with a second coding CLI using
   the same files — including the stall watchdog and the question path, not
   only "job done" (ADR-085 §6 rule 6).
8. **Watch list, 15 minutes a month.** Check the items in
   [ROADMAP.md § Watch list](ROADMAP.md#watch-list) before building anything
   they might make unnecessary (ADR-085 §6 rule 7).
9. **Open formats, operator-owned data.** Run records live in the vault, not
   with a vendor. No confidential employer data through vendor-hosted remote
   sessions without clarification (ADR-085 §6 rule 8, ADR-034).

## 4 · Evidence rules

1. **Prove the effect after every change.** "The command ran" and "the test
   is green" are not proof. Show the effect: the agent answers, the file is
   on disk, the row is in the DB, the page behaves differently.
2. **Every test gets a sabotage probe.** Break the code on purpose and watch
   the test turn red; a test that cannot fail proves nothing. Test a check
   against the current state before trusting it — patterns that are too wide
   or too narrow both give false alarms.
3. **Verify before you assert.** Notes, memory and reports from other agents
   are the past, not the current state. Anything a decision rests on is
   checked live (file and line, command, query) first, and labelled
   "verified" or "from notes, dated".
4. **Stop after two identical failures.** The same attempt failing twice
   means the diagnosis is wrong. Stop, re-diagnose, or ask — never a blind
   third try.
5. **Reports from helpers are unverified until checked.** A subagent's or
   background job's "done" counts only after an own counter-check.
6. **Measure when the system is warm.** Performance numbers taken right after
   a runtime start, or while another engine competes for the same box, are
   not comparable; say when and under what load a number was taken.

## 5 · Decision rules

1. **The operator decides direction, money, data and outside effect.**
   Direction: what gets built or stopped. Money: usage limits and paid
   services. Data: deletes, migrations that drop data, anything personal.
   Outside effect: deploys, merges to `main`, messages or posts to people,
   anything public.
2. **Everything else the head decides** — and records the decision and its
   reason in the run record. A head asks only when a question falls into one
   of the four areas above (ADR-085 §1).
3. **Unattended heads have a forbidden list.** Never: secrets or `.env`, SSH
   keys, deploys, pushes to `main`, deletes outside the worktree, outbound mail
   or posts. No permission bypass on the host (ADR-085 §7, decision 2).
4. **Big or ambiguous changes: recommend first, then deliver completely.**
   Small changes are done, not proposed.
5. **Direction changes are ADRs.** Reversing a decision needs a new ADR that
   marks the old one; originals are never rewritten
   ([decisions/README.md](decisions/README.md)).

## 6 · Language rule

1. **Two languages, two audiences.** `operator_language` is what an agent
   writes to the operator (reports, questions, chat replies).
   `work_language` (default `en`) is everything between agents and in
   artifacts: prompts, comments, handoffs, run records, code, commits. Both
   are per-agent fields (`backend/app/models/agent.py`, migration 0201).
2. **Only the lead agent talks in the operator's language.** Both fields
   default to `en`; the operator sets `operator_language` on the lead agent
   only. Heads and all other agents work in English: shorter prompts, one
   vocabulary, faster runs.
3. **UI text only through i18n.** Every label has a key in
   `frontend-v2/messages/<locale>.json`; the backend sends codes, not
   sentences. More languages are added as catalogs, not as code
   ([i18n.md](i18n.md)).
4. **Documents are content, not UI.** Vault notes and run records keep the
   language they were written in; the UI labels around them follow the
   locale.

## 7 · UI principles

The full set with audit evidence lives in the internal UI concept; these are
the rules every UI change is checked against. Visual rules:
[DESIGN.md](../DESIGN.md).

1. **"Needs you" comes first.** Every page answers in five seconds what is
   waiting for the operator, from one source: open approvals plus tasks in
   `blocked` or `user_test`.
2. **One state, one word.** Status is always dot plus word, never colour
   alone. Agent states: Active · Working · Paused · Offline. Task states: the
   lanes defined in `LANE`.
3. **Honest, not reassuring — and every number carries its age.** No
   "online" from sample data, no "$" without "list price". Unknown says
   "not tracked" or "unknown — why"; cached values say when they were checked.
4. **Outcome before process.** Finished work shows result, PR and evidence
   first; the history comes below.
5. **Consequences before the click; undo only where undo is real.** Anything
   that nudges an agent, cancels work or approves gets a confirmation that
   states the consequence. Undo toasts only for pure bookkeeping (status,
   assignee, project, snooze).
6. **Esc closes exactly one layer.** Focus moves into a layer on open and
   back on close; clicking outside never discards typed input.
7. **State lives in the URL.** Tab, filter and open detail are query
   parameters; a dead link says "not found".
8. **The page scrolls, the header stays.** No scroll inside scroll.
9. **Colour only from `LANE`/`STATUS`.** Tags, agents and types are grey;
   exceptions exist only where DESIGN.md documents them.
10. **Phone means monitor and intervene briefly.** Targets ≥ 44 px, inputs
    ≥ 16 px, tables become cards; no full editing on a phone.
11. **One type scale, one Markdown renderer.**
12. **Pages scheduled for removal get no polish** — only what shared
    building blocks bring along.

## 8 · What MC deliberately does NOT do

1. No own control-room UI, job page or coordinator before stop 2 — the
   coding-CLI vendor is shipping one (ADR-085 §7, decision 5).
2. No persistent "heads" and no new long-running lead ("lead agent 2.0").
3. No own harness driver, agent loop or MCP server for heads — a CLI script is
   enough; an admin token is never handed to a head.
4. No new task columns, statuses or "head" run type; the task core is frozen.
5. No model gateway or proxy in front of the coding CLI.
6. No box-manager features beyond the switch lock (refuse a model switch
   while the engine reports running requests).
7. No deletes without two weeks of measured non-use; agents are paused,
   never deleted, in this phase.
8. No dollar cap as a steering tool — usage share and operator minutes are
   the currency.
9. No zoo of third-party components; no second toast system, no new design
   language.
10. No long-range plan as a decision — the operator decides at the stop
    points in [ROADMAP.md](ROADMAP.md).

---

**Changing this file:** a principle changes only together with the ADR that
motivates it. Link the ADR in the rule; keep each rule to one to three
sentences.
