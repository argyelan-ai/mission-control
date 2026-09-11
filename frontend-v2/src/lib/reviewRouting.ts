import type { Agent, Task } from "@/lib/types";

/**
 * Decides whether a task in `review` is the OPERATOR's decision or an agent's.
 *
 * Background (incident 11.09.2026): every task in `review` was offered to the
 * operator with Approve/Reject buttons — including reviews that had already
 * been handed to the reviewer agent (Rex). The operator read that as "I have
 * to review this" and approved cards mid-review. Rule from the operator:
 * a review reaches them only when they explicitly asked for it
 * (`human_review_required`) or when no reviewer agent holds it.
 */
export function isOperatorReview(task: Pick<Task, "human_review_required" | "assigned_agent_id">, agent?: Pick<Agent, "role"> | null): boolean {
  if (task.human_review_required) return true;
  if (!task.assigned_agent_id) return true; // nobody holds the review → operator
  if (!agent) return true; // unknown agent → do not hide silently
  return agent.role !== "reviewer";
}
