<!-- GENERATED FILE — DO NOT EDIT BY HAND. -->
<!-- Source: docs/harness-matrix.json · regenerate: python3 tools/render-harness-matrix.py -->
<!-- Guard: backend/tests/test_harness_matrix.py fails when this file and the source drift. -->

# Harness matrix

What every harness can do in Mission Control, and how mature it is — one place, so nobody has to dig the facts together again.

- **Source:** [`docs/harness-matrix.json`](harness-matrix.json) — the per-harness matrix below **and** the dev-ring declarations of [harness-dev-ring.md](harness-dev-ring.md) (24 task capabilities per harness × place).
- **Guard:** `backend/tests/test_harness_matrix.py` turns red when a harness known in code has no column, when this file drifts from the source, or when cited evidence no longer exists. `backend/tests/test_harness_dev_ring.py` guards the ring section.
- Columns are the harnesses MC knows in code (`harness_compat`, `host_harness_adapter`, `compose_renderer`) plus the terminal session of ADR-100 stage 2c.

**Status words**

- `yes` — works today; the cell cites code (`path:line`) or a dated live measurement
- `partial` — works with a named limit (one place only, behind a switch, …); evidence cited
- `no` — absent today — by design, not applicable, or a known gap; carries `since` (decided or first recorded) + `recheck` (how we would notice it changed)
- `open` — unproven — a counted gap, never a guess

Places: `C` = container (cli-bridge image), `H` = host (launchd adapter). Numbers in brackets point to the evidence list at the end.

## Columns

| Capability | Claude Code | OpenClaude | omp | Kimi Code | Hermes | Grok Build | Voice (realtime) | Terminal session (guest card) |
|---|---|---|---|---|---|---|---|---|
| Code name | `claude` | `openclaude` | `omp` | `kimi` | `hermes` | `grok` | `jarvis` | `guest-session` |
| Kind | harness | harness | harness | harness | harness | harness | harness | session |
| Places | container, host | container, host | container, host | container, host | host | host | host | — |
| **Maturity** | **stable** — the lead agent runs on it (host) and heads are offered; dev ring complete except ssh-push | **experimental** — no live agent; head run unproven; host binary far behind the pinned version | **stable** — the local-model agent (container, ACP chat with thinking, tools, resume); pinned 18.14.2 | **frozen** — task loop wired, but no live agent, no MC chat, cloud only, not offered as head (needs --yolo) | **frozen** — agent paused since quiet mode (ADR-085); MC chat kept until its own app is stable (ADR-100 §5) | **frozen** — agent paused since quiet mode (ADR-085); xAI cloud only; no MC chat; not offered as head | **frozen** — voice concierge, no task loop; to be replaced by the lead agent's voice (ROADMAP Stop 3) | **beta** — ADR-100 stage 2c, shipped 2026-11-01 (#1014, #1015); Claude Code hook only, not yet measured in daily use |
| Last verified | 2026-11-02 | 2026-11-02 | 2026-11-02 | 2026-11-02 | 2026-11-02 | 2026-11-02 | 2026-11-02 | 2026-11-02 |
| Dev-ring score | C: 23 yes / 1 no / 0 open; H: 23 yes / 1 no / 0 open | C: 20 yes / 1 no / 3 open; H: 21 yes / 1 no / 2 open | C: 23 yes / 1 no / 0 open; H: 21 yes / 1 no / 2 open | C: 22 yes / 1 no / 1 open; H: 22 yes / 1 no / 1 open | H: 14 yes / 1 no / 9 open | H: 22 yes / 1 no / 1 open | not in the ring | not in the ring |
| Open cells (this table) | 4 | 9 | 2 | 4 | 9 | 8 | 4 | 3 |

## Runtime and model

| Capability | Claude Code | OpenClaude | omp | Kimi Code | Hermes | Grok Build | Voice (realtime) | Terminal session (guest card) |
|---|---|---|---|---|---|---|---|---|
| Wire protocol | **yes** `anthropic` [1] | **yes** `openai` [2] | **yes** `openai` [3] | **yes** `kimi` — Moonshot managed endpoint, own OAuth files [4][5] | **yes** `openai` [6] | **yes** `grok` — xAI cloud over its own OAuth [7][8] | **yes** `voice` — realtime speech socket [9][10] | **no** — a terminal session is not an MC agent — it binds no MC runtime; its own CLI decides the model (since 2026-11-01) [11] |
| Runtimes it can bind to | **yes** — runtimes of type `anthropic*` / slug `anthropic-claude-*` [12] | **yes** — every OpenAI-compatible type (vllm_docker, llamacpp_docker, ssh_process, lmstudio, openai_compatible, unsloth, cloud) [13][14] | **yes** — every OpenAI-compatible type; must keep a binding or the container will not boot [13][14][15] | **partial** — only the `kimi-cloud` seed runtime; the binding is a display anchor [4][16] | **yes** — every OpenAI-compatible type (host only) [13][14] | **partial** — only the `grok-cloud` seed runtime; the binding is a display anchor [7][17] | **yes** — `voice_openai` or `voice_xai` [9][18] | **no** — a terminal session is not an MC agent — no runtime binding (since 2026-11-01) [11] |
| Local (self-hosted) models | **partial** — fleet matrix says no (anthropic protocol only); heads can run it on a local engine via `ANTHROPIC_BASE_URL` — experimental v1 [1][19] | **yes** — any OpenAI-compatible local engine [13][14] | **yes** — the default local harness; local runtimes get longer turn and task deadlines [13][14][20] | **no** — cloud only; a custom provider in config.toml is not wired (since 2026-07-24) [4] | **yes** — any OpenAI-compatible local engine [13][14] | **no** — xAI cloud only (since 2026-09-16) [7] | **no** — cloud voice providers only (since 2026-09-16) [21] | **no** — a terminal session is not an MC agent — its own CLI setup decides (since 2026-11-01) [11] |
| Runtime switch in MC (same harness) | **yes** — C: any cli-bridge agent; H: in place through the host adapter [22][23] | **yes** — C and H, same mechanism [22][23] | **yes** — C and H, same mechanism [22][23] | **no** — one runtime type only (`kimi-cloud`) — nothing to switch to (since 2026-07-24) [5] | **yes** — in place through the host adapter (single host session) [23][24] | **no** — one runtime only (`grok-cloud`) — nothing to switch to (since 2026-09-16) [8] | **yes** — provider switch (OpenAI ↔ xAI) in place; takes effect on the next call [25][22] | **no** — a terminal session is not an MC agent — no runtime binding (since 2026-11-01) [11] |
| Harness change in a switch (cross-image, ADR-086) | **partial** — C: yes (image change → container recreate); H: the in-place path keeps the old harness's adapter — unproven [26][27][24] | **partial** — C: yes (image change → container recreate); H: the in-place path keeps the old harness's adapter — unproven [26][27][24] | **partial** — C: yes (image change → container recreate); H: the in-place path keeps the old harness's adapter — unproven [26][27][24] | **partial** — C: yes (image change → container recreate); H: the in-place path keeps the old harness's adapter — unproven [26][27][24] | **no** — host-only singleton bridge; no image to change to (since 2026-09-16) [28] | **no** — host-only singleton bridge; no image to change to (since 2026-09-16) [28] | **no** — a voice runtime is refused for every other harness and vice versa (since 2026-09-16) [29] | **no** — a terminal session is not an MC agent — no runtime binding (since 2026-11-01) [11] |

