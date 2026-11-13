# ADR-101 — A registered agent works its cards over MCP (worker flow)

**Status:** Accepted (2026-11-12) · amends PRINCIPLES §8 rule 3 (second exception) · builds on ADR-100 §1 and §2 · touches ADR-034, ADR-037, ADR-085
**Datum:** 2026-11-12
**Scope:** scripts/mc-mcp · scripts/mc-cli (reused, unchanged) · Architecture/Direction

## Kontext

ADR-100 §1 says every harness connects to MC "through the `mc` command line
or an MCP tool". For the task board only the first half exists. A harness
works cards today in one of two ways: it has a bridge written for it (a poll
loop that fetches the card, starts a turn and sends heartbeats), or its model
runs the `mc` command in a shell. A harness without a bridge and without the
`mc` command on its machine cannot work a card at all — even though most
harnesses can load an MCP server with one config entry.

What was established before this decision (checked against `origin/main` on
2026-11-12):

- **`scripts/mc-mcp.py` already has an agent mode** (ADR-085 §4): with an
  agent token it uses only `/agent/...` routes and registers the tools a
  token can serve (task detail, checklist, deliverables, comms). It cannot
  fetch a card, accept it, or finish it with the pre-finish checks.
- **The agent routes already carry the whole flow.** `GET /agent/me/poll`
  hands out the next card and the dispatch attempt id; the agent accepts with
  `PATCH status: in_progress`; comments, checklist, heartbeat and the vault
  routes exist. Nothing in the backend is missing.
- **Finishing has one owner.** `mc finish` (ADR-037) checks the reflection,
  the checklist and the state before it changes a status. A second
  implementation of these checks would drift.
- **A harness connected over MCP knows nothing about MC.** It does not know
  that a card must be accepted, that the checklist counts, or that a
  reflection is required. Without instructions a model treats the tools as a
  loose toolbox.
- **Three rules stand against this** unless a decision names the exception:
  PRINCIPLES §8 rule 3 (no MCP server for heads), ADR-100 §2 ("Every other
  MCP tool for heads still needs its own ADR") and ADR-085 (the admin MCP
  tool stays an operator tool; "a run MCP server for heads" was rejected).

The operator asked for this on 2026-11-12: agents that use the MCP server
should be able to work with the task board, and should be told how MC works
so that they act accordingly — fetch cards, work on them, finish them.

## Entscheidung

**A registered agent may work its cards through `scripts/mc-mcp.py`. The
worker flow is a set of MCP tools over the existing agent routes, switched on
per process, with the rules of MC told to the model at three places. It adds
no route, no table and no second finish logic.**

### 1. Who is a worker

A worker is an agent registered in MC: it has its own row, its own token, its
own scopes and its board. It is a normal agent row of the runtime kind `host`
without a harness: MC starts nothing; the worker's own poll claims the next
card assigned to it (an inbox card whose dependencies are met), whether or not
the board has auto-dispatch on. Its runtime must be ready, or the poll hands
out no fresh card. An admin token is never handed to a worker (unchanged), and
no token is shared between two workers.

*Pause* stops a worker from getting new cards: since ADR-089 Nachtrag
2026-11-13 the agent poll holds every card for a paused agent
(`agent_paused`), and the card waits until the operator sets it active. A
card it already accepted runs on. The cut for good is resetting its token
(see "Später ändern").

`scopes = []` means all scopes in MC. A worker therefore gets an explicit
list. The flow needs `tasks:read`, `tasks:write`, `vault:read` and
`vault:write`; everything else is added only when the job needs it.

### 2. The switch

The worker tools exist only when all three hold at process start:

- agent mode (an agent token is configured),
- `MC_MCP_WORKER` is `1`, `true`, `yes` or `on`,
- the process is **not** a guest session (ADR-100 §2).

Without the switch an agent-mode process is unchanged: a harness that has its
own bridge keeps polling and sending heartbeats itself, and must not get a
second poller. A guest session gains neither the worker tools nor the vault
tools, even with the switch set; its tool list stays as it was.

### 3. The tools

| Tool | What it does | Route behind it |
|---|---|---|
| `mc_next_task` | fetch the next card and accept it; also shows new comments and messages | `GET /agent/me/poll`, `PATCH …/tasks/{id}` |
| `mc_progress` | post a progress note on the card | `POST …/tasks/{id}/comments` |
| `mc_finish` | finish the card (done or review) | `mc finish` from `scripts/mc-cli` |
| `mc_handbook` | the rules of MC for a worker, on demand | none (static text) |
| `mc_vault_search`, `mc_vault_read` | find and read notes | `/agent/vault/...` (operator: `/vault/...`) |
| `mc_vault_write` | write a note, linked to the held card | `POST /agent/vault/note` |

