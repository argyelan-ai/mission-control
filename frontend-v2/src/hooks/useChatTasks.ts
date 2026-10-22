"use client";

/**
 * The tasks of an agent's chat (spec A §4.1/§6.1): `GET /agents/{id}/chat/tasks`.
 *
 * Polls 10 s while a task is live, 60 s otherwise, nothing while the tab is
 * hidden or the chat is not on screen (the caller's `enabled`). Sends the
 * last ETag; a 304 keeps the last answer. Runs in parallel with the chat's
 * history load — it needs nothing from it (S6).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ChatTasksResponse } from "@/lib/chatTypes";

export const CHAT_TASKS_LIVE_POLL_MS = 10_000;
export const CHAT_TASKS_IDLE_POLL_MS = 60_000;
/** Finished tasks fetched per poll (cards). */
export const CHAT_TASKS_LIMIT = 20;

export const chatTasksKey = (agentId: string) => ["chat-tasks", agentId] as const;

interface Cached {
  data: ChatTasksResponse;
  etag: string | null;
}

export function chatTasksInterval(data: ChatTasksResponse | undefined | null): number {
  return data && data.applies && data.tasks.some((t) => t.live) ? CHAT_TASKS_LIVE_POLL_MS : CHAT_TASKS_IDLE_POLL_MS;
}

export interface ChatTasksResult {
  data: ChatTasksResponse | null;
  /** The first answer (or an error) is in. */
  settled: boolean;
  error: boolean;
}

export function useChatTasks(agentId: string | null, enabled: boolean): ChatTasksResult {
  const qc = useQueryClient();
  const key = chatTasksKey(agentId ?? "");
  const query = useQuery({
    queryKey: key,
    enabled: !!agentId && enabled,
    queryFn: async (): Promise<Cached> => {
      const prev = qc.getQueryData<Cached>(key);
      const res = await api.chat.tasks(agentId as string, { limit: CHAT_TASKS_LIMIT, etag: prev?.etag });
      if (res === null) {
        if (prev) return prev;
        // A 304 without a cached answer cannot happen (no ETag was sent) —
        // ask once more without one rather than show nothing.
        const again = await api.chat.tasks(agentId as string, { limit: CHAT_TASKS_LIMIT });
        if (!again) throw new Error("chat tasks: empty answer");
        return { data: again.data, etag: again.etag };
      }
      return { data: res.data, etag: res.etag };
    },
    refetchInterval: (q) => chatTasksInterval(q.state.data?.data),
    refetchIntervalInBackground: false,
    retry: false,
  });
  return {
    data: query.data?.data ?? null,
    settled: query.isFetched || query.isError,
    error: query.isError,
  };
}
