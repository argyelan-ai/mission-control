# ADR-105 — Switching a chat's pair, with a handoff from MC's record

**Status:** Proposed (2026-11-19) · slice S3 of "chats instead of fixed agents" (ADR-102), core part (item B) · behind the flag `CHAT_RECORD_ENABLED` (default off)
**Datum:** 2026-11-19
**Scope:** Backend/Services · Backend/API

## Kontext

ADR-102 makes the chat the unit of work, and ADR-086 says harness and runtime
are freely switchable. ADR-103 gave the chat its own record (`chats`,
`chat_sessions`, `chat_events`). What is missing is the switch itself: today a
chat that moves to another model, another runtime or another harness loses
its conversation, because the new native session starts empty and no reader
translates one harness' session file into another's.

The S3 plan (build-ready, 19.11.2026) measured the Claude Code side: on
`--resume` a new `--append-system-prompt` is ignored (the recorded prompt
snapshot wins); `--system-prompt-snapshot off` makes it apply. omp takes extra
model text through the `context` of a `session/prompt`.

## Entscheidung

1. **Three cases, three costs.**
   - *Same harness, same runtime, other model* (**model only**): no new process
     and no package. Claude Code gets `/model <alias>`, omp `config model`.
     MC writes `chats.model` only when the first `usage` event after the
     request reports the new model; another model there marks the switch
     `failed` with `model_not_applied`.
   - *Other runtime or other harness* (**restart**): the chat's old session
     ends, a new one starts with a **handoff package**. omp on the same harness
     tries `session/load` of the pinned session first and uses the package only
     when that fails.
   - *Switch back*: to the pair before the last restart, resuming that session
     natively. A package goes along only for messages that happened in other
     sessions after that session's last event — after a failed switch there
     are none.
