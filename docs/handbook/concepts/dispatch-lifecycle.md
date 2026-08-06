# Dispatch and Lifecycle

Handing work to an autonomous process is easy. Knowing whether it actually got the work, is still doing it, and will eventually say something — that is the hard part, and it is most of what this subsystem does. Mission Control never assumes a task was picked up because a message was sent. A dispatched task stays in `inbox` until the agent explicitly acknowledges it, and from that moment several background loops watch for the ways an agent can fail quietly: never acknowledging, going idle mid-task, acknowledging and then vanishing, or sitting in review that nobody looks at. This page explains the handshake, the watchdogs behind it, and what reaches you when automation gives up.

## The dispatch–ACK handshake

Originally MC set a task to `in_progress` as soon as the message was sent. That lost tasks whenever an agent crashed right after dispatch, hid delivery failures, and let an agent claim it was working without having read anything. [ADR-001](../../decisions/001-dispatch-ack-handshake.md) replaced it with a two-phase handshake:

```
inbox                                created
inbox + assigned_agent_id            assigned
inbox + dispatched_at                briefing delivered, waiting for ACK
in_progress + ack_at                 agent confirmed — actually working
review | blocked | failed | done     terminal
```

Two timestamps carry the state: `dispatched_at` (the briefing went out) and `ack_at` (the agent confirmed with `PATCH status: in_progress`). Every dispatch briefing contains the ACK instruction explicitly, and `mc ack` is the first hard gate in every agent's operating card.

Two consequences worth internalising:

- **"Dispatched but not acknowledged" counts as busy.** An agent holding an unacknowledged task will not be given another one.
- **Re-delivery is idempotent.** An agent can ask for its next task repeatedly and keep getting the same one until it acknowledges.

Each dispatch attempt also carries a `dispatch_attempt_id` ([ADR-035](../../decisions/035-dispatch-attempt-id-audit-trail.md)) that the agent echoes on status updates, so a stale update from an abandoned earlier attempt cannot overwrite the current one.

Delivery itself is runtime-specific (`backend/app/services/dispatch_delivery.py`) with branches for `claude-code`, `cli-bridge` and `host`; an unknown runtime queues the task and logs it rather than dropping it. Every successful dispatch also persists the exact briefing as the first message on the task's thread, so you can read precisely what the agent was told.

## Who gets the task

`find_dispatch_target()` resolves in this order:

1. An explicitly assigned agent — dispatched directly.
2. Otherwise the **board lead** ([ADR-005](../../decisions/005-board-lead-first-dispatch.md)), which is the normal path: the lead breaks the work down and delegates.
3. Otherwise the first available agent, with a warning event.
4. Otherwise nobody — a warning event, and the task waits for you.

If the chosen agent is busy, the task goes into a per-agent Redis FIFO queue and is delivered when the agent frees up. If delivery fails outright, it goes into a pending-dispatch queue and is retried once the agent is reachable again.

## Watchdogs

Two singleton loops run these checks, each holding a Redis lock so only one worker acts: the **watchdog** every 30 seconds (`backend/app/services/watchdog/`) and the **task runner** every 60 seconds (`backend/app/services/task_runner.py`).

| Check | Loop | Trigger | Action |
|---|---|---|---|
| ACK timeout | task runner | `inbox` + `dispatched_at`, no `ack_at` past the agent's ACK timeout | Escalate / re-assign; after 3 attempts a circuit breaker fires `task.dispatch_exhausted` (severity error → notification) |
| Dispatch pending | task runner | Assigned but never delivered, >15 min | Retry delivery |
| Stale progress | task runner | `in_progress` with no comment for the role's idle threshold | Tiered recovery (below) |
| Stuck / silent abort | task runner | Acknowledged, then silent past `stuck_block_minutes` | Nudge, then auto-block + approval ([ADR-046](../../decisions/046-lifecycle-safety-watchdog.md)) |
| Orphaned tasks | watchdog | Agent process not seen for >30 min | Task back to `inbox` |
| Phase completion | watchdog | All subtasks of an `in_progress` parent are done | Parent → `review`, notify board lead |
| Review stuck | watchdog | Task in `review` with no update: 60 / 120 / 180 min | Nudge reviewer → notify lead → create an approval for you |
| Review decision missing | watchdog | Reviewer commented but recorded no decision, 15 min | Nudge |
| Blocked tasks | watchdog | Task sitting in `blocked` past the board's triage window | Reminder while an approval is pending; then escalates to the operator (`blocker_decision` approval) |
| Dependency zombies | watchdog | Task in `inbox`/`in_progress` waiting on a dependency that is `failed`/`blocked` | Creates an approval so the operator resolves the dependency |
| Undispatched recovery | watchdog | Assigned and active but `dispatched_at` is `NULL` | Re-send, max 1 per agent per cycle |
| Expired approvals | watchdog | `expires_at` passed | Mark `expired` |
| Agent health | watchdog | Stuck in `restarting` beyond the timeout | Mark `error` |
| Queue / pending processing | watchdog | Agent became free or reachable | Deliver the next queued task |

