import type { TaskStatus } from "@/lib/types";

/** Message keys in the tasks.* namespace — t() at the render site. */
export const STATUS_LABEL_KEY: Record<TaskStatus, string> = {
  inbox: "statusInbox",
  in_progress: "statusInProgress",
  review: "statusReview",
  user_test: "statusUserTest",
  waiting: "statusWaiting",
  done: "statusDone",
  blocked: "statusBlocked",
  failed: "statusFailed",
  aborted: "statusAborted",
};

export function statusLabelKey(status: string): string | null {
  return (STATUS_LABEL_KEY as Record<string, string>)[status] ?? null;
}
