# ADR-097 — Delivered is not working: claim on the model's first work, hold delivery while a local runtime is down

**Status:** Accepted
**Datum:** 2026-10-19
**Scope:** Backend/Dispatch · omp-bridge · Infra/Runtime

## Kontext

Live finding 19.10.2026 (task `4de17c6a`): the DGX Spark behind the slot
runtime was switched off. The poll handed the task to the omp-bridge, and
**4 s later** the bridge PATCHed `in_progress` — before the model had done
anything. omp then retried the dead endpoint for about 7 minutes per attempt;
the card said "in progress" for 20 minutes until it ended `blocked`.

Two things made that possible:

1. The bridge's serve loop called `mc ack` up front, before the run
   (`docker/omp-bridge/bridge.py`, "claim the task up front"). Delivery and
   work were the same moment in MC's eyes. The operator: *delivering is not
   picking up, and not working.*
2. The runtime-readiness gate (ADR-042, point 6) holds tasks only for
   `power_managed` runtimes, on purpose. With local AI as the main part of
   MC (PRINCIPLES, ADR-086), every local agent was open to a task being
   delivered into a session that can never start.

## Entscheidung

1. **The claim waits for the model (omp-bridge, ACP driver).** `mc ack`
   (inbox → in_progress) fires on the model's first work: a thought chunk, a
   tool call, a second text chunk, or a turn that ended `end_turn` /
   `max_tokens` with text. A lone text chunk is not enough — with the model
   endpoint down omp sends its error as one chunk and ends `refusal`. Until
   then the card stays `inbox` with `dispatched_at` set: delivered, not
   working. Bridge comments are held while the card is unclaimed (an agent
   comment is an implicit ACK in the backend); a run whose model never
   answered is blocked straight from inbox, with the cause in the blocker
   question. The native TUI path keeps the up-front ack (no event stream).
   The ownership check before a retry or continue-nudge (`mc task-get`,
   #881) counts `inbox` as still ours while the card is unclaimed; another
   status or another assignee still stops the run.
2. **The backend accepts the delivered-unclaimed shape.** The heartbeat
   control channel treats an inbox card with `dispatched_at` set, no ACK and
   the same dispatch attempt as live work (not "withdrawn"); a requeue still
   interrupts (it clears `dispatched_at` and the attempt). An agent blocking
   straight from inbox stamps `ack_at` (not `started_at`), so the poll does
   not hand the dead task out again.
3. **The poll claim waits for every local runtime.** ADR-042 point 6 is
   extended: `agent_poll` passes `include_local=True`; a runtime on one of
   our own boxes (`heads.pairs.is_local`: host-bound, OpenAI protocol) whose
   models endpoint does not answer holds the claim. One cached GET (same TTL
   as ADR-042); any answer below 500 counts as up, so auth or an unusual
   health path never stalls a working box; unexpected errors fail open.
   The held task's timeline gets one debounced line saying why. Push sites
   (delegation, manual re-dispatch) are **not** gated for local runtimes —
   a lead can still queue work while the model is down; it is delivered
   when the model answers.

## Alternativen

- **Stamp `ack_at` at delivery, keep the status, move the ACK timeout to
  `ack_at`** (reviewer suggestion) → not taken. `ack_at` means "the agent
  confirmed the task" in a dozen places (poll recovery, orphan detection,
  ACK ladder, analytics); redefining it is a state-machine change across
  the backend. `dispatched_at` already is the delivery stamp.
- **A new task state "delivered"** → would need a migration and a new column
  in every status view; `inbox` + `dispatched_at` already says exactly that.
- **Gate local runtimes at push sites too** → rejected: delegation would
  fail with 503 while the model is off, and the work would be lost instead of
  waiting.
- **Mark the task `blocked` when the runtime is down** → rejected: the
  runtime watcher brings models back on its own; a held inbox task is
  delivered automatically, a blocked one needs a human.

## Später ändern (ADR-093)

Nothing new to configure: the operator changes the outcome by starting the
model (Runtimes page) or rebinding the agent; the held task then goes out on
the next poll. The timeline line on the card says which runtime is not
answering. `ENABLE_LOCAL_RUNTIME_READINESS_GATE=false` is an emergency kill
switch like its ADR-042 sibling, not a user setting.

## Konsequenzen

### Positiv
- "In progress" means the model is working. A dead runtime no longer shows
  as busy work for 20 minutes.
- Tasks for an agent whose model is off wait in the inbox and go out by
  themselves once the model answers.

### Negativ
- **Deploy order:** the backend part (heartbeat guard) must be live before
  the new bridge image; an old backend would read every pre-claim heartbeat
  as "withdrawn" and stop the run.
- Between delivery and the model's first work the card is `inbox`; the ACK
  ladder pauses for it only while the heartbeat reports "working"
  (task_runner, capped at 2× the ACK timeout).
- One extra cached HTTP GET per local runtime per TTL on the poll path.

## Referenzen

- Betroffene Dateien: `docker/omp-bridge/bridge.py` (`FirstActivityAck`,
  `run_acp_once`, `drive_live_run`, `serve_loop`),
  `backend/app/services/runtime_readiness.py`,
  `backend/app/routers/agents.py` (`agent_poll`, `_withdrawn_task_reason`),
  `backend/app/routers/agent_task_status.py` (agent PATCH)
- Verwandte ADRs: ADR-042 (point 6 extended), ADR-086, ADR-093