## Task loop

| Capability | Claude Code | OpenClaude | omp | Kimi Code | Hermes | Grok Build | Voice (realtime) | Terminal session (guest card) |
|---|---|---|---|---|---|---|---|---|
| Task prompt reaches the turn boundary (dev ring `dispatch-prompt`) | **yes** — C: yes · H: yes [30][31][32] | **open** — C: open · H: yes [33][34] | **yes** — C: yes · H: yes [35][36][33][37] | **yes** — C: yes · H: yes [38][31][39] | **yes** — H: yes [40][41] | **yes** — H: yes [42] | **no** — voice worker, no task loop — not in the dev ring (since 2026-09-16) [25] | **no** — a terminal session is not an MC agent — it takes no tasks (since 2026-11-01) [11] |
| Ack: backend sees in_progress (dev ring `ack-transition`) | **yes** — C: yes · H: yes [43] | **yes** — C: yes · H: yes [43] | **yes** — C: yes · H: yes [43] | **yes** — C: yes · H: yes [43] | **yes** — H: yes [43] | **yes** — H: yes [43] | **no** — voice worker, no task loop — not in the dev ring (since 2026-09-16) [25] | **no** — a terminal session is not an MC agent — it takes no tasks (since 2026-11-01) [11] |
| Heartbeat carries context % (dev ring `heartbeat-context`) | **yes** — C: yes · H: yes [44][32] | **open** — C: open · H: open | **open** — C: yes · H: open [45] | **open** — C: open · H: open | **open** — H: open | **open** — H: open | **no** — voice worker, no task loop — not in the dev ring (since 2026-09-16) [25] | **no** — a terminal session is not an MC agent — it takes no tasks (since 2026-11-01) [11] |
| `mc` CLI in reach (which image bakes it) | **yes** — C: mc-claude-agent; H: installer symlinks `~/.local/bin/mc` [46][47] | **partial** — C: mc-agent-base; H: relies on the shared host symlink — unproven for an openclaude host agent [48] | **partial** — C: mc-omp-agent (live in the container); H: relies on the shared host symlink — unproven [49][50] | **partial** — C: mc-kimi-agent; H: relies on the shared host symlink — unproven [51] | **no** — no `mc` CLI on its PATH — hermes reaches MC only through the mc-mcp tools (since 2026-07-23) [52] | **open** — the bridge says the agent drives a 'copied mc CLI', but no installer for the grok host was found [53] | **no** — uses its own MC client package, not the CLI (since 2026-09-16) [54] | **partial** — the operator puts `scripts/mc-cli/mc` on PATH by hand (setup step 3) [55] |
| Finish with reflection (dev ring `cli-finish`) | **yes** — C: yes · H: yes [46][47] | **yes** — C: yes · H: yes [48][47] | **yes** — C: yes · H: yes [49][50][47] | **yes** — C: yes · H: yes [51][47] | **yes** — H: yes [52][56] | **yes** — H: yes [53][47] | **no** — voice worker, no task loop — not in the dev ring (since 2026-09-16) [25] | **no** — a terminal session is not an MC agent — it takes no tasks (since 2026-11-01) [11] |
| MC tools over MCP (mc-mcp) | **no** — no mc-mcp entry is rendered for claude agents (MCP servers come from the per-agent registry) (since 2026-11-02) [57] | **no** — same as claude — no mc-mcp entry rendered (since 2026-11-02) [57] | **no** — omp does not use mc-mcp; it talks to MC through the `mc` CLI and the bridge (since 2026-11-02) [58] | **open** — no kimi MCP config for mc-mcp found | **yes** — the config patcher registers `mcp_servers.mc` in agent mode [59][60] | **open** — no mc-mcp reference in the grok bridge | **no** — not a tool-using CLI (since 2026-09-16) [25] | **partial** — mc-mcp switches to agent mode on the session file; the operator registers it by hand [61] |
| Offered as a head (one job, own worktree) | **yes** [62] | **partial** — offered, but a head run is unproven [62][63] | **yes** — the default head harness [62] | **no** — unattended only with `--yolo` (a permission bypass, forbidden) (since 2026-09-24) [62] | **no** — singleton host bridge, not a one-shot process (since 2026-09-24) [62] | **no** — xAI cloud only, `-p` banned (since 2026-09-24) [62] | **no** — voice worker, no task loop — not a coding harness (since 2026-09-16) [25] | **no** — a terminal session is not an MC agent — it is the operator's own session (since 2026-11-01) [11] |

## MC chat (ADR-090 remote control)

