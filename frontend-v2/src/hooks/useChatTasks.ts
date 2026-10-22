"use client";

/**
 * The tasks of an agent's chat (spec A §4.1/§6.1): `GET /agents/{id}/chat/tasks`.
 *
 * Polls 10 s while a task is live, 60 s otherwise, nothing while the tab is
 * hidden or the chat is not on screen (the caller's `enabled`). Sends the
 * last ETag; a 304 keeps the last answer. Runs in parallel with the chat's
 * history load — it needs nothing from it (S6).
 *
 * And at once on a chat-state flip (§2.2/§6.1): when the chat's state
 * crosses to or from `task_working`, `waiting_model` or `idle`, a task was
 * delivered, started or ended — without this a task delivered to an idle
 * agent showed up to a minute late, and nothing else names it meanwhile
 * (review MUST-2 on #937). The ETag keeps a no-change answer a 304.
 */
import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ChatTasksResponse } from "@/lib/chatTypes";

export const CHAT_TASKS_LIVE_POLL_MS = 10_000;
export const CHAT_TASKS_IDLE_POLL_MS = 60_000;
/** Finished tasks fetched per poll (cards). */
export const CHAT_TASKS_LIMIT = 20;

export const chatTasksKey = (agentId: string) => ["chat-tasks", agentId] as const;

/** Chat states whose crossing (to or from) asks for the stubs at once. */
export const TASK_FLIP_STATES: ReadonlySet<string> = new Set(["task_working", "waiting_model", "idle"]);

/** Does a change of the chat's state from `prev` to `next` ask for the stubs? */
export function isTaskFlip(prev: string | null, next: string | null): boolean {
  if (prev == null || next == null || prev === next) return false;
  return TASK_FLIP_STATES.has(prev) || TASK_FLIP_STATES.has(next);
}

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

export function useChatTasks(
  agentId: string | null,
  enabled: boolean,
  /** The chat's current state (`stream.state.status`) — a flip refetches. */
  chatStatus: string | null = null,
): ChatTasksResult {
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
  const prevStatus = useRef(chatStatus);
  useEffect(() => {
    const prev = prevStatus.current;
    prevStatus.current = chatStatus;
    if (!agentId || !enabled || !isTaskFlip(prev, chatStatus)) return;
    void qc.invalidateQueries({ queryKey: chatTasksKey(agentId) });
  }, [agentId, enabled, chatStatus, qc]);
  return {
    data: query.data?.data ?? null,
    settled: query.isFetched || query.isError,
    error: query.isError,
  };
}
