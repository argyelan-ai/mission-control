# Boards and Tasks

Everything an agent does in Mission Control starts as a **task on a board**. A board is a workspace with its own rules, its own agents and its own pipeline; a task is one unit of work that travels through that pipeline until it is done, blocked or abandoned. If you have used a Kanban tool before, most of this will look familiar — the part that is different is that the cards move themselves, because an agent picks a task up, comments on it, and pushes it to the next status through the API. This page explains what the objects are, what the statuses mean, and which board settings change how autonomously the system behaves.

## Boards

A board (`boards` table) groups tasks, agents and rules. Boards can be organised into **board groups** for the sidebar, and each board can point at a **default project** that new tasks inherit.

Four workflow flags decide how much the board does on its own:

| Board flag | Default | What it does |
|---|---|---|
| `auto_dispatch_enabled` | `false` | A newly created task with no assignee is dispatched automatically (board lead first — see [Dispatch and lifecycle](dispatch-lifecycle.md)) |
| `require_review_before_done` | `false` | Blocks `in_progress → done`; the task must pass through `review` |
| `only_lead_can_change_status` | `false` | Only the board lead may change task status |
| `require_approval_for_done` | `false` | Closing a task needs an approval |

`require_review_before_done` defaults to *off* on purpose. [ADR-023](../../decisions/023-review-policy-trust-by-default.md) calls this **trust by default**: a reviewer agent should not have to look at every typo fix. The developer agent decides per task whether review is warranted (the policy — "code on main, new API/schema, security-relevant, unsure ⇒ review; housekeeping, reversible fixes, research ⇒ done" — is written into the agent's SOUL). Boards that genuinely need a hard gate flip the flag back on.

What ADR-023 explicitly *decoupled* from that flag is the **mandatory reflection**: independently of the review gate, an agent closing a task must first post a reflection comment with four required fields and a minimum length (`REFLECTION_REQUIRED_FIELDS` / `REFLECTION_MIN_CHARS` in `backend/app/constants.py`). That reflection is what feeds the learning loop described in [Knowledge](knowledge.md). Board leads are exempt — they coordinate, they don't implement. If *you* close a task manually from the UI, no reflection is required; the backend treats that as a deliberate opt-out.

One more board-level knob: `blocker_triage_minutes` (default 15) gives the board lead a window to resolve a blocker itself before an approval reaches you.

## The Home pipeline

**Home** shows the board's pipeline as swim lanes. There are nine lanes, and empty ones are hidden, so a quiet board shows only three or four:

`Inbox → In Progress → Waiting → Review → User Test → Blocked → Failed → Aborted → Done`

Lane colours come from one shared vocabulary (`frontend-v2/src/lib/colors.ts`), so the same colour means the same thing on every page. `Done` is shown whenever the board has completed tasks, even though those aren't "active".

## Task status

The nine statuses and their allowed transitions live in `backend/app/task_status.py` — that file is the single source of truth, and the same rules are mirrored as a Postgres trigger (`validate_task_transition`, migration 0159), which is the guard actually enforced in production.

| Status | Meaning |
|---|---|
| `inbox` | Created, not started. A dispatched-but-unacknowledged task is *also* still `inbox` |
| `in_progress` | An agent acknowledged it and is working |
| `waiting` | Paused on an answer to a blocking question; the session stays alive |
| `review` | Handed to a reviewer (agent or human) |
| `user_test` | Waiting for you to test it manually |
| `blocked` | External impediment; needs a decision |
| `failed` | The attempt failed |
| `aborted` | Abandoned |
| `done` | Complete |

Allowed transitions (from → to):

| From | To |
|---|---|
| `inbox` | `in_progress`, `blocked` |
| `in_progress` | `review`, `done`, `blocked`, `inbox`, `failed`, `waiting` |
| `review` | `done`, `in_progress`, `inbox`, `blocked`, `failed`, `user_test` |
| `user_test` | `done`, `in_progress`, `review` |
| `waiting` | `in_progress`, `blocked` |
| `blocked` | `inbox`, `in_progress`, `failed` |
| `failed` | `inbox` |
| `done` | `in_progress` (reopen) |
| `aborted` | `in_progress`, `inbox` |

Two subtleties worth knowing. `inbox → done` is *not* allowed for anyone — work has to be started before it can be finished. And `failed → inbox` is the only agent-side exit from a failed task; when you close a dead task from the UI, the backend widens this for the operator path only (`done` and `aborted` become reachable), because that is you cleaning up, not an agent skipping the loop.

## Projects, phases and subtasks

Above boards there are **projects**, and a project can be broken into **phases**. A task carries `project_id`, `phase_id` and optionally `parent_task_id`, which is what makes a phase work: the parent task is the phase, its subtasks are the work.

The mechanics you will actually notice:

- **Starting a phase cascades.** Setting a parent task to `in_progress` dispatches all of its `inbox` subtasks.
- **Finishing a phase is watchdog-driven** ([ADR-008](../../decisions/008-phase-completion-watchdog.md)). Every 30 seconds the watchdog checks parent tasks whose subtasks are all `done`, moves the parent to `review` and notifies the board lead.
- **A parent cannot be closed with open children.** `check_children_complete()` rejects it and names the offenders.
- **Implicit ACK.** When the board lead creates a subtask under a parent that is still `inbox` and assigned to itself, the parent is acknowledged automatically — creating the breakdown *is* the acknowledgement.

## Task fields you will actually touch

| Field | Values / notes |
|---|---|
| `priority` | `critical`, `high`, `medium` (default), `low` — used for list ordering |
| `task_type` | `story`, `bug`, `revision`, `chore` |
| `assigned_agent_id` | Who is working on it now; changes as the task moves (developer → reviewer → back) |
| `owner_agent_id` | Who created/delegated it — immutable, does not follow reassignment |
| `callback_agent_id` | Who gets the completion notification; defaults to the board lead |
| `repo_id` | Explicit repository from the Repos registry ([ADR-052](../../decisions/052-task-repo-select.md)) |
| `due_at`, `checklist_total`/`checklist_done` | Due date and checklist aggregate |

**Repository selection** follows one precedence rule: an explicitly chosen task repo beats the project's repo, which beats the shared `mc-workspace` fallback. A repo picked explicitly does *not* inherit the project's work rules — that is deliberate. See [ADR-050](../../decisions/050-repos-registry.md) for the registry itself.

**Reference files** ([ADR-053](../../decisions/053-reference-files.md)) are operator *input* — a screenshot, a spec PDF, an example CSV — uploaded onto a task or a project (project references are inherited by its tasks, marked as such). They are stored under `~/.mc/references/` and injected into the dispatch briefing as absolute paths, so the agent reads them straight off the shared mount. Do not confuse them with **deliverables**, which are agent *output* with their own lifecycle.

## How a task moves, end to end

1. You create the task on **Home** or **Tasks** (or an agent creates it, or a **Loop** creates one per round — see [ADR-051](../../decisions/051-loops.md)).
2. If the board auto-dispatches and nobody is assigned, it goes to the board lead first; otherwise it waits for you to assign it.
3. The agent receives a briefing and must acknowledge it. Until then the task stays `inbox` with `dispatched_at` set — [Dispatch and lifecycle](dispatch-lifecycle.md) covers what happens if it never does.
4. The agent works, posts progress comments, registers deliverables, and closes with a reflection — either to `review` or straight to `done`.
5. On `review` the git workflow opens a PR; a reviewer agent or you approve, and the merge closes the task.
6. Anything that stalls is picked up by a watchdog rather than sitting silently: no acknowledgement, no progress, no terminal update, a review nobody looked at.

## Where to look in the UI

**Home** is the pipeline and system health. **Tasks** is the full board view with project grouping. **Inbox** is where approvals and decisions land — blockers, install requests, loop gates, stuck reviews. **Sessions** lets you attach to the live terminal of the agent doing the work.

## Related

- [Dispatch and lifecycle](dispatch-lifecycle.md) — how a task reaches an agent and what catches it when it stalls
- [Agents and souls](agents-and-souls.md) — who does the work
- [Knowledge](knowledge.md) — where reflections and lessons end up
- [`docs/flows/task-lifecycle.md`](../../flows/task-lifecycle.md) — the code-level walkthrough
