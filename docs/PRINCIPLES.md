# Mission Control — Principles

The rules this project builds by. Each rule is short on purpose; the reasoning
lives in the linked decision records. When a rule and the code disagree, the
code is the bug — or the rule needs a new ADR, not a quiet exception.

Where the project stands on these rules: [ROADMAP.md](ROADMAP.md).
The decision behind the current direction:
[ADR-102 — MC is the cockpit for your AI](decisions/102-cockpit-direction.md).
Still in force from earlier decisions:
[ADR-086 — Harness and runtime freely switchable, local first](decisions/086-harness-runtime-cross-switch.md)
and [ADR-093 — Everything the user may change is changeable in MC](decisions/093-user-configurable-and-extensible.md).
Rule numbers stay stable because code and ADRs cite them; a rule that ADR-102
overturned is marked in place, not deleted or renumbered.

---

## 1 · Why MC exists now

**North star: MC is the cockpit for your AI — one chat, your models and
devices, your knowledge. On your own hardware, as simple as Claude,
installable by anyone** (ADR-102, replacing the "autonomous team" north star
of ADR-085).

The ten principles (ADR-102 §1). Parts not built yet name the ROADMAP stage
that builds them.

1. **Chat first, as simple as Claude.** MC opens in the last chat. Few
   menus: chat, models, knowledge (stages 1–2).
2. **Chats instead of fixed agents.** No named, long-running agents. Every
   chat picks its model and harness and can switch both at any time
   (stage 1).
3. **Local AI and devices belong to the core.** MC sets up models on the
   operator's devices (GPU boxes, Windows PCs, later others); the speed is
   visible in the chat. Local first, the cloud as fallback (stage 3).
4. **A slim core; everything else is a module** with a fixed interface,
   from a marketplace (stage 4).
5. **A real product, not tinkered software.** Every feature works after a
   fresh install: one-step install (Mac, Windows, NAS), update by button
   with backup and way back, several users, and an automatic test of every
   feature on a fresh install before every release (stage 4).
6. **Knowledge in one place; projects instead of a task page.** Facts
   (notes), skills and project knowledge kept apart. A project is a folder
   with its chats and its knowledge, on a fixed file structure (stage 2).
7. **Private reach.** Home network or Tailscale only, set up from inside MC;
   no access from the open internet (stage 3).
8. **The phone is a full client.** An own iOS app: notices only for what
   matters, dictation with live text, widgets, one-tap approvals. Slack,
   Telegram and Discord go away as channels (stage 5).
9. **Simplify before extending.** What nobody uses goes away, with evidence
   and a way back. Ready-made parts are adopted instead of rebuilt where they
   fit. The chat and the phone app are MC's own; good patterns from other
   products are rebuilt independently, never copied.
10. **Honest numbers.** Insights stays; local usage is priced at API prices
    so the operator sees what it would have cost (stage 2).

## 2 · The direction

Rules 3–5 are cited by code and ADRs and keep their numbers.

1. **Work happens in chats.** A chat is the unit of work, not a head and not
   a fixed agent (ADR-102). The head launcher (one short-lived coding session
   per job in its own worktree, ADR-085 §1) and the night shift stay as
   built until stage 2 decides keep, module or remove.
2. **A slim core.** The core is what the ten principles name — chat, models
   and devices, knowledge and projects, insights — plus API, database and
   auth. Every other page is judged in stage 2: core, module or removed
   (ADR-102).
3. **Configure before building.** Before any new MC code: at most three
   evenings of trying what already exists, with zero MC code. Only a measured
   gap gets built; a ready-made part that fits is adopted (ADR-085 §3,
   ADR-102 principle 9).
4. **Build stop on the fleet layer — superseded by ADR-102.** The chat is
   the product's home and is built further (ROADMAP stages 1 and 3), for
   every harness, not only for two fixed agents. What holds: dispatch,
   watchdog/healers and group chat get safety and operations fixes only until
   stage 2 judges them; session-to-session conversation stays inside the
   closed list of ADR-100 §3.
