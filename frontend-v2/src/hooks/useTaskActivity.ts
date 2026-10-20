"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { TaskActivity } from "@/lib/chatTypes";

/** How often the chat's task block asks (a read of two files + one count).
 *  The clock of a running thought ticks locally every second regardless. */
export const TASK_ACTIVITY_POLL_MS = 2000;

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
    refetchInterval: TASK_ACTIVITY_POLL_MS,
    refetchIntervalInBackground: false,
    retry: false,
  });
  if (query.isError) return null;
  return query.data ?? null;
}
