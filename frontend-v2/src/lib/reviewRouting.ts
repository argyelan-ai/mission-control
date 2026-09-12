import type { Agent, Task } from "@/lib/types";

/**
 * Decides whether a task in `review` is the OPERATOR's decision or an agent's.
 *
 * Background (incident 11.09.2026): every task in `review` was offered to the
 * operator with Approve/Reject buttons — including reviews that had already
 * been handed to the reviewer agent. The operator read that as "I have
 * to review this" and approved cards mid-review. Rule from the operator:
 * a review reaches them only when they explicitly asked for it
 * (`human_review_required`) or when no reviewer agent holds it.
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
export function isOperatorReview(task: Pick<Task, "human_review_required" | "assigned_agent_id">, agent?: Pick<Agent, "role_canonical"> | null): boolean {
  if (task.human_review_required) return true;
  if (!task.assigned_agent_id) return true; // nobody holds the review → operator
  if (!agent) return true; // unknown agent → do not hide silently
  return agent.role_canonical !== "reviewer";
}
