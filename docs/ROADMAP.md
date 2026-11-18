# Mission Control — Roadmap

Where the project is on the direction set in
[ADR-102 — MC is the cockpit for your AI](decisions/102-cockpit-direction.md).
The rules behind it: [PRINCIPLES.md](PRINCIPLES.md).

No dates are promised. The order is: first MC becomes something the operator
uses daily, then it becomes installable for others. Every stage ends with
something the operator can see and use. The operator decides at each stop
point whether to continue, reorder or stop.

**Status words:** `done` (merged and live, with PR) · `in progress` (started,
not all acceptance checks passed) · `next` (ready to start) · `later`
(waits for an earlier stage or a stop point).

**Before this roadmap:** the previous roadmap (stages E0–E6 under ADR-085 to
ADR-101) delivered, among others: the measuring layer (usage baseline per
week and source, the route beacon for page views, #692), the switch lock
across engine families, the head launcher and night shift as opt-in
previews, chat features (live diffs, todo panel, cache rate, question cards
and plan mode, tool screenshots), and the removal of several dead verticals.
Its full text is in the git history of this file (`git log -p -- docs/ROADMAP.md`).

---

## Stages

| Stage | After it, the operator can … | Content | Clarify first | Operator's call | Status |
|---|---|---|---|---|---|
| **0** — write down the direction | … rely on every helper reading the new direction in the repo instead of the old one | ADR-102, PRINCIPLES and this roadmap · three checks that decide later stages, without rebuilding anything (see Open checks) · sort the open PRs: finish, close or park — some belong to the old direction (for example #515 task detail, #871 lead agent at first run) | — | Which open PRs to close | in progress |
| **1** — the chat is home | … open MC, land in the last chat, see all chats with their state on the left, and pick model and harness per chat | New navigation (the variant the operator chose) · speed line under the input · chat list with state and search (N4) · chats instead of fixed agents — changes how MC stores conversations, the largest piece · polish of history and input (N1, N2) · approvals with three fixed buttons and a permission level per chat (N3) | What becomes of today's agents and their histories? Where does a chat's working folder live? How does the list look on the phone? | Pictures (phone first) before anything visible goes live | next |
| **2** — clean up: fewer pages, projects instead of tasks | … use a short menu (chat, models, knowledge), projects as folders, and honest numbers in Insights | The task page goes; a project is a folder with its chats and knowledge · knowledge in one place: notes, skills, project knowledge · every page gets a verdict: core, module or removed, each removal with evidence (page views) and a way back · Insights: one switch all / cloud / local for the whole page, with cost; local usage priced at API prices · the separate voice assistant goes, voice becomes a function in every chat | What happens to old tasks and their data (way back)? Scheduled runs and loops: core or module? | The list "stays · module · removed" per page | later |
| **3** — models and devices in the core | … switch the model mid-chat without losing the thread, and set up a new device from MC | Model switch with hand-over of the conversation (N5) · many providers through one seam (ACP), depending on the stage 0 check · devices page in the core: GPU boxes, Windows PCs, later others · first start finds installed agents and projects instead of creating sample agents (N7) · Tailscale set up from MC | How much of the conversation moves on a switch? Which providers first? | Little: mostly invisible, goes live after tests and a counter-check; anything visible as in stage 1 | later |
| **4** — a real product | … have someone else install MC in one step on Mac, Windows or NAS, and everything works | One-step install; Windows means the MC server runs on Windows · update by button with backup and way back (decided in ADR-094) · several users · an automatic test of every feature on a fresh install before every release (a start: open PR #873) · modules with a fixed interface and a marketplace; first candidates: X content, news | What does the module interface look like? What may a module do, and what not (security)? How much of ADR-094 is built? | Whether and when MC is promoted publicly (outside effect) | later |
| **5** — the phone app | … get a notice on the iPhone, approve with one tap, and dictate a message | Own iOS app for all of MC: notices only for what matters, one-tap approvals, widgets · dictation: text lands in the input, sent only on tap (N6) · messages wait while there is no network (N8) | How the app is built. How notices arrive without content sitting at a third-party service (iPhone notices always pass Apple's push service — what they contain is the question) | Money: a paid Apple developer account; other costs not clear yet | later |

## Packages N1–N8

Chat and phone patterns rebuilt independently (ADR-102 principle 9). Nothing
is built yet.

| Package | What | Stage | Needs first | Status |
|---|---|---|---|---|
| N1 chat history | Duration in the tool line, an "answer only" detail level, a "changed files" card under the answer, a helper line ("4 helpers, 3 working") | 1 | — | next |
| N2 input | `@` files, quote an answer, long pasted text becomes a file | 1 | — | next |
| N3 approvals | Three fixed buttons, typing an answer = no with a reason; four permission levels per chat | 1 | What each harness supports in levels | later |
| N4 chat list | Chats instead of agents, state per row, search, pin | 1 | Picture round with the navigation | later |
| N5 model switch with thread | The conversation moves along when model or harness changes | 3 | Spec and ADR; the stage 0 check of what survives today | later |
| N6 dictation | Speech to text in the input, sent only on tap | 5 | Where recognition runs (local) | later |
| N7 first start | Finds installed agents and projects | 3 | ADR-102 (this change) | later |
| N8 notices to the phone | Approval notice, queue without network | 5 | The own iOS app | later |

## Counter-positions considered

- **Phone app earlier.** With Slack, Telegram and Discord off, no notice
  reaches the phone until stage 5. If that hurts in daily use, a simple
  notice moves forward; which way works for it is not checked.
- **Product earlier (stage 4 before 1).** Makes sense if others should
  install MC soon. Catch: the install gets polished for a surface that
  changes right after.
- **Clean-up first (stage 2 before 1).** Less code makes every rebuild
  easier. Catch: nothing new to see for a long time, and MC stays hardly
  used.

## Parked, not in the order

- A phone agent and "share to MC" (an inbox for raw material).
- Check a DeepSeek harness.
- External harnesses working cards over MCP (ADR-101, built, deferred).

## Open checks

Each is answered by the stage in brackets; none of them rebuilds anything.

| Open check | Decides |
|---|---|
| Does the open agent protocol ACP carry Claude Code and Codex, not only omp? (0) | Stage 3: many providers through one seam |
| What survives a model or harness switch mid-chat today? (0) | N5 |
| Does the Windows installer (`setup.ps1`, sets up WSL2 and Docker) run cleanly today? (0) | Stage 4: Windows as an equal platform |
| Which pages are opened at all? | **Answered:** the route beacon (`POST /api/v1/usage/page`, #692) records views per route since 2026-09-28. The numbers feed stage 2's verdicts and stay out of this repo |
| How long each stage takes | Unknown on purpose; no dates until a stage is planned in detail |

## Watch list

Checked 15 minutes a month (PRINCIPLES §3 rule 8). A hit can make a planned
build unnecessary — check here before building.

| Watch | Why it matters here |
|---|---|
| Coding-CLI and harness changelogs: ACP support, local-model subagents, remote start with long-lived tokens | Stage 3 providers; fewer own adapters |
| Engine-side protection against model eviction (swap proxies) | Could replace the switch lock and part of the device management |
| Subscription plan and usage-limit rules | Sets what cloud fallback costs and the usage share for automatic heads |

The coherence thread (product map and `kz check`, [docs/produkt/README.md](produkt/README.md))
runs alongside every stage and gates nothing.

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
   paths — "the operator", "GPU boxes".
