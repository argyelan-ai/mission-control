"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { TaskActivity } from "@/lib/chatTypes";

/** How often the chat's task block asks (a read of two files + one count).
 *  The clock of a running thought ticks locally every second regardless. */
export const TASK_ACTIVITY_POLL_MS = 2000;

/** How often it asks while the answer is `applies: false` — the chat itself
 *  shows the task (Claude Code, Kimi, Grok, native omp, Hermes …), which
 *  does not change from one second to the next (review R2). Not "never":
 *  an omp ACP agent can answer false for a moment while its pointer is set. */
export const TASK_ACTIVITY_IDLE_POLL_MS = 30_000;

export function taskActivityInterval(data: TaskActivity | undefined): number {
  return data?.applies === false ? TASK_ACTIVITY_IDLE_POLL_MS : TASK_ACTIVITY_POLL_MS;
}

/**
 * What the agent's RUNNING task does, for the chat's task block
 * (`GET /agents/{id}/chat/task-activity`). Only mounted while the chat shows
 * a bound task ("Working on …"), so an idle agent costs nothing. `null`
 * until the first answer, and on an error — the block then shows nothing
 * rather than a stale picture.
 */
export function useTaskActivity(agentId: string): TaskActivity | null {
  const query = useQuery({
    queryKey: ["chat-task-activity", agentId],
    queryFn: () => api.chat.taskActivity(agentId),
    refetchInterval: (q) => taskActivityInterval(q.state.data),
    refetchIntervalInBackground: false,
    retry: false,
  });
  if (query.isError) return null;
  return query.data ?? null;
}