5. **Reversible first.** Persistent agents are paused, not deleted. Code is
   deleted only after two weeks of measured non-use, in themed PRs, with
   evidence and a way back (ADR-085 §5, ADR-102 principle 9).
6. **Harness × runtime, freely and cross-wise.** Every chat is a pair of
   harness and runtime, chosen per chat and changeable at any time, within
   the pairs `harness_compat.py` allows — the one source of truth for what
   works (ADR-086 decisions 1–2, applied to chats by ADR-102).
7. **Local first.** Local runtimes are pre-selected wherever they are good
   enough; the cloud is the fallback, and it is visible which pair ran
   (ADR-086 decision 3).

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
   it changes or disappears tomorrow? Procedure, data **and** the operator
   surface must stay portable: every vendor-only feature needs a neutral path
   in MC that works for all harnesses (start, status, heartbeat, stop,
   restart with another pair). The procedure core lives in `AGENTS.md` and
   the prompt, not only in a vendor skill (ADR-086 decisions 4–5, amends
   ADR-085 §6 rule 2).
4. **The backend knows no harness details** — only job, run record and box
   status. The one exception is the existing harness layer
   (`harness_compat.py`, `harness_catalog.py`); start commands live there,
   nowhere else (ADR-085 §6 rule 3, ADR-086).
5. **Third-party components — superseded in part by ADR-102.** The limit
   "at most one new third-party component per quarter, only if it replaces
   something" (ADR-085 §6 rule 4) no longer holds: ready-made parts that fit
   are adopted instead of rebuilt, and modules come from a marketplace
   (§1 principles 4 and 9). What holds: versions pinned, and each adopted
   part replaces own code or fills a measured gap.
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
10. **Build for the future, sustainably — extend, don't replace.** Design
    every part so later features attach as additions (new adapter, new
    provider, new view, new field) instead of forcing a rebuild of what
    exists. Before building, ask: will this have to be removed when the next
    expected feature arrives? If yes, choose the seam that lets both live.
    Avoid one-off paths that only fit today's single case. Replacing or
    rewriting a part stays a legitimate option when it is clearly simpler and
    better — decide it explicitly (ADR or PR note), with a migration and
    rollback path, never as an accident. (Operator decision, 2026-10-04.)
11. **Whatever a user may change later is changeable in MC — and designed in
    from the start.** Tokens, providers, harnesses, runtimes, devices and
    other settings can be viewed, tested on save, replaced and removed in the
    UI; container components can be installed from MC, host installs stay in
    the installer with an explicit yes. Providers, harnesses and recipes are
    registry entries with an icon, a test and an install procedure — adding
    one is an entry plus an adapter, not a new page or special case
    (ADR-093, operator decision 2026-10-17).

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
   Outside effect: deploys, messages or posts to people, anything public.
   Taste: what the operator sees — visible design goes to the operator as a
   picture first (ADR-102). Merges to `main` follow the operator's standing
   merge rule; no agent or head merges on its own.
2. **Everything else the agent decides** — the agent in a chat or a head —
   and records the decision and its reason. It asks only when a question
   falls into one of the areas above (ADR-085 §1).
3. **Unattended heads have a forbidden list.** Never: secrets or `.env`, SSH
   keys, deploys, pushes to `main`, deletes outside the worktree, outbound mail
   or posts. No permission bypass on the host (ADR-085 §7, decision 2).
4. **Usage share limits automatic heads.** New automatic heads stop starting
   above the usage share the operator set (provisional: 30 % of the weekly
   limit, ADR-085 decision 3). The chat-channel part is **superseded by
   ADR-102**: Slack, Telegram and Discord go away as channels; notices come
   through the iOS app (ROADMAP stage 5).
5. **Big or ambiguous changes: recommend first, then deliver completely.**
   Small changes are done, not proposed.
6. **Direction changes are ADRs.** Reversing a decision needs a new ADR that
   marks the old one; originals are never rewritten
   ([decisions/README.md](decisions/README.md)).

## 6 · Language rule

