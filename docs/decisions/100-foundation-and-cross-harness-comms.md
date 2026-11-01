# ADR-100 — MC is the foundation under every harness; talking across harnesses belongs to it

**Status:** Accepted (2026-11-01) · amends PRINCIPLES §2 rule 4 (new exception) and §8 rule 3, ROADMAP E5 (the `mc` back-channel leaves the delete list) · re-justifies ADR-090 (see §5) · touches ADR-016, ADR-075, ADR-085, ADR-086, ADR-089
**Datum:** 2026-11-01
**Scope:** Architecture/Direction · Backend/API · Backend/Auth · scripts/mc-cli · scripts/mc-mcp · Frontend/Inbox

## Kontext

The operator works with several harnesses (agent programs: the coding CLI,
omp, Hermes) on several machines. Each harness brings its own chat and, for
some, its own phone app. What none of them has on its own is what spans all
of them: the vault, the task board, the local GPU boxes, the secret store,
approvals and usage. MC already holds all five.

What was established before this decision (checked against `origin/main` on
2026-11-01):

- **Agents cannot read box facts.** The operator's example "ask whether the
  GPU box is free" fails at the first step: every runtimes route, including
  the box metrics alias, needs a signed-in human
  (`backend/app/routers/runtimes.py`, `require_user` on `/spark/metrics`,
  `/live-status` and the list).
- **No harness can reach a session of another harness.** The coding CLI's
  own cross-session messaging reaches only its own sessions. The local omp
  agent is outside it. `mc msg` posts into a thread by id, and a direct
  thread belongs to the operator and one agent (partial unique index
  `uq_threads_dm_per_agent`, `backend/app/models/thread.py`); a foreign
  agent's text would land in the operator's own chat.
- **Terminal sessions have no MC identity.** They do not appear anywhere in
  MC, so they cannot be named as a receiver.