**ACK timeouts are per runtime**, not one global number (`_get_ack_timeout_minutes`): a per-agent `dispatch_config` override wins, then the runtime default — 5 minutes for `host`, 15 for `cli-bridge` — then a hard fallback of 5. Host agents should escalate quickly; containers need room for a cold start.

**Idle thresholds are per role.** Workers (`developer`, `reviewer`, `tester`, `deployer`, `writer`) are considered stale after 15 minutes without a comment, `researcher` after 20, `orchestrator` after 45 — orchestrators delegate and wait on callbacks by design. The fallback for an unknown role is 60 minutes.

## Tiered recovery

A stale task does not go straight to a notification. [ADR-026](../../decisions/026-context-management-auto-recovery.md) defines an escalation ladder that tries to revive the agent first:

1. **Heartbeat probe** (10 s timeout) — is it alive at all?
2. **Restart** the agent, per runtime (Docker container or host process).
3. **Resume** with a structured recovery recap, so the agent does not wake up with a truncated prompt and no idea what it was doing.
4. **Notify you** — an error-severity event, which routes to your configured channel.

The same ADR added **context compaction**: agents self-report context usage in their heartbeat, and at 85 % the backend sends a checkpoint instruction, waits, and then resets the session *with* a structured recap rather than wiping it blind. Every stage emits an activity event (`agent.compaction`, `agent.recovery_started`, `agent.recovery_tier_complete`, `agent.recovery_failed`), which is the audit trail — there is no separate recovery table.

## Silent abort: the last rung

The nastiest failure mode is an agent that acknowledges a task and then simply stops, without ever sending a terminal update. The task hangs `in_progress` forever, blocks its phase, and appears in no escalation lane. [ADR-046](../../decisions/046-lifecycle-safety-watchdog.md) closes this — but conservatively, because the governing rule is that **falsely blocking a healthy agent is worse than the bug**. A long build, a big test run or a slow reasoning turn must never be mistaken for death.

So the check fires only when *all* of these hold: the agent is on `cli-bridge` (the only runtime that stamps a liveness signal *during* work), the task was genuinely acknowledged, nothing is waiting on a callback, the agent is not a board lead, the process **is** alive (`last_seen_at` fresh — otherwise it's process death, which a different check handles), its in-turn activity signal is stale past a conservative threshold (25 minutes default, 45 for slow local models), no agent comment corroborates activity, and no equivalent approval is already open. Even then it nudges first and blocks only if the condition persists.

The action is the least destructive one available: the task goes to `blocked` — never `failed` — keeping its assignee so it can be resumed, while releasing the agent's lock so it stops looking busy. You get an approval with a concrete question.

Be aware of the accepted gap: **host agents are not covered in v1.** Their poll loop doesn't refresh the in-turn liveness signal, so including them would block healthy long-runners. A silent-aborting host agent still hangs until the underlying heartbeat is ported.

## Review gates and approvals

Review is a handoff, not a status change. Moving a task to `review` finds a reviewer, assigns the task, and delivers a review briefing; a rejection sends it back to the original developer with the reviewer's feedback. A re-dispatch is recognisable at a glance: the briefing is titled "correction needed" and leads with the feedback instead of the original description.

Whether review is mandatory is a board setting, off by default — see [Boards and tasks](boards-and-tasks.md) and [ADR-023](../../decisions/023-review-policy-trust-by-default.md).

**Approvals** are the channel from automation to you. They appear in the **Inbox**, expire on a deadline (the watchdog marks stale ones `expired`), and can be resolved from the UI or a connected chat channel. The kinds you will see: blocker decisions when an agent is stuck, install requests for skills, plugins and MCP servers, review-stuck escalations, and loop gates. A board's `blocker_triage_minutes` (default 15) gives the board lead a window to resolve a blocker itself before it reaches you.

## What this buys you

There are layered safety nets — immediate dispatch on creation, 30-second queue and delivery retries, 60-second ACK and staleness checks, then escalation — with the deliberate design property that **no task silently disappears**. Some path always picks it up again, and when automation runs out of options it asks you rather than staying quiet.

## Related

- [Boards and tasks](boards-and-tasks.md) — statuses and board rules
- [Agents and souls](agents-and-souls.md) — the agent side of the contract
- [`docs/flows/dispatch-system.md`](../../flows/dispatch-system.md) and [`docs/flows/watchdog-system.md`](../../flows/watchdog-system.md) — code-level walkthroughs
