# Mission Control — Roadmap

Where the project is on the direction set in
[ADR-085 — Head per job](decisions/085-head-per-job.md). The rules behind it:
[PRINCIPLES.md](PRINCIPLES.md).

No dates are promised. Stages move when their acceptance check passes, and
the operator decides at each stop point whether to continue or take the exit.

**Status words:** `done` (merged and live, with PR) · `in progress` (started,
not all acceptance checks passed) · `next` (ready to start) · `later`
(waits for a stop point or a measured need).

---

## Stages

Strangler pattern: the new path grows next to the old one; the persistent
fleet is paused instead of running in parallel.

| Stage | What | Status | Acceptance check |
|---|---|---|---|
| **E0** — direction and quiet mode | ADR-085 (#649) · build stop on dispatch, healers, Sessions chat, group chat · all persistent agents except the lead and voice agent paused (live since 2026-09-22) · Claude 5 model prices, so usage stops billing as zero (#650) · measuring layer: actor on status changes (#640), daily metrics digest (#641), run record (#645, #646) · still open: token baseline per source and week; page-usage measurement from existing logs | in progress | ADR merged; paused agents receive no dispatch (probe card stays in inbox); Claude 5 usage priced above zero; baseline table exists |
| **E1** — the new path by day | Procedure files (`SKILL.md` + `AGENTS.md`) on the CLI's goal loop — maintained outside this repo during the trial; moving them into the repo is part of this stage: plan → failing test → change → sabotage probe → fresh reviewer → PR → run record · remote start by day · three trial jobs (one outside this repo, one with a local helper on a GPU box) · **switch lock stage 1**: refuse a model switch while the engine reports running requests | in progress (procedure drafted; trial jobs and switch lock not started) | A deliberately red test ends as "not passed" in the run record, never as done · each job: run record in the vault, PR open, operator minutes ≤ 15, no manual status changes · local step produces an applicable `git diff`; an engine outage mid-step falls back to a cloud subagent and is recorded · a switch during a running local request is refused · three jobs: one with a large-model head, two with a mid-size head · a run record is understandable without MC knowledge in 5 minutes · interactive sessions stay logged in over a 48-hour probe · **comparison old vs. new** in usage share (not dollars): operator minutes, follow-up questions, manual rescues of heads, share of local steps, large vs. mid-size head (sabotage: a deliberately aborted head counts as one rescue) · procedure files live in the repo |
| **Stop 1** (~2 weeks after E0) | Is the new meaning of the north star ("team" = procedure plus short-lived heads) acceptable? Does the path work without the operator at the desk, outside this repo, with a local helper? Are GPU boxes as helpers acceptable? Is output-only on chat channels acceptable? | next | Operator decision recorded. **No → exit "harden the fleet":** fleet leaves quiet mode, hardening resumes, new ADR |
| **E2** — unattended runs | Scheduled background sessions without remote control · forbidden list enforced (ADR-085 decision 2) · usage switch (no new heads above the limit share) · stall notice job (reads run-record heartbeats, reports "blocked / stalled" to chat) · automatic fallback local → cloud helper | later | Evening job → PR and run record next morning, also with a simulated GPU-box outage · a job with a deliberately forbidden action produces a visible "blocked" notice by morning · interactive sessions stay logged in |
| **Stop 2** (~week 5) | Are unattended runs acceptable? Is the vendor's local coordinator available? → decision 5 final: adopt it, or build one lean page | later | Operator decision recorded; no → day-time operation only |
| **E3** — measured gaps only | Possibly switch lock stage 2 (lease with expiry), a jobs table without FK to tasks (or Markdown only), usage attribution by worktree path · **mandatory swap probe** with a second coding CLI, including watchdog and question path | later | A second CLI finishes one job with the same files, and its "blocked" notice arrives too |
| **E4** — channels and lead agent | Re-route chat inbound (channels become output-only, ADR-085 decision 4) · voice-agent decision (stop 3) · paused agents off for good, "restart individually if missed" | later | A chat message to the lead agent gets a clean pointer to the new start path instead of being dropped · voice decision recorded |
| **Stop 3** — voice agent | Keep, rebuild on the new path, or retire; decided together with the channel re-routing | later | Operator decision recorded |
| **E5** — clean-up | Delete frozen code in themed PRs (terminal remote control, healers, Sessions chat, `mc` back-channel, group chat, empty tables in one migration PR) · review the box manager against existing swap/orchestration tools (new ADR) · UI only as decision 5 says | later | CI green; fix commits on fleet-layer code near zero, and the overall fix share below the recorded baseline (counting: non-merge commits on `main` with a `fix` prefix over 60 days — 245 of 494, about 50 %, on 2026-09-23); existing installations get release notes for each removal |

---

## UI waves

Page-by-page overhaul of the existing UI (independent of decision 5: pages
that stay get fixed now). Every wave is one PR package with a click list of at
most ten items for the operator.

| Wave | What | Status | Acceptance check |
|---|---|---|---|
| **0b** — defuse one-click traps | "Approve all" removed from the command palette, confirmations with consequence sentence (cancel task, disable job, deactivate user, dispatch again), draft-discard prompt, provider-scoped API-key picker, installer shell only on click, demo-data lock on setup | done (#653) | No one-click action nudges an agent, cancels or approves without a confirmation |
| **0a** — 17 audited bug fixes | Overlay stacking on phones, click-outside only for non-forms, vault scroll, paused-aware agent counts, example-crew banner, tab state in URL, and others; follow-up sidebar count (#654) | done (#652, #654) | Each fix has a test that was red before and green after |
| **0c** — fast runtimes page | Runtime probes in parallel with a per-probe timeout, no cache | done (#651) | Page shows content in about the time of the slowest single probe (measured 14.4 s → 5.9 s) |
| **3a** — task detail lite | State card with embedded approval, facts row, "Summary" from the run record as default tab, ⋯ menu, grey monogram | in progress (#655 open, auto-merge on, waiting for a branch update) | Opens on the summary for finished work; tab and task live in the URL; "not found" on dead links |
| **1a** — open-everything probe + dialog kit + undo | A script that opens every panel and dropdown (run before and after each wave), one dialog/sheet/confirm component with the old props, undo through the existing `notify`, skeleton/empty/not-found states | next | Probe runs as one command with a write lock; Esc, focus and overlay stacking correct in all dialogs of the pages that stay |
| **1b** — rest of the kit | Menus/popovers, tabs, `useUrlState`, one Markdown renderer, page body, tokens, lint ratchet + i18n check | later (operator's call at the pause after 3a and 1a) | Ratchet counts only go down |
| **2** — overlays and URL on pages that stay | Tasks, Inbox, Memory, Settings, Runtimes, Schedule on the shared kit; confirmation for runtime stop | later (operator's call at the pause after 3a and 1a) | Every overlay on these pages passes the probe; state survives reload |
| **3b** — task detail full | Four tabs, activity stream with detail level, facts column on wide screens, keyboard navigation | later (after stop 2, only if the MC UI stays) | — decided at stop 2 |
| **4** — status vocabulary everywhere | One status module for agents and tasks, honest insights, formatters everywhere | later (after stop 2) | — |
| **5** — one language per screen | Remaining hard-coded labels to i18n (task form, insights, memory types) | later (after stop 2) | English UI shows no German label |
| **6** — phone | Palette and search in the header, responsive tables, settings as select, 16/44 px everywhere | later (after stop 2) | — |
| **7** — colour and polish | Colour rules per DESIGN.md, stricter token ratchet, strict probe | later (after stop 2) | — |

**Pause after 3a and 1a:** the operator looks at the result and decides whether
1b and 2 follow now or wait for stop 2.

Pages that are candidates for removal (Sessions, Office, loops, content, and
private add-on pages) get only what shared building blocks bring along.

---

## Risks and open checks

Each open check is answered by the stage named in brackets.

| Risk or open check | Consequence here |
|---|---|
| Remote start needs a full interactive login; long-lived tokens are rejected | Unattended runs (E2) use scheduled background sessions without remote control |
| Auto mode falls back to asking after 3 blocks in a row or 20 in total | Night runs can stall; hence short jobs, a precise forbidden list and the stall notice job (E2) |
| Usage quota is tight; long workflows may not pause at the limit in background sessions | Usage switch as a share of the current limit; at most 2–3 heads at once (E2) |
| Do all local engines report running requests the same way? | Blocks switch lock stage 1 until checked per engine (E1) |
| Do existing logs contain page views? | Decides whether the page-usage measurement needs any code (E0) |
| Does the status line (usage share) run in background and remote sessions? | Decides where the run record gets its usage share (E1/E2) |
| Is a mid-size head as good as a large-model head? | Measured in the E1 comparison; sets the default head size |

---

## Watch list

Checked 15 minutes a month (PRINCIPLES §3 rule 8). A hit can make a planned
build unnecessary — check here before building.

| Watch | Why it matters here |
|---|---|
| Local variant of the coding-CLI vendor's coordinator (announced 2026-09-17, cloud-only so far) | Decides stop 2: adopt it as the control room instead of building one |
| Coding-CLI changelog: local-model subagents, agent view, remote start with long-lived tokens | Local helpers without a wrapper; unattended remote start |
| Engine-side protection against model eviction (swap proxies) | Could replace the switch lock and part of the box manager |
| Subscription plan and usage-limit rules | Sets the usage share for automatic heads (provisional: 30 % of the weekly limit) |
| Board / multi-agent products | Only relevant above three parallel jobs or real multi-phase projects |

---

## How to update this file

1. **Change a status only with evidence:** the PR number for `done`, the
   passed acceptance check for moving a stage. Never promise a date.
2. **Stop points are the operator's call.** Record the decision (and, for a
   reversal, the new ADR) before changing anything after a stop point.
3. **New work enters as a measured gap** (PRINCIPLES §2 rule 3), not as a
   wish; add it to the stage where it was measured.
4. **Keep it one page.** Details belong in ADRs and PR descriptions; link
   them instead of copying.
5. **Public repository:** no personal names, agent names, host names or
   paths — "the operator", "the lead agent", "GPU boxes".
