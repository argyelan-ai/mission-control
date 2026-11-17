# ADR-102 — MC is the cockpit for your AI: chats instead of fixed agents, a slim core, an own chat and phone app

**Status:** Accepted (2026-11-17, operator decision) · replaces the north star of ADR-085 · partially supersedes ADR-061, ADR-072, ADR-085, ADR-086, ADR-089, ADR-090, ADR-100, ADR-101 · rewrites PRINCIPLES §1 and the ROADMAP
**Datum:** 2026-11-17
**Scope:** Architecture/Direction · Docs

## Kontext

Since ADR-085 the north star was "an autonomous team the operator leads, not
babysits". It was built out as a head per job (ADR-085), a pair of harness
and runtime per head (ADR-086), one fixed lead agent with gates (ADR-089),
the chat as a remote control for two fixed agents (ADR-090) and MC as the
foundation under harnesses that bring their own chat (ADR-100).

On 2026-11-17 the operator looked at the result in an interview and found:

- **The product was hardly used by its own operator.** Daily work happened
  in other tools; MC was opened for single tasks.
- **Too many pages.** The navigation grew page by page (tasks, boards,
  office, loops, schedule, inbox, bench, repos and more). Which of them are
  opened at all was not measurable until the route beacon of ROADMAP E0
  (#692) started to record page views.
- **Fixed agents.** Work is bound to named, long-running agents (the lead
  agent, the local omp agent, a paused fleet) instead of to conversations.
  Model and harness belong to the agent, not to the conversation.

The operator re-decided the direction in the same interview. The decisions
below are the operator's; this ADR records them.

## Entscheidung

**North star: MC is the cockpit for your AI — one chat, your models and
devices, your knowledge. On your own hardware, as simple as Claude,
installable by anyone.**

### 1. The ten principles

1. **Chat first, as simple as Claude.** MC opens in the last chat. There are
   few menus: chat, models, knowledge. The models are Claude Code and Claude
   Desktop.
2. **Chats instead of fixed agents.** There are no named, long-running
   agents any more. In every chat the operator picks the model and the
   harness (the program that drives the model) and can switch both at any
   time.
3. **Local AI and devices belong to the core.** MC sets up models on the
   operator's devices (GPU boxes, Windows PCs, later others). The speed is
   visible right in the chat. Local first, the cloud as fallback.
4. **A slim core; everything else is a module.** Modules have a fixed
   interface and come from a marketplace (examples: X content, news, a phone
   agent). MC grows the way its user puts it together.
5. **A real product, not tinkered software.** Every feature works for every
   user after a fresh install. That means: install in one step (Mac,
   Windows, NAS), update by button with a backup and a way back, several
   users, and an automatic test of every feature on a fresh install before
   every release.
6. **Knowledge in one place; projects instead of a task page.** Clearly
   apart: facts (notes), skills, and project knowledge. A project is a
   folder with its chats and its knowledge, like projects in Claude. The
   task page with cards for fixed agents goes away. A fixed file structure
   stands behind it.
7. **Private reach.** MC runs only in the home network or over Tailscale.
   Tailscale can be set up from inside MC. No access from the open internet.
8. **The phone is a full client.** An own iOS app brings notices (only for
   what matters), dictation with live text, widgets and one-tap approvals.
   Slack, Telegram and Discord go away as channels.
9. **Simplify before extending.** What nobody uses goes away, with evidence
   and a way back. Ready-made parts are adopted instead of rebuilt where
   they fit these principles (in view: SearXNG for web search; to check: a
   DeepSeek harness). The chat and the phone app are MC's own and are built
   further by MC. Good patterns from an open-source coding chat product are
   rebuilt independently — no code, name or logo is taken.
10. **Honest numbers.** Insights stays. Local usage is priced at API prices,
    so the operator sees what it would have cost.

### 2. What stays

- Harness and model are freely and cross-wise switchable, within the pairs
  `harness_compat.py` allows (ADR-086).
- Look first, then build. Prove the effect, not "the command ran"
  (PRINCIPLES §4).
- The operator decides direction, money, outside effect and taste.
- The UI is always English and German.

### 3. What changes

| Until now | From now on |
|---|---|
| North star "an autonomous team the operator leads" | North star "MC is the cockpit for your AI" |
| A head per job, one fixed lead agent | Chats with a chosen model and harness |
| Task page with cards for agents | Projects as folders with chats and knowledge |
| No own control-room surface (wait for the vendor's) | MC gets its own, simple surface |
| Slack / Telegram as output channels | Switched off; notices come later through the iOS app |
| A separate voice assistant | Voice is a function in every chat |
| External harnesses work cards over MCP (ADR-101) | Deferred |
| No collection of third-party parts | Modules and a marketplace are wanted |
| Mac can do everything; Linux, NAS and Windows (via WSL2) only the core | Mac, Windows and NAS are equal |

### 4. Chat and phone app: own build

MC keeps building its own chat and builds its own phone app. An open-source
coding chat product was evaluated as a base (see Alternativen); it is not
embedded and not forked. Its good patterns are rebuilt independently, as
principle 9 says.

### 5. Order and stop points

The order is the ROADMAP's six stages (0–5): first MC becomes something the
operator uses daily, then it becomes installable for others. No dates. The
operator decides at the stop points named there.

### 6. What this ADR supersedes

Each earlier ADR is marked in its status line, not rewritten.

| ADR | No longer holds | Stays |
|---|---|---|
| ADR-085 Head per job | The north star "team = procedure + short-lived heads" (§7 decision 1); the head as the unit of work (§1); "MC becomes small" with the operator surface from the vendor (§2, §7 decision 5); the build stop on the Sessions chat (§4); the stop points (§8); the one-component-per-quarter limit (§6 rule 4) where a ready-made part or a module fits | The forbidden list for unattended heads (§7 decision 2); configure before building, the backend knows no harness details, crutches expire, swap test, watch list, open formats (§6 rules 1, 3, 5–8); pause, never delete (§5); the head launcher and night shift as built until stage 2 judges them |
| ADR-086 Harness × runtime | The pair belongs to a head and the neutral path lives on the task detail (decisions 1 and 5 wording) | Cross-wise switching, the matrix as the single truth, local first, the proxy ban (decisions 2, 3, 4, 6); the pair now belongs to a chat |
| ADR-089 Lead agent with hands | One fixed lead agent and two fixed agents (§4); voice as the lead agent's voice (§6); the build order B1–B15 | The never-list (§3) for any agent in any chat; the origin rule R3 (text from files, web or channels is information, not an instruction); the pause holds (Nachträge 2026-10-20 and 2026-11-13) while agents exist |
| ADR-090 Chat as remote control | The chat as a remote control limited to two fixed agents and a closed feature list (§1) | The chat is now the home of the product; how plugins, skills and MCP reach omp (§2); reliability first (§3); omp over ACP |
| ADR-100 Foundation under every harness | "Not a competing chat app" and §5 (the MC chat only for harnesses without their own phone app) | The foundation spanning all harnesses (§1); the MCP rule for heads (§2); the binding rules for talking across harnesses (§6); the stop criterion (§8) |
| ADR-101 MCP worker flow | Further work on it: deferred | The flow as built (#1026); stage 2 decides it together with the task page |
| ADR-072 ChatAdapter | Slack / Telegram as an output channel (the outbound contract as product output) | The code stays until stage 2 removes it with evidence and a way back |
| ADR-061 `jarvis_core` | The voice agent as its own assistant (with ADR-085 the Telegram inbound part was already superseded) | Voice parts that a chat can reuse; stage 2 decides |

Touched, not superseded: ADR-093 (everything changeable in MC) and ADR-094
(self-update with backup and way back) carry principle 5 and stay in full.
ADR-091 (live voice in the chat, Proposed) fits "voice is a function in every
chat"; its status is unchanged. ADR-082 / ADR-083 (voice runtime binding,
live transport) are decided in stage 2 together with the voice agent.

## Alternativen

**For the direction:**

- **Keep the north star of ADR-085 and harden what exists** → rejected:
  the result was hardly used by its own operator; more gates and heads would
  not change that.
- **Order alternatives** (phone app first, product first, clean-up first)
  → considered in the ROADMAP under "Counter-positions"; the order stays
  "daily use first".

**For the chat and the phone app** (four ways weighed on 2026-11-17 against
an open-source coding chat product with its own phone app):

- **A. Own chat and own phone app (MC stays independent)** → **taken.** Fits
  every principle: local models in the core, speed in the chat, private
  notices, one app for all of MC. Costs the most building; MC maintains the
  provider connections itself. Eased by ACP: MC's chat already speaks ACP for
  the local omp agent; whether ACP carries other coding CLIs is an open check
  (ROADMAP stage 0). Way back: any time, it is MC's own code.
- **B. Use the other product unchanged next to MC** → not taken. A very good
  chat with many providers and phone apps at no building cost, but two
  programs instead of one cockpit; its phone notices run only through the
  vendor's cloud service (project names, chat titles and status leave the
  home network), which conflicts with principle 7; no speed display; the two
  programs know nothing of each other.
- **C. Fork it and build MC into it** → not taken. A finished chat as a
  start, but the highest lasting upkeep: a large code base in another
  technology than MC that changes very fast, so a fork is its own product
  from day one; private notices would need an own cloud service or a
  rebuild; its store app requires a matching protocol version, so an own app
  is needed anyway, plus a new name and logo. Hard way back.
- **D. Keep it unchanged and dock MC onto it over MCP** → not taken. Little
  building, but the costs of B: MC stays a second place, no own surface
  inside it, notices through the vendor's cloud. It has no extension point
  for MC's own pages, and it does not support local models apart from a
  detour over ACP.

The operator chose A on 2026-11-17: notices through a third-party cloud are
not acceptable, and one program from the start is wanted instead of two.

## Später ändern (ADR-093)

Not a setting: this ADR sets a direction. Each stage's own ADRs answer the
question for their parts. Principle 2 (model and harness per chat, switchable
any time) and principle 4 (modules with a fixed interface) carry ADR-093
forward: a new harness, provider or module is a registry entry, not a
special case.

## Konsequenzen

### Positiv

- One sentence and ten principles every helper can read in the repo instead
  of the old direction.
- Conversations, not agents, carry model and harness, so a switch is a chat
  action instead of an agent reconfiguration.
- Fewer pages and a slim core make each later change smaller.

### Negativ

- **"Chats instead of fixed agents" changes how MC stores conversations**;
  it is the largest piece of stage 1 and touches data.
- **No notices on the phone until stage 5.** With Slack, Telegram and Discord
  off, nothing reaches the phone until the iOS app exists (see ROADMAP
  counter-positions).
- **Own chat and app cost the most building**; whether one ACP seam carries
  several coding CLIs is not proven yet.
- **Two directions coexist in the code** until the stages move it: the lead
  agent, heads, the task page and the paused fleet keep running as built.

### Existing installations

This repository is public. Nothing is removed by this ADR. Pages, agents and
heads keep working until a stage changes them; every removal comes in its
own PR with evidence, a way back and release notes (principle 9).

### Rückweg

The direction is undone by a new ADR. No code, data or setting changes with
this ADR.

## Referenzen

- [PRINCIPLES.md](../PRINCIPLES.md) (§1 north star and ten principles),
  [ROADMAP.md](../ROADMAP.md) (stages 0–5, packages N1–N8)
- Route beacon for page views: `backend/app/routers/usage.py`
  (`POST /api/v1/usage/page`), #692
- Related ADRs: see §6