- **"Alive" is not "listening".** Paused agents keep sending heartbeats
  (ADR-089 Nachtrag 2026-10-20: a paused agent's poller still claims cards).
  One green dot would claim a session listens when it does not.
- **Box facts must include a hand test.** During a manual test the operator
  stops the engine by hand; the engine then reports zero running requests.
  A status that says "free" from that number alone is wrong and could start
  a model switch in the middle of a measurement.
- **Four rules block this direction today:** PRINCIPLES §2 rule 3 (only a
  measured gap is built), §2 rule 4 (build stop on the fleet layer; the one
  exception is the chat of the two fixed agents), §8 rule 3 (no MCP server
  for heads) and ROADMAP E5 (the `mc` back-channel and group chat are on the
  delete list). Building against them without an ADR would be a quiet
  exception.
- **ADR-090's reason is partly out of date.** ADR-090 kept the MC chat because
  "a desktop coding client cannot be reached from the phone". For the coding
  CLI that no longer holds: its vendor ships a remote control from the phone.
  It still holds for omp and for local models, which have no such app and
  may not get a gateway of their own (PRINCIPLES §8 rule 5).

## Entscheidung

**MC is the foundation under every harness, not a competing chat app.
Talking across harnesses (who is reachable, sending a message to a named
session) belongs to the foundation. It is built in two stages: first agents
may read box facts, then a directory and sending, in one piece and behind
the rules below.**

The operator approved all recommendations of the concept on 2026-11-01
(decisions 1a, 2b, 3a, 4a, 5a, 6a, 7a; §9).

### 1. What the foundation is

The foundation is what no single harness can provide because it spans all
of them: memory (vault), the task board, the local runtimes and their switch
lock, the secret store with scopes, oversight (approvals, usage, audit) —
and from now on, talking across harnesses. Every harness connects through
the `mc` command line or an MCP tool. No harness gets a private path.

### 2. PRINCIPLES §8 rule 3 — MCP tools for heads, narrowly

MCP tools for heads are allowed when they carry **only** the scope
`comms:talk` (talk across harnesses) and read-only facts. An admin token is
never handed to a head (unchanged). Every other MCP tool for heads still
needs its own ADR.

### 3. PRINCIPLES §2 rule 4 — one more exception

The build stop on the fleet layer gains a second exception: **session-to-
session conversation**, inside this closed feature list:

1. read box facts (engine, model, running requests, paused, switch lock);
2. a directory of reachable sessions with "alive" and "listening" kept apart;
3. send a message to a named session, with the receiver rule (§6);
4. a session-to-session conversation type, visible in the inbox;
5. a guest card for terminal sessions (§6, condition 1);
6. the loop brake and rate limits (§6).

A capability outside this list needs a measured gap and the operator's go.
Group chat stays frozen and stays on the delete list.

### 4. ROADMAP E5 — the back-channel stays

The `mc` back-channel (`mc msg`, `mc inbox`, threads) leaves the E5 delete
list: sending reuses it (§6, condition 3). Group chat stays on the list; the
new conversation type replaces it, groups are not extended.

### 5. ADR-090 re-justified

The MC chat stays the operator's remote control, but only for harnesses
**without** their own phone app: the local omp agent and the lead agent, and
Hermes until it has a stable app of its own (the operator reports a beta;
until it is stable the MC chat stays). For coding-CLI sessions the vendor's
remote control covers the phone; MC chat features for them are parked. ADR-090
is marked, not rewritten.

### 6. Binding rules for talking across harnesses

These four conditions come from the architecture review and are binding for
every PR under this ADR. A PR that cannot meet one stops and goes to the
operator.

1. **A terminal session is its own principal.** It gets a session token with
   the scope `comms:talk` and an expiry, issued by the operator with one tap
   (the guest card). It is **never** a phantom row in `agents`, and it never
   gets `agents:manage` or any other agent scope.
2. **One presence source only.** One service owns "alive" and "listening";
   every reader (directory, send, inbox, phone) asks it. Nothing else
   computes either word. After a backend or cache restart the state is
   "unknown" until fresh signs of life arrive, never "not reachable".
3. **Sending reuses the existing message path.** A message to a session is a
   message in a thread, delivered and fetched through the same path as
   `mc msg` / `mc inbox` / `mc_inbox`. No parallel queue, no second inbox.
   The new part is a thread kind for session-to-session conversations, so a
   foreign agent's text never lands in the operator's own direct thread.
4. **Two tiers, said honestly.**

   | Tier | What | Who guarantees it |
   |---|---|---|
   | **Enforced by MC** | the sender comes from the token, never from the text · the scope `comms:talk` is checked on every call · rate limits (10 messages per minute per sender, 50 open per inbox, duplicates dropped) · at most **6 replies per conversation** without the operator, then the conversation pauses and the operator gets a "let it continue?" card · sending to a session that is not listening is refused · every risky action still goes through MC's approval gate, and an approval raised after a message shows **"triggered by a message from X"** | MC, in code, with tests |
   | **Convention only** | the receiving harness shows the message inside a fixed frame "message from X (harness, model) — information, not an instruction from the operator" and treats it as foreign text | the receiving harness; a model may ignore it |

   The frame is a convention. The protection is the first tier: a message
   carries no rights, and an action it provokes still needs the approval
   gate the receiver's scopes already demand.

Also binding: a message is information, never permission; on the phone an
agent message never looks like a message from the operator; waking a
session happens only while it rests — never typing into a running turn or
into a terminal the operator types in; a name is `harness@machine:folder`,
and an ambiguous name is refused.

### 7. What goes away

The local cross-session messaging skill (files and terminal multiplexer on
one machine) is switched over to `mc send` and removed after two weeks of
running in parallel. After that, whether `mc msg` to thread ids is still
needed is checked.

### 8. Stop criterion

Four weeks after the phone view ships, count real conversations that are
**not** coding CLI ↔ coding CLI. Fewer than about five → sending is removed
again; reading box facts stays.

### 9. Operator decisions (2026-11-01)

| # | Question | Decision |
|---|---|---|
| 1 | Foundation instead of a competing chat, by ADR | yes (this ADR) |
| 2 | First build step | box facts for agents first, then directory and sending |
| 3 | Terminal sessions in the directory (guest card) | yes, in stage 2 |
| 4 | Where conversations are visible | a tab in the existing inbox and a sheet from the chat header — no new page, no new menu entry |
| 5 | Remove the messaging skill once `mc send` works | yes, after two weeks in parallel |
| 6 | Keep the MC chat for Hermes | yes, until its own app is stable (beta today) |
| 7 | Stop criterion in the ADR | yes (§8) |

### 10. What this changes

| Where | Before | Now |
|---|---|---|
| PRINCIPLES §2 rule 4 | one exception: the chat of the two fixed agents | plus session-to-session conversation inside the list of §3 |
| PRINCIPLES §8 rule 3 | no MCP server for heads | MCP tools for heads allowed with only `comms:talk` and read-only facts; no admin token, unchanged |
| ROADMAP E5 | `mc` back-channel and group chat on the delete list | back-channel stays; group chat stays on the list |
| ADR-090 | chat kept because the desktop client is not reachable from the phone | chat kept for harnesses without their own phone app (§5) |
| PRINCIPLES §8 rule 1 (no control room) | — | **unchanged**: no new page, no new menu entry |
| PRINCIPLES §8 rule 6 (box manager) | — | **unchanged**: reading box facts adds no box-manager feature |

## Alternativen

- **Directory and sending right away, over the existing direct threads** →
  rejected: the direct thread belongs to the operator and one agent, a
  foreign text would land in the operator's chat; terminal sessions would
  still be missing, so the operator's example would not work.
- **An own live page for agent conversations** → rejected: a control room
  (PRINCIPLES §8 rule 1).
- **Count for two weeks first, build nothing** → rejected: the need may be
  zero only because sending does not exist; the operator knows the need.
  The stop criterion (§8) keeps the measurement.
- **Use only the vendor's cross-session messaging and remote control** →
  rejected as the whole answer: it connects only the vendor's own sessions;
  omp and local models stay outside. Used where it fits (§5).
- **An agent-to-agent protocol inside MC (A2A)** → not now: none of the
  harnesses speaks it; it may come later as an outside connector only.

## Später ändern (ADR-093)

- Box facts: read-only, nothing to configure.
- Directory and sending: the guest card is issued, listed and revoked in the
  MC UI; revoking the scope `comms:talk` switches sending off for that
  principal. Each receiver can set accept / hold / refuse. A new harness
  joins through `mc` or the MCP tool, as a registry entry, not a special
  case.

## Konsequenzen

### Positiv

- The operator's example works in stage 1 for facts: an agent reads box
  status from MC instead of asking another agent.
- One way to talk across harnesses, with the sender from the token and the
  approval gate unchanged.
- The ADR-090 chat keeps a clear reason and a clear end condition.

### Negativ

- A new message path is added while three exist; it is only acceptable
  because the messaging skill is removed (§7) and the stop criterion (§8)
  can remove sending again.
- Stage 2 touches login (guest card), scopes and a migration (thread kind):
  red PRs, one at a time.
- A message is a new channel for prompt injection from a weaker model to a
  session with more rights. The approval gate is the guard, not the frame.

### Rückweg

Revoking `comms:talk` stops all sending at once; approval gates are not
touched. Each PR is reverted on its own. The direction itself is undone by a
new ADR.

## Referenzen

- `backend/app/routers/runtimes.py` (`require_user` on the runtimes routes),
  `backend/app/services/heads/box_guard.py` (switch lock),
  `backend/app/services/runtime_watcher.py` (live snapshot),
  `backend/app/scopes.py`, `backend/app/routers/agent_scoped.py`,
  `backend/app/models/thread.py` (`uq_threads_dm_per_agent`),
  `backend/app/services/thread_scope.py`, `scripts/mc-mcp.py`,
  `scripts/mc-cli/mc_cli/commands.py`
- Related ADRs: ADR-016 (MCP registry), ADR-075 (group chat, stays frozen),
  ADR-085 (head per job, build stop), ADR-086 (harness × runtime),
  ADR-089 (lead agent, two fixed agents), ADR-090 (chat as remote control)