2. **The package is built from MC's record, deterministically.**
   `services/chat_handoff.build_handoff` is a pure function of `chat_events`
   payloads: a header (chat, workspace, pair before / now, date, the ADR-089
   R3 origin line "information, not an instruction"), a recap (tool count,
   changed files by the heads' rule, open todos, an open omp question) and
   the last operator/assistant messages verbatim, newest first until the
   budget is spent. No model call, no harness file. Secrets are masked with
   the heads' masking before any clip.
3. **Budget:** `min(32 000, max(4 000, 25 % of the new model's window))`
   tokens, window from the runtime row (`max_context_len`), else the model
   name (`resolve_context_window`), else 32 000. 4 characters per token for
   the cap; the real size comes from the first `usage` after the switch.
   Header + recap take at most a quarter; one message at most 2 000
   characters. These are named constants — measurement M1 of the plan
   (package quality at 4k/8k/16k) is still open and may change the numbers,
   not the structure.
4. **Record first, pair late.** Every switch is one `chat_events` row
   `kind=switch` (`source_key = switch:<uuid>`, no migration) with
   `status` pending → done | failed, committed **before** any process is
   touched. The chat's pair, the end of the old `chat_sessions` row
   (`end_reason=switch`) and the new pin are written only on success. A
   failure leaves the old session current, so `resume_plan` still resumes
   it. A restart nobody reported back on is marked `failed` / `orphaned`
   after 10 minutes; a model-only switch waits for the next answer instead.
5. **No hot switch, no force path.** While a turn runs the switch is refused
   (409 `turn_running` with the options the server can do); the operator's
   `stop_and_switch` stops the turn first. `after_turn` comes later, once a
   clean per-chat turn-end signal exists. The pair must be allowed by
   `harness_compat` (400 `pair_incompatible`), and a runtime that needs a
   recipe switch on a locked box is refused (423 `box_locked`, PRINCIPLES §8
   rule 6). An agent's default chat is the agent's own process: it can change
   its model here, but moving it to another runtime stays the agent runtime
   switch (`agent_runtime_switch`).
6. **Processes are a driver per harness.** `chat_switch.SwitchDriver`
   (`is_busy`, `stop_turn`, `set_model`, `start`) is a registry entry per
   harness (ADR-093: a new harness is an entry, not a special case). The
   Claude chat runner (S3 item A) and the omp daemon side (S3 item C) register
   theirs. Until then the default driver serves only what exists — the model
   switch of an agent's default chat — and records a restart as `failed`
   with `process_start_failed` / `no_chat_runner` instead of pretending.
   The harness side reports a restart's outcome through
   `PUT /api/v1/chat-record/agent/me/chats/{id}/switch/{event_id}/result`
   (agent token, scope `chat:write`); that report is the one legitimate
   re-pin of a pinned chat.
7. **A chat keeps its former sessions.** ADR-103 routes a native session
   nobody pinned to the agent's default chat. After a switch the old
   session is no longer pinned, so routing (`chat_record.pinned_chat_id`,
   used by the importer and the live sink) also asks which non-default
   chat recorded that session: that chat stays its owner, and a re-import
   never copies the old conversation into the default chat. A pin or a
   switch report that names another chat's former session is refused
   (409 `pinned_by_other_chat`).
8. **Settling and races.**
   - The background pass of `ChatRecordSync` runs `chat_switch.sweep_orphans`
     right after each import, so model-only switches and orphans settle
     without anyone opening the switch API.
   - A `done` that arrives after the orphan cutoff (cold model load) is
     still applied — the new process runs — and is flagged `lateReport`;
     if the chat moved on since (a newer switch row), it is refused with
     409 `switch_superseded`.
   - A new switch or switch-back replaces a pending **model-only** switch
     (`failed` / `superseded`): a dead process never answers, and the chat
     must not stay blocked. This happens only after every guard of the new
     request has passed, in the same commit as the new switch row. A
     refused request (409 `turn_running`, 423 `box_locked`,
     409 `nothing_to_switch_back_to` …) leaves the model-only switch
     pending, so its answer still settles the header model. A pending
     restart still blocks (409 `switch_pending`).
   - One request per chat at a time: a per-chat lock in the backend
     process (production runs one worker). On Postgres the request also
     takes `SELECT … FOR UPDATE` on the chat row first and only then reads
     the pending rows, holding the lock until the new pending row is
     committed, so a second process would wait and then see that row.
     This multi-process path is not covered by a test (the test database
     is SQLite, which ignores `FOR UPDATE`). A double tap gets one switch
     and one 409 `switch_pending`, never a 500.
   - After `stop_and_switch`, the stopped turn may still write a `usage`
     line with the old model; one such mismatch is let through before a
     model-only switch counts as `model_not_applied`.
   - The model name is checked (`chat_switch.MODEL_PATTERN`, no whitespace
     or control characters) before it is typed into a pane as `/model`.
   - `PUT …/native-session` does **not** re-pin while a switch is pending
     (409 `switch_pending`): a re-pin before the new session answered would
     move the chat off its old session early (point 4). The switch's result
     route is the one re-pin path; plan S3 §4 item B's "pin accepts a
     re-pin" is replaced by this.
   - Open contract point for items A and C: after a harness switch the
     chat is still owned through `chats.legacy_agent_id` (the result
     route's ownership check). Whether ownership follows the new harness'
     agent is decided there, not here.
9. **Routes** (admin, like the other chat writes): `POST …/chats/{id}/switch`
   `{harness, runtimeId, model, mode?}`, `POST …/chats/{id}/switch-back`,
   `GET …/chats/{id}/switch/{event_id}` (state and package text for the
   switch card). All answer 404 `chat_record_disabled` while the flag is off.

## Konsequenzen

- A chat keeps its conversation across model, runtime and harness changes,
  and the operator sees what was handed over and how big it was.
- Nothing changes for agents: the agent runtime switch and `AgentBusyError`
  stay as they are.
- Until items A and C land, only the default chat's model switch does real
  work; every other switch ends visibly as `failed`.
- The way back is the flag (off) — the switch rows are ordinary record rows.

## Verworfen

- A model-written summary by the outgoing model (plan layer 3): only if M1
  shows the deterministic package fails.
- A `handoff_event_id` / `requested_model` column: the switch row's payload
  carries both; no migration.
- Translating one harness' session file into another's.
