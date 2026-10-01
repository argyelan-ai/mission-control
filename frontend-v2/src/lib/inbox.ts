/**
 * Inbox — the one place for everything that waits on the operator.
 *
 * Three kinds of items wait (mobile nav V2, operator decision 2026-10-01):
 *   1. reviews that are the operator's call (lib/reviewRouting.ts) and ready
 *      to decide — the reviewer agent already commented, or nobody else will;
 *   2. open approvals (the backend lists only pending ones);
 *   3. open head questions — a head stopped in `needs_you` and nobody has
 *      answered it yet (no newer run for the same task).
 *
 * `deriveInbox` is the ONLY place that decides what counts. The Inbox page
 * renders its buckets and the phone tab bar shows `count` as the badge — so
 * the number on the icon is always the number of rows on the page.
 *
 * Pure: no React, no fetching. The data comes from `hooks/useInbox.ts`.
 */
import type { Agent, Approval, Task, TaskComment } from "@/lib/types";
import type { HeadRun } from "@/lib/heads";
import { isOperatorReview, isSelfReviewStall } from "@/lib/reviewRouting";

export type InboxInput = {
  approvals: Approval[];
  /** Tasks in status `review` on the active board. */
  reviewTasks: Task[];
  agents: Agent[];
  /** Comments per review task id; `undefined` = not loaded yet. */
  commentsByTask: Record<string, TaskComment[] | undefined>;
  headRuns: HeadRun[];
};

export type InboxBuckets = {
  /** Operator reviews ready for a decision (get Approve/Changes/Hold). */
  reviews: Task[];
  /** Reviews held by a reviewer agent — shown read-only, not counted. */
  agentReviews: Task[];
  /** Operator reviews still waiting for the assigned agent's comment. */
  waitingForReview: number;
  approvals: Approval[];
  /** Latest run per task that waits on an answer from the operator. */
  headQuestions: HeadRun[];
  /** What waits on the operator: reviews + approvals + head questions. */
  count: number;
};

/**
 * True when a review task needs the assigned agent's comment before it is
 * ready for the operator — the only case where comments must be loaded.
 */
export function reviewNeedsComments(task: Task, agent: Agent | null | undefined): boolean {
  if (!isOperatorReview(task, agent)) return false;
  if (!task.assigned_agent_id) return false;
  return !isSelfReviewStall(task);
}

/**
 * The head runs that wait on the operator: per task only the NEWEST run
 * counts (an answered question was continued as a new run via "restart"),
 * and a run whose card was deleted waits on nobody.
 */
export function openHeadQuestions(runs: HeadRun[]): HeadRun[] {
  const latest = new Map<string, HeadRun>();
  const orphans: HeadRun[] = [];
  for (const run of runs) {
    if (!run.task_id) {
      orphans.push(run);
      continue;
    }
    const prev = latest.get(run.task_id);
    if (!prev || (run.created_at ?? "") > (prev.created_at ?? "")) latest.set(run.task_id, run);
  }
  return [...latest.values(), ...orphans].filter((r) => r.state === "needs_you" && !r.task_deleted);
}

export function deriveInbox(input: InboxInput): InboxBuckets {
  const agentMap = new Map(input.agents.map((a) => [a.id, a]));
  const agentFor = (task: Task) => (task.assigned_agent_id ? agentMap.get(task.assigned_agent_id) ?? null : null);

  const reviews: Task[] = [];
  const agentReviews: Task[] = [];
  let waitingForReview = 0;

  for (const task of input.reviewTasks) {
    const agent = agentFor(task);
    if (!isOperatorReview(task, agent)) {
      agentReviews.push(task);
      continue;
    }
    if (!reviewNeedsComments(task, agent)) {
      reviews.push(task);
      continue;
    }
    const comments = input.commentsByTask[task.id];
    if (comments?.some((c) => c.author_agent_id === task.assigned_agent_id)) reviews.push(task);
    else waitingForReview += 1;
  }

  const headQuestions = openHeadQuestions(input.headRuns);
  return {
    reviews,
    agentReviews,
    waitingForReview,
    approvals: input.approvals,
    headQuestions,
    count: reviews.length + input.approvals.length + headQuestions.length,
  };
}

/** Badge text: nothing at 0, "99+" above 99 so the badge keeps its size. */
export function badgeLabel(count: number): string | null {
  if (!Number.isFinite(count) || count <= 0) return null;
  return count > 99 ? "99+" : String(count);
}