1. **Two languages, two audiences.** `operator_language` is what an agent
   writes to the operator (reports, questions, chat replies).
   `work_language` (default `en`) is everything between agents and in
   artifacts: prompts, comments, handoffs, run records, code, commits. Both
   are per-agent fields (`backend/app/models/agent.py`, migration 0201).
2. **Only the lead agent talks in the operator's language — superseded in
   part by ADR-102:** there will be no fixed lead agent; which chats answer
   in the operator's language is decided in stage 1. Until then the per-agent
   fields and defaults below stay as built. Both fields
   default to `en`; the operator sets `operator_language` on the lead agent
   only. Heads and all other agents work in English: shorter prompts, one
   vocabulary, faster runs. Existing installations keep the
   `operator_language` that migration 0201 copied from the old `language`
   field until the operator changes it.
   Both are set in Settings → Language: one list of all agents, plus the
   default a new agent gets (stored in `app_settings`, `en`/`en` until the
   operator changes it; changing it never touches existing agents).
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
10. **Phone sizes.** Targets ≥ 44 px, inputs ≥ 16 px, tables become cards.
    The old limit "monitor and intervene briefly, no full editing on a phone"
    is **superseded by ADR-102**: the phone is a full client (§1 principle 8).
11. **One type scale, one Markdown renderer.**
12. **Pages scheduled for removal get no polish** — only what shared
    building blocks bring along.
13. **What belongs together lives together.** One page per concept with
    consistent icons — all model providers (cloud and local) on one page,
    all harnesses on one, all devices on one; settings link there instead of
    repeating them (ADR-093).

## 8 · What MC deliberately does NOT do

1. **No own control room — superseded by ADR-102.** MC gets its own, simple
   surface: the cockpit, with few menus (chat, models, knowledge). What
   holds: no extra page or menu entry beyond them unless a ROADMAP stage
   decides it; sheets and panels inside the chat come first.
2. **No persistent heads; exactly one lead agent — superseded in part by
   ADR-102.** There are no fixed, named agents in the new direction; chats
   take their place (stage 1). What holds: no persistent heads. The lead
   agent and the local omp agent keep running as built until stage 1 moves
   them into chats.
3. No own harness driver, agent loop or MCP server for heads — a CLI script is
   enough; an admin token is never handed to a head. Exceptions: MCP tools
   that carry only the scope `comms:talk` and read-only facts (ADR-100 §2);
   and the worker flow, in which a registered agent with its own token and an
   explicit scope list works its cards over MCP (ADR-101) — built, but
   deferred by ADR-102: not extended until stage 2 decides it with the task
   page.
4. No new task columns, statuses or "head" run type; the task core is frozen.
   Stage 2 replaces the task page with projects.
5. No model gateway or proxy in front of the coding CLI — so Claude Code on
   a local model stays unavailable until an own ADR decides otherwise; the
   local route is OpenClaude or omp (ADR-086 decision 6).
6. **No box-manager features beyond the switch lock — superseded by
   ADR-102.** Setting up models on the operator's devices is core (§1
   principle 3, stage 3). What holds: the switch lock (refuse a model switch
   while the engine reports running requests) stays on every switch path.
7. No deletes without two weeks of measured non-use, evidence and a way
   back; agents are paused, never deleted, until stage 1 decides what
   becomes of them.
8. No dollar cap as a steering tool — usage share and operator minutes are
   the currency; cost is shown honestly (§1 principle 10).
9. No second toast system, no new design language; third-party parts follow
   §3 rule 5.
10. No headless print-mode or SDK scripting for automatic Claude heads —
    they use the coding CLI's official interactive or background modes. Other
    harnesses use their own official non-interactive mode, which may be a
    print mode (ADR-086).
11. No long-range plan as a decision — the operator decides at the stop
    points in [ROADMAP.md](ROADMAP.md).

---

**Changing this file:** a principle changes only together with the ADR that
motivates it (the current direction: ADR-102). Link the ADR in the rule;
keep each rule to one to three sentences.