| Capability | Claude Code | OpenClaude | omp | Kimi Code | Hermes | Grok Build | Voice (realtime) | Terminal session (guest card) |
|---|---|---|---|---|---|---|---|---|
| Chat works (send from MC, see the reply) | **partial** — C: tmux input into any cli-bridge agent; H: only the lead agent's session (slug list, not harness) [64][65] | **partial** — C only (claude parser with the openclaude process); H: no input channel [66][67] | **partial** — C: headless ACP chat; H: no input channel (ACP needs cli-bridge) [68][69] | **no** — input reaches the container TUI, but there is no transcript adapter — the chat shows nothing (since 2026-11-02) [70] | **partial** — only with `HERMES_DRIVER=acp` (hermes-bridge holds the ACP daemon) [71][72] | **no** — no transcript adapter and no chat input channel (since 2026-11-02) [64][67] | **no** — not in the sessions chat; voice has its own channel (ADR-091) (since 2026-09-16) [67] | **no** — MC chat features for coding-CLI sessions are parked; the vendor's remote control covers the phone (since 2026-11-01) [11] |
| Thinking shown | **no** — Claude Code writes thinking encrypted (empty text) — the parser skips it (since 2026-09-01) [73] | **open** — same parser as claude; never measured for openclaude | **yes** — live 'Thinking' preview plus a capped thought in the transcript [74][75] | **no** — input reaches the container TUI, but there is no transcript adapter — the chat shows nothing (since 2026-11-02) [70] | **open** — shares the omp parser; not measured on a hermes transcript [72] | **no** — no transcript adapter and no chat input channel (since 2026-11-02) [64][67] | **no** — not in the sessions chat; voice has its own channel (ADR-091) (since 2026-09-16) [67] | **no** — MC chat features for coding-CLI sessions are parked; the vendor's remote control covers the phone (since 2026-11-01) [11] |
| Tool calls shown | **yes** — tool events with edit stats [76][77] | **open** — same parser as claude; never measured for openclaude | **yes** — live tool output, per-edit diffs, file chips (P1) [78][79] | **no** — input reaches the container TUI, but there is no transcript adapter — the chat shows nothing (since 2026-11-02) [70] | **open** — shares the omp parser; not measured on a hermes transcript | **no** — no transcript adapter and no chat input channel (since 2026-11-02) [64][67] | **no** — not in the sessions chat; voice has its own channel (ADR-091) (since 2026-09-16) [67] | **no** — MC chat features for coding-CLI sessions are parked; the vendor's remote control covers the phone (since 2026-11-01) [11] |
| Tool arguments stream while generated | **open** — the tail reads finished transcript lines; no streaming path found | **open** — same as claude | **no** — arguments are taken once, when the tool call starts (since 2026-11-02) [79] | **no** — input reaches the container TUI, but there is no transcript adapter — the chat shows nothing (since 2026-11-02) [70] | **open** — shares the omp mapper; not measured | **no** — no transcript adapter and no chat input channel (since 2026-11-02) [64][67] | **no** — not in the sessions chat; voice has its own channel (ADR-091) (since 2026-09-16) [67] | **no** — MC chat features for coding-CLI sessions are parked; the vendor's remote control covers the phone (since 2026-11-01) [11] |
| Images / attachments into the chat | **open** — attachments arrive as a `[Anhang: <path>]` text line; whether the model sees the image is unmeasured | **open** — same as claude | **open** — same text line; ADR-090 lists 'send an image' (P5) as a package, not found built [80] | **no** — input reaches the container TUI, but there is no transcript adapter — the chat shows nothing (since 2026-11-02) [70] | **open** — same as omp | **no** — no transcript adapter and no chat input channel (since 2026-11-02) [64][67] | **no** — not in the sessions chat; voice has its own channel (ADR-091) (since 2026-09-16) [67] | **no** — MC chat features for coding-CLI sessions are parked; the vendor's remote control covers the phone (since 2026-11-01) [11] |
| Session resume after restart | **open** — no resume-id logic found for the tmux path | **open** — same as claude | **yes** — `session/load` of the stored id; on failure a new session and a reset card [81][82] | **no** — input reaches the container TUI, but there is no transcript adapter — the chat shows nothing (since 2026-11-02) [70] | **open** — the spec names one contract for omp and hermes; hermes resume not read [69] | **no** — no transcript adapter and no chat input channel (since 2026-11-02) [64][67] | **no** — not in the sessions chat; voice has its own channel (ADR-091) (since 2026-09-16) [67] | **no** — MC chat features for coding-CLI sessions are parked; the vendor's remote control covers the phone (since 2026-11-01) [11] |
| Own phone app / remote control outside MC | **yes** — the vendor ships a remote control from the phone [83] | **open** — no mention in the repo | **no** — no app of its own — the MC chat is its remote control (since 2026-11-01) [83][11] | **open** — no mention in the repo | **partial** — beta app; MC chat stays until it is stable [11][84] | **open** — no mention in the repo | **no** — the voice concierge is reached through MC (since 2026-09-16) [25] | **yes** — a Claude Code terminal session: the vendor's remote control [83] |

## Cross-harness comms (ADR-100)