The existing agent-mode tools (task detail, checklist, deliverables) stay as
they are and complete the flow.

Vault search and read are also registered in operator mode and in plain agent
mode. Vault write is agent-only: a note has an author, and the operator's
admin route does not provide one.

### 4. Instructions at three places

1. **On connect:** the server's MCP `instructions` describe the loop in four
   steps and point to the handbook.
2. **On demand:** `mc_handbook` returns the full rules — a card's life,
   staying alive, the checklist, the four sections of the reflection, the
   vault, and what a worker never does.
3. **In every answer:** each tool answer ends with the next step ("call
   `mc_next_task` again in about a minute", "check the checklist with
   `mc_checklist`", …). An accepted card carries a small table that maps the
   `mc` verbs in the brief to the tool names.

The brief of a card is written for agents that have the `mc` command. The
worker gets the same brief, unchanged, between two marker lines, so that the
text from MC and the text from the server stay apart.

### 5. Accepting, re-delivery and the attempt id

`mc_next_task` accepts the card it fetched in the same call, with the
dispatch attempt id from the poll. MC re-delivers a card when a run went
silent (fresh attempt id). The server then takes over the fresh id and tells
the model that this is the same card and that it must continue, not start
over. When MC reports the worker as `working`, or after a restart of the
process, the server reads the card and its current attempt id from MC instead
of trusting its memory.

Only a card in `inbox` or `in_progress` is accepted. The poll also hands out
an assigned card in `review` or `blocked` — once, because it marks such a card
as acknowledged itself. Accepting it would pull a card out of review or past
its blocker, so the server shows it with its brief, says that it was not
accepted, and changes nothing. For the same reason `mc_finish` reads the
card's status first and finishes only a card in progress: `mc finish` itself
allows review → done, and a worker must never approve its own review card.

### 6. Staying alive

While a card is held and in progress, the server sends the agent heartbeat
(`status: working`) every 30 seconds from a background thread
(`MC_MCP_HEARTBEAT_SECONDS`, `0` = off, at most 60).

The heartbeat is a limited claim, on purpose: it stops when the model has
called no MC tool for 20 minutes (`MC_MCP_HEARTBEAT_QUIET_MINUTES`, `0` = no
limit). It therefore proves that the process is alive and that the model
talked to MC recently. It does **not** prove that the model is producing
output right now — an MCP server cannot see that. A worker that thinks for
longer than the quiet window without one tool call falls silent, and MC's
existing recovery re-delivers the card. The handbook tells the model to post
a progress note at least every 15 minutes.

One `idle` beat is sent when the card is finished or taken away.

### 7. Finishing

`mc_finish` runs `mc finish` in-process: the same function, with a small HTTP
client that follows the command's own client contract. The reflection is
checked first, before any call to MC. The adapter does not retry on a server
error — the model decides, with the answer in front of it. After the command
the server reads the card once and reports the real status; "finished" is
never said from the command's exit alone.

The `mc` command line must sit next to the server (`scripts/mc-cli`). Without
it `mc_finish` finishes nothing and says why.

### 8. PRINCIPLES §8 rule 3 — second exception

The rule gains a second exception: the worker flow of this ADR, for a
registered agent with its own token and an explicit scope list. The rest of
the rule stands — no own harness driver, no agent loop inside MC, no admin
token for a head.

### 9. Not in this decision

- **No HTTP transport with a token per request.** The server stays a local
  stdio process, one per worker, started by the harness.
- **No vault write for the operator mode.**
- **No new scope, route, column or status.** The task core stays frozen.
- **No page in the MC UI for a worker.** The tested way to create one is a
  single call to the existing `POST /agents` route (runtime kind `host`,
  explicit scopes). The plain token is in the answer, and MC also keeps it in
  the agent's TOOLS.md (readable on the agent page) and as an agent secret,
  like for every agent. The agent wizard also creates the row, but then
  provisions a host agent — a new token and service files a worker does not
  use. Cutting a worker off needs the token reset, which is an API call
  today; *pause* stops new cards (§1, ADR-089 Nachtrag 2026-11-13).
  The steps are in `docs/setup/mcp-worker.md`. A worker option in the wizard
  with the connect snippet, and a reset button, are visible changes and go to
  the operator first.
- **No change to dispatch or the poll.** The poll claims a worker's assigned
  card without a dispatch step; that is existing behaviour and stays as it is
  here (build stop). The pause gate in the poll came later, on the
  operator's decision (ADR-089 Nachtrag 2026-11-13).
- **No message tools beyond the existing comms tools.** `mc_inbox` and
  `mc_msg` are the existing agent-mode tools; only `mc blocked` has no tool.
  The verb table says so.

## Alternativen

- **A bridge per harness (the way so far):** works, and stays for the
  harnesses that have one. Rejected as the only way because every new harness
  costs a bridge, and a harness on another machine cannot use it.
- **Only teach the model to run `mc` in a shell:** needs the `mc` command, a
  token file and a shell on the worker's machine, and no harness tells the
  model how MC works. Stays possible; not enough alone.
- **Re-implement the pre-finish checks in the MCP server:** rejected — two
  owners of one rule (ADR-037).
- **Register the worker tools for every agent-mode process:** rejected — a
  harness with its own bridge would poll twice and steal its own cards.
- **Let the operator-mode server hand out cards:** rejected — an admin token
  in a head (PRINCIPLES §8 rule 3, ADR-085).
- **An HTTP MCP endpoint in the backend:** would spare the local process, but
  adds a network door and a token-per-request design. Not needed for the
  first worker; named in §9.

## Später ändern (ADR-093)

- A worker's scopes, board and runtime are fields of its agent row and are
  changed with the existing agent routes. Resetting its token
  (`POST /agents/{id}/reset-token`) cuts the access at once: the token cache
  no longer outlives a reset (ADR-010 Nachtrag).
- The switch and the two heartbeat values are environment variables of the
  worker's own MCP entry — they belong to the harness config on the worker's
  machine, not to MC's `.env`.
- A new harness joins with one MCP entry (`docs/setup/mcp-worker.md`), not
  with a special case in MC.

## Konsequenzen

### Positiv

- Any harness that can load an MCP server can work cards, on any machine that
  reaches MC, with a local or a hosted model.
- The model is told the rules by the system it works in, at the moment it
  needs them, instead of by a prompt somebody has to maintain per harness.
- One finish logic, one set of routes, no backend change.

### Negativ

- A second way to work cards exists next to the bridges. It is acceptable
  because it reuses the same routes and the same finish command.
- The heartbeat proves less than a bridge's turn signal (§6). A worker that
  hangs inside one long model turn is noticed only after the quiet window.
- A worker counts as live in MC only while it calls MC. Between cards the
  server sends no heartbeat; MC's "last seen" is its last call to MC.
- MC's recovery treats a worker as a host agent. Its restart step
  (`task_runner`, tier 2) finds no service for the worker, logs a warning and
  changes nothing: MC cannot restart a worker. Only re-delivery recovers a
  card, and only when the harness on the worker's machine is still running.
  `RECOVERY_TIER2_SKIP_AGENT_SLUGS` silences the step for a named worker.
- The brief still speaks in `mc` verbs. The verb table translates; a verb
  without a tool can only be reported in a progress note.
- A worker's token lives in the harness config on the worker's machine. That
  is the same trust as an agent's env file, on a machine MC does not manage.

### Rückweg

Remove `MC_MCP_WORKER` from the worker's MCP entry: the tools are gone at the
next start. Reset the agent's token in MC: the access is gone at once. The
code is one section at the end of `scripts/mc-mcp.py` and reverts on its own.

## Referenzen

- `scripts/mc-mcp.py` (section "Worker flow (ADR-101)"),
  `scripts/mc-cli/mc_cli/commands.py` (`_cmd_finish`, `_validate_reflection`),
  `backend/app/routers/agents.py` (`GET /agent/me/poll`, heartbeat),
  `backend/app/routers/agent_scoped.py`, `backend/app/scopes.py`,
  `backend/tests/test_mc_mcp_worker_flow.py`, `docs/setup/mcp-worker.md`
- Related ADRs: ADR-034 (vault as source of truth), ADR-037 (pre-finish
  checks), ADR-063 (agent wizard), ADR-085 (head per job, operator-only admin
  MCP), ADR-097 (delivered is not working), ADR-100 (foundation under every
  harness, guest sessions)
