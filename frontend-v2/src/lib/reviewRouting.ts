import type { Agent, Task } from "@/lib/types";

/**
 * `dispatch_intent` values that mean "this task never got a real, positive
 * handoff into its current holder" — carried over unchanged from whatever
 * the in_progress phase set, because `handle_review_handoff` (task_lifecycle.py)
 * returned early before overwriting it. Each is a value the task can still
 * be wearing once it reaches `review`:
 *  - "root" — first submission of a root task; never left in_progress via a
 *    handoff.
 *  - "subtask" — same, for a delegated subtask.
 *  - "review_rework" — came back from a request_changes cycle and the
 *    resubmitting developer hit the same "reviewer === developer" wall again
 *    on their next `review` transition.
 *
 * Deliberately NOT in this set (B2, PR #517 Rex review — the original
 * `!== "review_handoff"` check wrongly caught these too):
 *  - "manual_redispatch" — set by tasks.py / agent_task_status.py on ANY
 *    `assigned_agent_id` change, independent of status. A Lead reassigning a
 *    stuck review from one reviewer to another produces exactly this value
 *    on the NEW reviewer — that reviewer IS being actively handed the card,
 *    just not through `handle_review_handoff`, so it must not read as a stall.
 *  - "human_review" — `handle_human_review_handoff` clears
 *    `assigned_agent_id` in the same step, so a task carrying this value
 *    never reaches this function with an agent still assigned in the first
 *    place.
 *  - "test_handoff" — moves the task to `user_test`, not `review`; not a
 *    reachable state here.
 *  - "review_handoff" — the real-handoff value itself, excluded by
 *    definition (that's what a non-stall looks like).
 */
const NO_HANDOFF_DISPATCH_INTENTS = new Set<Task["dispatch_intent"]>([
  "root",
  "subtask",
  "review_rework",
]);

/**
 * True when a `review` task is assigned to a reviewer-role agent, but no
 * actual handoff to that agent ever happened — the card just sits with the
 * same agent that developed it (W2, PR #514 Rex review).
 *
 * See `NO_HANDOFF_DISPATCH_INTENTS` for which `dispatch_intent` values this
 * checks against and why — that gap is the only signal the frontend has, the
 * backend does not expose a dedicated "self-review" flag.
 */
export function isSelfReviewStall(task: Pick<Task, "assigned_agent_id" | "dispatch_intent">): boolean {
  return !!task.assigned_agent_id && NO_HANDOFF_DISPATCH_INTENTS.has(task.dispatch_intent);
}

/**
 * Decides whether a task in `review` is the OPERATOR's decision or an agent's.
 *
 * Background (incident 11.09.2026): every task in `review` was offered to the
 * operator with Approve/Reject buttons — including reviews that had already
 * been handed to the reviewer agent. The operator read that as "I have
 * to review this" and approved cards mid-review. Rule from the operator:
 * a review reaches them only when they explicitly asked for it
 * (`human_review_required`), when no reviewer agent holds it, or when the
 * reviewer agent holding it never actually got the handoff (self-review
 * stall, see `isSelfReviewStall`) — in both of the latter cases nobody is
 * independently looking at the card, so it is the Lead's / operator's call.
 *
 * Role check uses `role_canonical`, not the raw `role` string (W1, PR #514
 * Rex review): `Agent.role` is freetext-capable in the backend (setattr in
 * the PATCH field-merge loop bypasses the model's enum validator), so a
 * strict `role === "reviewer"` comparison here would silently stop matching
 * a real reviewer agent whose role got written as e.g. "Reviewer" or
 * "code-reviewer". `role_canonical` is normalized server-side once
 * (app/scopes.py:normalize_agent_role); an agent missing it (e.g. a stale
 * cached object, or truly freetext with no canonical match) falls through
 * to `!== "reviewer"` → operator, which is the documented safe direction.
 */
export function isOperatorReview(
  task: Pick<Task, "human_review_required" | "assigned_agent_id" | "dispatch_intent">,
  agent?: Pick<Agent, "role_canonical"> | null,
): boolean {
  if (task.human_review_required) return true;
  if (!task.assigned_agent_id) return true; // nobody holds the review → operator
  if (!agent) return true; // unknown agent → do not hide silently
  if (agent.role_canonical !== "reviewer") return true;
  return isSelfReviewStall(task); // reviewer === developer → no real handoff → Lead's call
}
