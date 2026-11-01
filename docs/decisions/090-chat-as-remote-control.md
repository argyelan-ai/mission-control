# ADR-090 — The MC chat is the operator's remote control; the chat may grow again; omp stays the local harness

**Status:** Accepted (2026-10-12) · amends ADR-085 §4 and ADR-089 §4 / §9 (the chat exception) and PRINCIPLES §2 rule 4 · touches ADR-084 (capability row for omp)
**Amended by:** [ADR-099](099-chat-workspace.md) — §1 feature list gains the header buttons changes / browser and a resizable side panel; see the note at the end
**Re-justified by:** [ADR-100](100-foundation-and-cross-harness-comms.md) §5 — the chat stays for harnesses without their own phone app; see the note at the end
**Datum:** 2026-10-12
**Scope:** Architecture/Direction · Frontend/Pages · Backend/Services · Docker/Agent-Images

## Context

ADR-085 §4 froze the Sessions chat (safety and operations fixes only).
ADR-089 made one exception — the lead agent's chat input and driver — and kept
two fixed agents: the lead agent and one local omp agent driven over ACP
(§4, §5; PRINCIPLES §8 rule 2).

What was established before this decision:

- **Away from the desk, the MC chat is the operator's only way to work.** A
  desktop coding client used at the desk cannot be reached from the phone;
  MC can. The operator: "MC chat is my remote control." This matches the
  earlier rule that MC on the phone is the main channel (operator, 2026-09-24).
- **Local-model work mostly bypasses MC today.** The box token ledger (#779)
  counted 1 812 450 fresh tokens on the GPU boxes on 2026-10-11; about 24 000
  came through MC's local agent (query on `runtime_token_hours`;
  `backend/app/services/usage_baseline.py`). Two days of data only.
- **The chat's reliability problems are MC's own.** "A turn is already
  running" is raised by MC's chat daemon (`docker/omp-bridge/acp_chat.py`,
  the `busy` reply). #777 added the follow-up queue and a real `/new`; the
  running local agent's container contains it. Whether the operator still
  hits the error in daily use is not verified. Further known items: a
  hard-coded omp slash list in `backend/app/services/agent_chat_input.py`,
  and omp's session-cumulative token counters that MC recomputes.
