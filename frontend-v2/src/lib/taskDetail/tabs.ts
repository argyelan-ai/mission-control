import type { TaskStatus } from "@/lib/types";

/**
 * Tabs of the task detail. "thread" is not in the tab strip any more (it is a
 * channel across cards, reached from the ⋯ menu) but it is still a valid view
 * and a valid `?tab=` value. The E2E tab is gone (no task ever had
 * e2e_test_required set).
 */
export const TASK_TAB_KEYS = [
  "summary",
  "comments",
  "deliverables",
  "workspace",
  "transcript",
  "timeline",
  "history",
  "thread",
] as const;

export type TaskTabKey = (typeof TASK_TAB_KEYS)[number];

export function isTaskTabKey(value: unknown): value is TaskTabKey {
  return typeof value === "string" && (TASK_TAB_KEYS as readonly string[]).includes(value);
}

/** Running → Comments (the live activity is there); everything else → Summary. */
export function defaultTabFor(status: TaskStatus): TaskTabKey {
  return status === "in_progress" ? "comments" : "summary";
}

/** The tab to show: the requested one if it exists for this task, else the status default. */
export function resolveTab(
  requested: string | null | undefined,
  available: readonly TaskTabKey[],
  status: TaskStatus,
): TaskTabKey {
  if (isTaskTabKey(requested) && available.includes(requested)) return requested;
  return defaultTabFor(status);
}