| Capability | Claude Code | OpenClaude | omp | Kimi Code | Hermes | Grok Build | Voice (realtime) | Terminal session (guest card) |
|---|---|---|---|---|---|---|---|---|
| `comms:talk` usable | **yes** — explicit per-agent grant, harness-agnostic (an empty scope list does not grant it) [85][86] | **yes** — explicit per-agent grant, harness-agnostic (an empty scope list does not grant it) [85][86] | **yes** — explicit per-agent grant, harness-agnostic (an empty scope list does not grant it) [85][86] | **yes** — explicit per-agent grant, harness-agnostic (an empty scope list does not grant it) [85][86] | **yes** — explicit per-agent grant, harness-agnostic (an empty scope list does not grant it) [85][86] | **yes** — explicit per-agent grant, harness-agnostic (an empty scope list does not grant it) [85][86] | **open** — the grant is per agent and would work in principle; never exercised for the voice agent [85] | **yes** — the session token carries only `comms:talk` [87] |
| `mc who` / `mc send` in the docs it reads | **no** — CARD.md does not list who/send (CARD_VERBS); only `mc docs tasks` / `mc help` show them (since 2026-11-02) [88] | **no** — CARD.md does not list who/send (CARD_VERBS); only `mc docs tasks` / `mc help` show them (since 2026-11-02) [88] | **no** — CARD.md does not list who/send (CARD_VERBS); only `mc docs tasks` / `mc help` show them (since 2026-11-02) [88] | **no** — CARD.md does not list who/send (CARD_VERBS); only `mc docs tasks` / `mc help` show them (since 2026-11-02) [88] | **yes** — the `mc_who` / `mc_send` MCP tools carry their own docstrings [89][90] | **no** — CARD.md does not list who/send (CARD_VERBS); only `mc docs tasks` / `mc help` show them (since 2026-11-02) [88] | **no** — voice worker, no task loop — not wired (since 2026-09-16) [25] | **yes** — setup and verbs in the hook README [91] |
| Inbox delivery (nudge + pull) | **yes** — poll.sh pastes one wake-up line; the agent pulls with `mc inbox` [92] | **yes** — shared poll.sh [92] | **yes** — bridge nudge + pull (ADR-071 port) [93] | **yes** — shared poll.sh, nudge mode [92][39] | **yes** — bridge nudge pointing at the `mc_inbox` MCP tool [94] | **yes** — grok-bridge nudge + pull [95] | **open** — no inbox path for the voice agent found | **partial** — a one-line notice only; reading is opt-in (`MC_COMMS_AUTO_READ=1`) [96] |
| Listed in `mc who` | **yes** — from its agent row once `comms:talk` is granted [85][97] | **yes** — from its agent row once `comms:talk` is granted [85][97] | **yes** — from its agent row once `comms:talk` is granted [85][97] | **yes** — from its agent row once `comms:talk` is granted [85][97] | **yes** — from its agent row once `comms:talk` is granted [85][97] | **yes** — from its agent row once `comms:talk` is granted [85][97] | **open** — would follow from a grant; never exercised | **yes** — enrolled by `mc hello` from the session-start hook [98] |
| Auto-enrol hook | **no** — agents need no hook: the agent row is the directory entry (since 2026-11-01) [85] | **no** — agents need no hook: the agent row is the directory entry (since 2026-11-01) [85] | **no** — agents need no hook: the agent row is the directory entry (since 2026-11-01) [85] | **no** — agents need no hook: the agent row is the directory entry (since 2026-11-01) [85] | **no** — agents need no hook: the agent row is the directory entry (since 2026-11-01) [85] | **no** — agents need no hook: the agent row is the directory entry (since 2026-11-01) [85] | **no** — voice worker, no task loop — no directory hook (since 2026-09-16) [25] | **yes** — SessionStart / UserPromptSubmit / Stop / SessionEnd hooks [99] |
| Busy / idle presence | **yes** — derived from agent liveness and run state — one presence source [97] | **yes** — derived from agent liveness and run state — one presence source [97] | **yes** — derived from agent liveness and run state — one presence source [97] | **yes** — derived from agent liveness and run state — one presence source [97] | **yes** — derived from agent liveness and run state — one presence source [97] | **yes** — derived from agent liveness and run state — one presence source [97] | **open** — would follow from agent liveness; not exercised | **yes** — the hook reports busy on prompt and idle on stop [100][101] |
| Box status (`mc box status`) | **yes** — `mc box` verb (scope tasks:read) [102] | **yes** — `mc box` verb in the image [102][48] | **yes** — `mc box` verb, live in the container [102][50] | **yes** — `mc box` verb in the image [102][51] | **yes** — `mc_box_status` MCP tool [103] | **open** — depends on the `mc` CLI on the grok host, which is unproven | **no** — voice worker, no task loop — not wired (since 2026-09-16) [25] | **open** — box status needs tasks:read; a session token carries only comms:talk — not measured |

## Extensions

| Capability | Claude Code | OpenClaude | omp | Kimi Code | Hermes | Grok Build | Voice (realtime) | Terminal session (guest card) |
|---|---|---|---|---|---|---|---|---|
| Claude-Code skills / plugins managed by MC | **yes** [104] | **yes** [105] | **no** — own config surfaces; MC plugin/skill files are ignored (since 2026-09-16) [106] | **no** — own config surfaces (since 2026-09-16) [107] | **no** — host-only, all capabilities off (since 2026-09-16) [108] | **no** — host-only, all capabilities off (since 2026-09-16) [108] | **no** — not in HARNESS_CAPABILITIES; falls to the all-off row (since 2026-09-16) [109] | **no** — a terminal session is not an MC agent — the operator's own CLI setup (since 2026-11-01) [11] |
| Subagents on the agent's own model | **open** — native in the CLI; MC neither configures nor measures them | **open** — native in the CLI; not measured | **yes** — the config renderer pins subagent roles (task, scout, reviewer) to the bound model [110] | **open** — not measured | **open** — not measured | **open** — not measured | **no** — voice worker, no task loop — no subagents (since 2026-09-16) [25] | **open** — the operator's own CLI; not MC's concern |
| Turn hooks wired by MC | **yes** — UserPromptSubmit / Stop turn-signal hooks + statusLine in settings.json [111] | **partial** — only when the bound runtime speaks the anthropic protocol [112] | **yes** — guard hook (bash lesson guards) and turn-end hook [113][114] | **yes** — `[[hooks]]` UserPromptSubmit / Stop in config.toml [115] | **open** — none found | **open** — none found | **no** — voice worker, no task loop — no hooks (since 2026-09-16) [25] | **yes** — the comms hook in the operator's own settings [99] |

## Operations

