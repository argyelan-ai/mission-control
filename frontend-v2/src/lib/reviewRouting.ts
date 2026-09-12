import type { Agent, Task } from "@/lib/types";

/**
 * True when a `review` task is assigned to a reviewer-role agent, but no
 * actual handoff to that agent ever happened — the card just sits with the
 * same agent that developed it (W2, PR #514 Rex review).
 *
 * Backend rule (task_lifecycle.py:handle_review_handoff): "Reviewer must not
 * be the same agent" — if the only available reviewer is also the developer
 * of this card, the function returns early and skips the handoff entirely,
 * *before* it ever sets `task.dispatch_intent = "review_handoff"`. So a task
 * that reached `review` through a real handoff always carries
 * `dispatch_intent === "review_handoff"`; a self-review that never got
 * handed off keeps whatever intent it had from the in_progress phase
 * (typically "root"). That gap is the only signal the frontend has — the
 * backend does not expose a dedicated "self-review" flag.
 */
export function isSelfReviewStall(task: Pick<Task, "assigned_agent_id" | "dispatch_intent">): boolean {
  return !!task.assigned_agent_id && task.dispatch_intent !== "review_handoff";
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
