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
7. **Routes** (admin, like the other chat writes): `POST …/chats/{id}/switch`
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