| Capability | Claude Code | OpenClaude | omp | Kimi Code | Hermes | Grok Build | Voice (realtime) | Terminal session (guest card) |
|---|---|---|---|---|---|---|---|---|
| Provisioning path | **yes** — C: cli-bridge provision; H: generic staged host path (plist + run.sh + agent.env + poll.sh) [116][117] | **yes** — same as claude [116][117] | **yes** — same as claude; host omp also gets its own models.yml profile [117][118] | **yes** — C: cli-bridge provision; H: adapter bootstrap [119][120] | **yes** — adapter bootstrap (singleton) [121][122] | **yes** — adapter bootstrap (singleton) [123][124] | **no** — owned by the voice-worker compose service; bootstrap refuses (since 2026-09-16) [25] | **yes** — an admin issues a guest card; the hook enrols the session [125] |
| Config sync (`sync-config`) | **yes** — C: files + optional container restart; H: files only, no restart [126] | **yes** — C: files + optional container restart; H: files only, no restart [126] | **yes** — C: files + optional container restart; H: files only, no restart [126] | **yes** — C: files + optional container restart; H: files only, no restart [126] | **yes** — host files only, no restart [126] | **yes** — host files only, no restart [126] | **no** — nothing to sync; the binding is read per call (since 2026-09-16) [25] | **no** — a terminal session is not an MC agent — nothing to sync (since 2026-11-01) [11] |
| Token / auth model | **yes** — Claude account OAuth token from the vault (`CLAUDE_CODE_OAUTH_TOKEN`) [127] | **yes** — `OPENAI_API_KEY`: agent secret first, then runtime secret; keyless local runtimes get none [128] | **yes** — same as openclaude [128] | **yes** — own OAuth credential files in the per-agent config mount; no env token [129] | **yes** — same as openclaude [128] | **yes** — its own OAuth session; MC renders only control-plane vars [7] | **yes** — voice-worker env, or the vault via `/agent/voice/credentials` [130] | **yes** — guest card (≤ 7 days, mode 0600) → per-session token [131] |
| Pinned CLI version | **yes** 2.1.308 [132] | **yes** 0.30.0 [133] | **yes** 18.10.1 — with sha256 [134] | **yes** 0.29.1 — with sha256 [135] | **no** — not in cli-versions.json (since 2026-11-02) [136] | **yes** 0.2.93 [137] | **no** — voice worker, no task loop — no CLI (since 2026-09-16) [25] | **no** — a terminal session is not an MC agent — the operator's own CLI (since 2026-11-01) [11] |
| Binary on the host PATH (live) | **yes** 2.1.316 [138] | **partial** 0.1.8 — far behind the pin (0.30.0) [139] | **partial** 16.2.13 — host binary behind the pin; the container runs 18.10.1 [140] | **partial** 0.27.0 — behind the pin (0.29.1) [141] | **yes** v0.18.0 [142] | **yes** 0.2.93 — matches the pin [143] | **no** — voice worker, no task loop — no CLI (since 2026-09-16) [25] | **no** — a terminal session is not an MC agent — the operator's own CLI (since 2026-11-01) [11] |
| Pane fixtures in the adapter TCK | **yes** [144] | **yes** [145] | **no** — container omp runs headless over ACP — no pane to scrape (since 2026-11-02) [146] | **yes** [147] | **open** — no fixtures; the hermes TUI path greps its own paste | **open** — no fixtures; the grok bridge scrapes its pane without a TCK | **no** — voice worker, no task loop — no pane (since 2026-09-16) [25] | **no** — a terminal session is not an MC agent — not MC-driven (since 2026-11-01) [11] |
| Known limits | **partial** — fleet matrix refuses local runtimes (heads only, experimental); host chat bound to the lead agent's slug [148][67] | **partial** — turn-signal hooks and statusLine only on anthropic-protocol runtimes [112] | **partial** — stream idle timeout raised to 1 h (engines that hold tool arguments send nothing for minutes); cannot boot without a runtime binding [149][15] | **partial** — OAuth refresh rotation kills copied credential files; unattended only with `--yolo` [4][150] | **partial** — the only harness reading the thinking switch; ACP chat only behind `HERMES_DRIVER=acp` [151][152] | **partial** — `-p` banned fleet-wide; one persistent TUI per host [53] | **partial** — realtime voice only; one provider per call [21] | **partial** — one card enrols at most 5 live sessions sharing one rate limit; a second terminal in the same folder stays out [153][154] |
| Live agents on this harness | **yes** — 1 active host agent (the lead) [155] | **no** — no agent (since 2026-11-02) [155] | **yes** — 1 active container agent [155] | **no** — no agent (since 2026-11-02) [155] | **no** — 1 agent, paused (since 2026-09-22) [155] | **no** — 1 agent, paused (since 2026-09-22) [155] | **yes** — 1 active voice agent [155] | **open** — guest sessions are not agent rows; not counted |

## Maturity levels

- `stable` — in daily use and covered by tests; changes need the normal gates
- `beta` — shipped and working, not yet proven in daily use
- `experimental` — wired in code, never proven end to end
- `frozen` — kept and guarded, no new features (quiet mode, ADR-085) — or on its way out

## How to change this

1. Edit `docs/harness-matrix.json` (a new harness: add a column, a cell in every row, and — for an agent harness — its dev-ring rows).
2. Every `yes` / `partial` cell cites `path:line` or `live:YYYY-MM-DD <what was measured>`; every `no` carries `since` + `recheck`; unknown is `open`, never a guess.
3. Run `python3 tools/render-harness-matrix.py` and commit both files. `backend/tests/test_harness_matrix.py` fails until you do.

## Evidence