- **omp already supports plugins, skills and MCP natively — MC just does not
  feed them.** The omp then pinned in the agent image (18.1.10) has `omp
  plugin install|uninstall|list|enable|disable|marketplace|discover`
  (`--scope user|project`, `--json`), skill discovery (`--skills`,
  `--no-skills`) and extensions (`-e`, `--hook`) (`omp --help`, `omp plugin
  --help` in the container). omp's docs (newer version) describe the
  marketplace as compatible with the Claude plugin format and the official
  marketplace, skills under `<root>/skills/<name>/SKILL.md` plus
  `skills.customDirectories`, and MCP servers in `agent/mcp.json` of the
  active profile, also importing the coding CLI's user config (oh-my-pi
  `docs/marketplace.md`, `docs/skills.md`, `docs/mcp-config.md`); that the
  pinned version behaves the same in every detail is not proven. MC's omp
  profile has no `skills/`, no `mcp.json` and an empty plugin list, because
  `HARNESS_CAPABILITIES["omp"]` switches plugins and skills off
  (`backend/app/services/harness_compat.py`). The omp pin moved to 18.10.1
  the same day (#788).
- **MC's own plugin and MCP layers exist:** a central plugin store in the
  Claude format with a per-agent allowlist
  (`backend/app/services/plugin_manager.py`), an MCP registry with dry run,
  rollback and approval types `install_mcp` / `uninstall_mcp`
  (`backend/app/services/mcp_registry.py`, ADR-016). Missing are the
  browsing, the one-tap UI and the write path into omp.
- **Effort exists in the composer** (`agent_chat_input.py`, ACP `config` op
  `thinking`). MC's rendered omp `models.yml` sets no thinking format for the
  local model; omp knows a `chat-template` format that sends
  `chat_template_kwargs` (omp docs `provider-compat-reference.md`, newer
  version). Whether the levels change anything on the local model is not
  measured.
- **No agent can create a schedule** (no such tool in `scripts/mc-mcp.py` or
  `backend/app/routers/agent_scoped.py`). The MCP agent mode (#783) is the
  place for such a tool.
- **Delivered the same day:** the todo panel as a harness-neutral chat event
  (#787, #790), the cache hit rate in the context ring (#786, #791), refusal
  of install/modify slash commands typed as chat text (#792), and package P1
  — live tool output, per-edit diffs, file chips and session cost (#794,
  #795).

## Decision

**The MC chat is the operator's remote control — the main channel away from
the desk. The chat of the two fixed agents is unfrozen and may grow, inside a
closed feature list and a budget. omp stays the local harness, driven over ACP
as today.**

### 1. Scope of the unfreeze

- Unfrozen: the chat (input, driver, chat panels) of the **lead agent** and
  the **one local omp agent** (ADR-089 §4).
- Still frozen (ADR-085 §4 unchanged): paused agents' chat input, group chat,
  dispatch, healers, watchdogs, task core.
- **Feature list (operator, 2026-10-12: "port everything our chat does not
  know or cannot do yet"):** (1) chat reliability, (2) todo panel = task
  checklist (#787, #790), (3) cache rate (#786, #791), then the omp port
  packages from the capability inventory: P1 live tool output, diffs, file
  chips, cost (#794) · P2 screenshots/images from tools · P3 question cards
  and plan mode · P4 session list, resume, fork · P5 send an image ·
  P6 install plugins / skills / MCP · P7 small items; plus effort per harness
  (measured on the local model) and automations from the chat. A capability
  outside this list needs a measured gap (ADR-085 §3) and the operator's go.
- Phone first: every feature is accepted on a 390 px phone screen in both
  languages and both themes (PRINCIPLES §7 rule 10 stays: monitor and
  intervene, no full editing).

### 2. How plugins, skills and MCP reach omp

No new harness and no omp fork — MC writes into omp's own native places:

| Part | Today for omp | New |
|---|---|---|
| MCP servers | none in the profile | the existing MCP sync writes the agent's allowlisted servers into the omp profile's `agent/mcp.json`; secrets come from the secret store at render time, not from the manifest |
| Skills | none | allowlisted skills are copied (or listed via `skills.customDirectories`) into the omp profile; `SKILL.md` is the shared format |
| Plugins | `cli_plugins=False` | install through omp's own `omp plugin install <name>@<marketplace> --scope user`, pointed at MC's central marketplace cache; MC's allowlist decides, omp executes |
| Parts omp cannot run | — | shown as "not on this harness", never hidden |

**Install flow (operator, 2026-10-12: "if I tell an agent to install XY it
should be able to, and it must survive restarts — uncomplicated"):** the
operator asks the agent (or types the slash command, or taps a catalog
entry) → the agent prepares the install → **one approval card with one tap**
in the chat → MC records it in the agent's allowlist and renders it into the
harness's native place → it survives container recreates because MC
re-renders from the allowlist. Until that card exists, install/modify slash
commands typed in the chat are refused (#792).

The capability row `HARNESS_CAPABILITIES["omp"]` gets `cli_skills` /
`cli_plugins` only after a live proof (§4). This changes the capability row
(ADR-084), **not** the harness × runtime pair matrix of ADR-086.

### 3. Chat reliability first

Stage 1 is reliability, regardless of features: reproduce "a turn is
already running" with the operator on the phone (fixed in code by #777;
confirm or fix), the slash list read from what omp advertises instead of a
hard-coded list, honest error lines for an unreachable daemon, and one
contract test for the chat control protocol (ADR-071 pattern) with a
sabotage probe.

### 4. Success measure — remote control

- **Primary:** five defined jobs, each done **completely from the phone**,
  without the desk, within two weeks after stage 5:
  (1) start a code fix with the local agent and follow it to a PR;
  (2) answer a question / approve a card in the chat;
  (3) install an MCP server for the local agent and see it used;
  (4) create an automation from the chat and read its result in the chat;
  (5) a long chat with follow-ups, Stop and `/new` without a chat error.
  Pass = 5/5.
- **Reliability:** chat error lines (`busy`, unreachable) per week, read from
  the transcripts — target 0 caused by MC.
- **Secondary signal:** the share of local fresh tokens through MC
  (`via_mc_local` vs. `external`, #779). Baseline ~1 %. Not a pass/fail: desk
  work outside MC is allowed.

### 5. Guardrails against the build pattern

1. **Budget per package:** each package (P1–P7, effort, automations) plans
   its PRs up front; one that needs 50 % more than planned stops and goes to
   the operator.
2. **Feature list** (§1) — nothing outside it without a measured gap.
3. **Stop points** (operator decides): after stage 1 (reliability proven on
   the phone), at each visible UI (picture gate, ADR-089 §2), at the effort
   measurement (keep or hide the switch), at the success check.
4. **No new third-party component.** omp stays pinned (18.10.1 since
   2026-10-12, #788); every further omp update is its own decision with its
   own live test, not a side effect of this ADR.
5. If the success check fails, nothing new is started; the operator decides
   between fixing the failing job and stopping.
6. **Live proof before done:** every package is accepted only after a live
   test in the real MC on the phone and desktop with real data and a
   screenshot to the operator (operator rule 2026-10-12).

### 6. Rollback

Every feature ships behind the existing switches or at L3 autonomy and is
undone by a revert PR. The omp profile writes (MCP, skills, plugins) are
re-rendered from MC's allowlists; an empty allowlist restores today's empty
profile. The unfreeze itself is undone by a new ADR.

### 7. What this changes

| Where | Before | Now |
|---|---|---|
| ADR-085 §4 | Sessions chat: safety and ops fixes only | the chat of the two fixed agents may gain the features of §1; the rest of §4 unchanged |
| ADR-089 §4 / §9 | exception: the lead agent's chat input and driver | exception widened to the chat of both fixed agents (lead + local omp agent) |
| PRINCIPLES §2 rule 4 | "Exception: the lead agent's chat input and driver (ADR-089)" | "Exception: the chat of the two fixed agents (ADR-089, ADR-090)" |
| ADR-084 capability row | omp: no plugins, no skills | omp: skills and plugins after the live proof (§2) |
| ADR-089 §5, PRINCIPLES §8 rule 2 | omp over ACP as the local brain | **unchanged** |
| ADR-086 matrix, PRINCIPLES §3 rule 5 | — | **unchanged** |

## Alternatives

- **Phone cockpit only** (watch and steer, no new chat features) → not
  enough: away from the desk the operator has no other way to *work*.
- **Pi replaces omp** (earlier draft of this ADR) → withdrawn by the operator
  on 2026-10-12: no ACP in Pi, a new chat channel and a removal of 23 000
  lines of omp bridge are not needed for the remote-control goal; omp already
  has plugins, skills and MCP natively.
- **Build the features in the desktop client instead** → not reachable from
  the phone.
- **Unfreeze the whole Sessions chat** → no: paused agents stay frozen; only
  the two chats the operator uses grow.

## Consequences

### Positive

- One remote control with MC's approvals, both languages and both brains.
- omp agents get plugins, skills and MCP for the first time, through omp's
  own mechanisms, not a fork.
- Reliability is measured from transcripts instead of felt.

### Negative

- **The chat grows again** — the area ADR-085 froze because of its repair
  load. Budget, closed list and stop points exist for that.
- **omp moves fast:** the version this was measured on (18.1.10) was several
  breaking releases behind and was bumped to 18.10.1 (#788); omp's docs may
  describe a newer version still, so each omp-side step is proven live on
  the pinned version first.
- **Plugin hooks run code** inside the agent's container; installs stay
  behind an approval card.

### Open source / existing installations

Nothing is removed. New features ship behind existing switches or at L3;
installations that do nothing keep today's behaviour.

## Note (2026-11-01) — amended by ADR-099

[ADR-099 — Chat workspace](099-chat-workspace.md) adds two items to the
closed feature list of §1, approved by the operator on 2026-11-01: the
changes / browser buttons in the chat header (detail level into ⋯) and a
resizable side panel. Layout only; the text above is not rewritten.

## Note (2026-11-01) — re-justified by ADR-100

[ADR-100 — MC is the foundation under every harness](100-foundation-and-cross-harness-comms.md)
§5: the context line "a desktop coding client cannot be reached from the
phone" no longer holds for the coding CLI, whose vendor ships a remote
control. The chat stays the remote control for harnesses **without** their
own phone app — the local omp agent, the lead agent, and Hermes until its own
app is stable. MC chat features for coding-CLI sessions are parked. The text
above is not rewritten.

## References

- Chat seam: `backend/app/services/acp_chat_transport.py`,
  `docker/omp-bridge/acp_chat.py`, #777
- Capabilities: `backend/app/services/harness_compat.py` (`HARNESS_CAPABILITIES`)
- Plugins/MCP: `backend/app/services/plugin_manager.py`,
  `backend/app/services/mcp_registry.py`, `backend/app/services/mcp_sync.py`,
  ADR-015, ADR-016
- Effort: `backend/app/services/agent_chat_input.py`
- Usage split: `backend/app/services/usage_baseline.py`, #779
- omp: `omp --help`, `omp plugin --help` (18.1.10, container); oh-my-pi docs
  `marketplace.md`, `skills.md`, `mcp-config.md`,
  `provider-compat-reference.md` (newer version); pin bump #788
- Delivered under this ADR: #786, #787, #790, #791, #792, #794, #795; MCP
  agent mode #783
- Related ADRs: ADR-016, ADR-071, ADR-084, ADR-085, ADR-086, ADR-089