1. [`backend/app/services/harness_compat.py:75`](../backend/app/services/harness_compat.py#L75)
2. [`backend/app/services/harness_compat.py:76`](../backend/app/services/harness_compat.py#L76)
3. [`backend/app/services/harness_compat.py:77`](../backend/app/services/harness_compat.py#L77)
4. [`backend/app/services/harness_compat.py:65-73`](../backend/app/services/harness_compat.py#L65-L73)
5. [`backend/app/services/harness_compat.py:80`](../backend/app/services/harness_compat.py#L80)
6. [`backend/app/services/harness_compat.py:78`](../backend/app/services/harness_compat.py#L78)
7. [`backend/app/services/harness_compat.py:59-64`](../backend/app/services/harness_compat.py#L59-L64)
8. [`backend/app/services/harness_compat.py:79`](../backend/app/services/harness_compat.py#L79)
9. [`backend/app/services/harness_compat.py:47-50`](../backend/app/services/harness_compat.py#L47-L50)
10. [`backend/app/services/harness_compat.py:87`](../backend/app/services/harness_compat.py#L87)
11. [`docs/decisions/100-foundation-and-cross-harness-comms.md:97-104`](../docs/decisions/100-foundation-and-cross-harness-comms.md#L97-L104)
12. [`backend/app/services/harness_compat.py:113-116`](../backend/app/services/harness_compat.py#L113-L116)
13. [`backend/app/services/harness_compat.py:36-41`](../backend/app/services/harness_compat.py#L36-L41)
14. [`backend/app/services/harness_compat.py:132-133`](../backend/app/services/harness_compat.py#L132-L133)
15. [`backend/app/services/harness_compat.py:247-258`](../backend/app/services/harness_compat.py#L247-L258)
16. [`backend/app/services/harness_compat.py:122-125`](../backend/app/services/harness_compat.py#L122-L125)
17. [`backend/app/services/harness_compat.py:117-121`](../backend/app/services/harness_compat.py#L117-L121)
18. [`backend/app/services/harness_compat.py:126-131`](../backend/app/services/harness_compat.py#L126-L131)
19. [`docs/specs/head-launcher.md:124`](../docs/specs/head-launcher.md#L124)
20. [`docs/specs/head-launcher.md:122`](../docs/specs/head-launcher.md#L122)
21. [`backend/app/services/harness_compat.py:43-50`](../backend/app/services/harness_compat.py#L43-L50)
22. [`backend/app/services/host_harness_adapter.py:497-511`](../backend/app/services/host_harness_adapter.py#L497-L511)
23. [`backend/app/services/host_harness_adapter.py:484-494`](../backend/app/services/host_harness_adapter.py#L484-L494)
24. [`backend/app/services/agent_runtime_switch.py:876-890`](../backend/app/services/agent_runtime_switch.py#L876-L890)
25. [`backend/app/services/host_harness_adapter.py:363-375`](../backend/app/services/host_harness_adapter.py#L363-L375)
26. [`backend/app/services/agent_runtime_switch.py:793`](../backend/app/services/agent_runtime_switch.py#L793)
27. [`backend/app/services/agent_runtime_switch.py:841-846`](../backend/app/services/agent_runtime_switch.py#L841-L846)
28. [`backend/app/services/harness_compat.py:52-57`](../backend/app/services/harness_compat.py#L52-L57)
29. [`backend/app/services/agent_runtime_switch.py:802-808`](../backend/app/services/agent_runtime_switch.py#L802-L808)
30. [`docker/mc-claude-agent/entrypoint.sh:253-255`](../docker/mc-claude-agent/entrypoint.sh#L253-L255)
31. [`docker/shared/poll.sh:254-256`](../docker/shared/poll.sh#L254-L256)
32. [`docker/boss-host/entrypoint.sh:6`](../docker/boss-host/entrypoint.sh#L6)
33. [`backend/app/services/host_harness_adapter.py:263-270`](../backend/app/services/host_harness_adapter.py#L263-L270)
34. [`backend/app/services/host_harness_adapter.py:326-338`](../backend/app/services/host_harness_adapter.py#L326-L338)
35. [`docker/omp-bridge/bridge.py:1204`](../docker/omp-bridge/bridge.py#L1204)
36. [`docker/omp-bridge/bridge.py:3488`](../docker/omp-bridge/bridge.py#L3488)
37. [`backend/app/services/host_harness_adapter.py:340-362`](../backend/app/services/host_harness_adapter.py#L340-L362)
38. [`docker/mc-kimi-agent/entrypoint.sh:188`](../docker/mc-kimi-agent/entrypoint.sh#L188)
39. [`docker/kimi-host/entrypoint.sh:113-115`](../docker/kimi-host/entrypoint.sh#L113-L115)
40. [`scripts/hermes-bridge.py:444`](../scripts/hermes-bridge.py#L444)
41. [`scripts/hermes-bridge.py:509`](../scripts/hermes-bridge.py#L509)
42. [`scripts/grok-bridge.py:405`](../scripts/grok-bridge.py#L405)
43. [`backend/app/routers/agent_scoped.py:333-337`](../backend/app/routers/agent_scoped.py#L333-L337)
44. [`docker/shared/poll.sh:733-744`](../docker/shared/poll.sh#L733-L744)
45. [`docker/omp-bridge/bridge.py:2161-2196`](../docker/omp-bridge/bridge.py#L2161-L2196)
46. [`docker/mc-claude-agent/Dockerfile:164-167`](../docker/mc-claude-agent/Dockerfile#L164-L167)
47. [`scripts/install-boss-host.sh:127-141`](../scripts/install-boss-host.sh#L127-L141)
48. [`docker/mc-agent-base/Dockerfile:128-131`](../docker/mc-agent-base/Dockerfile#L128-L131)
49. [`docker/omp-bridge/Dockerfile:150-152`](../docker/omp-bridge/Dockerfile#L150-L152)
50. live measurement — 2026-11-02 omp container: `mc` on PATH, verbs include finish/hello/who/send/box
51. [`docker/mc-kimi-agent/Dockerfile:123-126`](../docker/mc-kimi-agent/Dockerfile#L123-L126)
52. [`scripts/hermes-bridge.py:958-963`](../scripts/hermes-bridge.py#L958-L963)
53. [`scripts/grok-bridge.py:5-14`](../scripts/grok-bridge.py#L5-L14)
54. [`voice_worker/Dockerfile:46-49`](../voice_worker/Dockerfile#L46-L49)
55. [`scripts/comms/claude-code/README.md:14-15`](../scripts/comms/claude-code/README.md#L14-L15)
56. [`scripts/mc-mcp.py:444-460`](../scripts/mc-mcp.py#L444-L460)
57. [`backend/app/services/mcp_sync.py:39-66`](../backend/app/services/mcp_sync.py#L39-L66)
58. [`docs/specs/chat-over-acp.md:604`](../docs/specs/chat-over-acp.md#L604)
59. [`scripts/hermes-config-patch.py:76-84`](../scripts/hermes-config-patch.py#L76-L84)
60. [`scripts/mc-mcp.py:1-16`](../scripts/mc-mcp.py#L1-L16)
61. [`scripts/mc-mcp.py:36-48`](../scripts/mc-mcp.py#L36-L48)
62. [`backend/app/services/heads/pairs.py:27-29`](../backend/app/services/heads/pairs.py#L27-L29)
63. [`docs/specs/head-launcher.md:123`](../docs/specs/head-launcher.md#L123)
64. [`backend/app/services/transcript_adapters.py:343-353`](../backend/app/services/transcript_adapters.py#L343-L353)
65. [`backend/app/services/agent_chat_input.py:635-656`](../backend/app/services/agent_chat_input.py#L635-L656)
66. [`backend/app/services/transcript_adapters.py:345`](../backend/app/services/transcript_adapters.py#L345)
67. [`backend/app/services/agent_chat_input.py:651-656`](../backend/app/services/agent_chat_input.py#L651-L656)
68. [`backend/app/services/acp_chat_transport.py:186-195`](../backend/app/services/acp_chat_transport.py#L186-L195)
69. [`docs/specs/chat-over-acp.md:16`](../docs/specs/chat-over-acp.md#L16)
70. [`backend/app/services/transcript_adapters.py:366-376`](../backend/app/services/transcript_adapters.py#L366-L376)
71. [`backend/app/services/acp_chat_transport.py:190-195`](../backend/app/services/acp_chat_transport.py#L190-L195)
72. [`backend/app/services/transcript_adapters.py:346-352`](../backend/app/services/transcript_adapters.py#L346-L352)
73. [`backend/app/services/transcript_chat.py:643-660`](../backend/app/services/transcript_chat.py#L643-L660)
74. [`backend/app/services/transcript_chat.py:1886-1889`](../backend/app/services/transcript_chat.py#L1886-L1889)
75. [`docker/omp-bridge/acp_chat_events.py:176-179`](../docker/omp-bridge/acp_chat_events.py#L176-L179)
76. [`backend/app/services/transcript_chat.py:1702-1703`](../backend/app/services/transcript_chat.py#L1702-L1703)
77. [`backend/app/services/transcript_chat.py:1829`](../backend/app/services/transcript_chat.py#L1829)
78. [`docs/decisions/090-chat-as-remote-control.md:64-68`](../docs/decisions/090-chat-as-remote-control.md#L64-L68)
79. [`docker/omp-bridge/acp_chat_events.py:1457-1470`](../docker/omp-bridge/acp_chat_events.py#L1457-L1470)
80. [`docs/decisions/090-chat-as-remote-control.md:88`](../docs/decisions/090-chat-as-remote-control.md#L88)
81. [`docs/specs/chat-over-acp.md:228-231`](../docs/specs/chat-over-acp.md#L228-L231)
82. [`docker/omp-bridge/acp_chat.py:538`](../docker/omp-bridge/acp_chat.py#L538)
83. [`docs/decisions/100-foundation-and-cross-harness-comms.md:44-48`](../docs/decisions/100-foundation-and-cross-harness-comms.md#L44-L48)
84. [`docs/decisions/100-foundation-and-cross-harness-comms.md:164`](../docs/decisions/100-foundation-and-cross-harness-comms.md#L164)
85. [`backend/app/services/comms_directory.py:79-84`](../backend/app/services/comms_directory.py#L79-L84)
86. [`backend/app/comms_auth.py:54-60`](../backend/app/comms_auth.py#L54-L60)
87. [`backend/app/routers/agent_comms.py:97-111`](../backend/app/routers/agent_comms.py#L97-L111)
88. [`backend/app/agent_doc_constants.py:210-214`](../backend/app/agent_doc_constants.py#L210-L214)
89. [`scripts/mc-mcp.py:1038-1039`](../scripts/mc-mcp.py#L1038-L1039)
90. [`scripts/mc-mcp.py:1064-1065`](../scripts/mc-mcp.py#L1064-L1065)
91. [`scripts/comms/claude-code/README.md:1-30`](../scripts/comms/claude-code/README.md#L1-L30)
92. [`docker/shared/poll.sh:146-148`](../docker/shared/poll.sh#L146-L148)
93. [`docker/omp-bridge/bridge.py:2344-2353`](../docker/omp-bridge/bridge.py#L2344-L2353)
94. [`scripts/hermes-bridge.py:958-967`](../scripts/hermes-bridge.py#L958-L967)
95. [`scripts/grok-bridge.py:141`](../scripts/grok-bridge.py#L141)
96. [`scripts/comms/claude-code/README.md:24-25`](../scripts/comms/claude-code/README.md#L24-L25)
97. [`backend/app/services/comms_directory.py:194-235`](../backend/app/services/comms_directory.py#L194-L235)
98. [`scripts/comms/claude-code/mc-comms-hook.py:122`](../scripts/comms/claude-code/mc-comms-hook.py#L122)
99. [`scripts/comms/claude-code/hooks.example.json:1-8`](../scripts/comms/claude-code/hooks.example.json#L1-L8)
100. [`scripts/comms/claude-code/mc-comms-hook.py:138`](../scripts/comms/claude-code/mc-comms-hook.py#L138)
101. [`scripts/comms/claude-code/mc-comms-hook.py:152`](../scripts/comms/claude-code/mc-comms-hook.py#L152)
102. [`scripts/mc-cli/mc_cli/commands.py:3870-3871`](../scripts/mc-cli/mc_cli/commands.py#L3870-L3871)
103. [`scripts/mc-mcp.py:975-976`](../scripts/mc-mcp.py#L975-L976)
104. [`backend/app/services/harness_compat.py:337-343`](../backend/app/services/harness_compat.py#L337-L343)
105. [`backend/app/services/harness_compat.py:344-350`](../backend/app/services/harness_compat.py#L344-L350)
106. [`backend/app/services/harness_compat.py:351-357`](../backend/app/services/harness_compat.py#L351-L357)
107. [`backend/app/services/harness_compat.py:358-364`](../backend/app/services/harness_compat.py#L358-L364)
108. [`backend/app/services/harness_compat.py:367-376`](../backend/app/services/harness_compat.py#L367-L376)
109. [`backend/app/services/harness_compat.py:379-390`](../backend/app/services/harness_compat.py#L379-L390)
110. [`docker/omp-bridge/render-omp-config.sh:311-316`](../docker/omp-bridge/render-omp-config.sh#L311-L316)
111. [`backend/templates/cli_agent_settings.json.j2:6-38`](../backend/templates/cli_agent_settings.json.j2#L6-L38)
112. [`backend/app/services/harness_compat.py:393-399`](../backend/app/services/harness_compat.py#L393-L399)
113. [`docker/omp-bridge/guard-hook.mjs:1-20`](../docker/omp-bridge/guard-hook.mjs#L1-L20)
114. [`docker/omp-bridge/turn-end-hook.mjs:1-12`](../docker/omp-bridge/turn-end-hook.mjs#L1-L12)
115. [`docker/mc-kimi-agent/entrypoint.sh:149-162`](../docker/mc-kimi-agent/entrypoint.sh#L149-L162)
116. [`backend/app/routers/agents.py:2000-2015`](../backend/app/routers/agents.py#L2000-L2015)
117. [`backend/app/routers/agents.py:2068-2076`](../backend/app/routers/agents.py#L2068-L2076)
118. [`backend/app/services/host_provisioning.py:87-90`](../backend/app/services/host_provisioning.py#L87-L90)
119. [`backend/app/services/agent_bootstrap.py:489`](../backend/app/services/agent_bootstrap.py#L489)
120. [`backend/app/services/host_harness_adapter.py:164-170`](../backend/app/services/host_harness_adapter.py#L164-L170)
121. [`backend/app/services/agent_bootstrap.py:321`](../backend/app/services/agent_bootstrap.py#L321)
122. [`backend/app/services/host_harness_adapter.py:79-85`](../backend/app/services/host_harness_adapter.py#L79-L85)
123. [`backend/app/services/agent_bootstrap.py:580`](../backend/app/services/agent_bootstrap.py#L580)
124. [`backend/app/services/host_harness_adapter.py:123-129`](../backend/app/services/host_harness_adapter.py#L123-L129)
125. [`scripts/comms/claude-code/README.md:9-17`](../scripts/comms/claude-code/README.md#L9-L17)
126. [`backend/app/routers/agents.py:2165-2176`](../backend/app/routers/agents.py#L2165-L2176)
127. [`backend/app/services/harness_compat.py:195-198`](../backend/app/services/harness_compat.py#L195-L198)
128. [`backend/app/services/harness_compat.py:200-214`](../backend/app/services/harness_compat.py#L200-L214)
129. [`backend/app/routers/internal.py:336-342`](../backend/app/routers/internal.py#L336-L342)
130. [`backend/app/services/harness_compat.py:81-87`](../backend/app/services/harness_compat.py#L81-L87)
131. [`scripts/comms/claude-code/README.md:9-13`](../scripts/comms/claude-code/README.md#L9-L13)
132. [`docker/cli-versions.json:5-7`](../docker/cli-versions.json#L5-L7)
133. [`docker/cli-versions.json:2-4`](../docker/cli-versions.json#L2-L4)
134. [`docker/cli-versions.json:8-11`](../docker/cli-versions.json#L8-L11)
135. [`docker/cli-versions.json:12-15`](../docker/cli-versions.json#L12-L15)
136. [`docker/cli-versions.json:1-19`](../docker/cli-versions.json#L1-L19)
137. [`docker/cli-versions.json:16-18`](../docker/cli-versions.json#L16-L18)
138. live measurement — 2026-11-02 `claude --version` on the host → 2.1.316
139. live measurement — 2026-11-02 `openclaude --version` on the host → 0.1.8
140. live measurement — 2026-11-02 `omp --version` on the host → 16.2.13; in the omp container → 18.10.1
141. live measurement — 2026-11-02 `kimi --version` on the host → 0.27.0
142. live measurement — 2026-11-02 `hermes --version` on the host → v0.18.0
143. live measurement — 2026-11-02 `grok --version` on the host → 0.2.93
144. [`backend/tests/fixtures/panes/claude`](../backend/tests/fixtures/panes/claude)
145. [`backend/tests/fixtures/panes/openclaude`](../backend/tests/fixtures/panes/openclaude)
146. [`backend/app/services/acp_chat_transport.py:186-188`](../backend/app/services/acp_chat_transport.py#L186-L188)
147. [`backend/tests/fixtures/panes/kimi`](../backend/tests/fixtures/panes/kimi)
148. [`backend/app/services/harness_compat.py:144-155`](../backend/app/services/harness_compat.py#L144-L155)
149. [`backend/app/services/harness_compat.py:281-288`](../backend/app/services/harness_compat.py#L281-L288)
150. [`docs/specs/head-launcher.md:125`](../docs/specs/head-launcher.md#L125)
151. [`backend/app/services/harness_compat.py:236-244`](../backend/app/services/harness_compat.py#L236-L244)
152. [`backend/app/services/acp_chat_transport.py:190-191`](../backend/app/services/acp_chat_transport.py#L190-L191)
153. [`scripts/comms/claude-code/README.md:11-13`](../scripts/comms/claude-code/README.md#L11-L13)
154. [`scripts/comms/claude-code/README.md:28-30`](../scripts/comms/claude-code/README.md#L28-L30)
155. live measurement — 2026-11-02 agents table (read-only query): active = 1 claude host agent, 1 omp container agent, 1 jarvis; paused = 1 grok, 1 hermes; no kimi or openclaude agent
