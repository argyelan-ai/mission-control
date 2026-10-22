"use client";

/**
 * One task step in full, on tap (spec A §4.3): a thought's text, a tool's
 * command and output, the task text, a whole text row. Cached per row; the
 * running thought (`rowId = "live"`) refreshes every 2 s, only while open.
 */
import { createContext, useContext } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { TaskStepDetail, TaskStepLiveDetail } from "@/lib/chatTypes";

export const TASK_STEP_LIVE_POLL_MS = 2_000;

/** Which agent's chat the task rows belong to (the rows only know their task). */
export const TaskStepsAgentContext = createContext<string | null>(null);

export const taskStepKey = (agentId: string, taskId: string, rowId: string) =>
  ["chat-task-step", agentId, taskId, rowId] as const;

interface Cached<T> {
  data: T;
  etag: string | null;
}

export function useTaskStepDetail<T extends TaskStepDetail | TaskStepLiveDetail = TaskStepDetail>(
  taskId: string,
  rowId: string | null,
  open: boolean,
  /** Part of the cache key: a running tool's detail is fetched again once it is done. */
  version: string = "",
): { data: T | null; loading: boolean; error: boolean } {
  const agentId = useContext(TaskStepsAgentContext);
  const qc = useQueryClient();
  const live = rowId === "live";
  const key = [...taskStepKey(agentId ?? "", taskId, rowId ?? ""), version] as const;
  const query = useQuery({
    queryKey: key,
    enabled: !!agentId && !!rowId && open,
    queryFn: async (): Promise<Cached<T>> => {
      const prev = qc.getQueryData<Cached<T>>(key);
      const res = await api.chat.taskStepDetail(agentId as string, taskId, rowId as string, { etag: prev?.etag });
      if (res === null) {
        if (prev) return prev;
        throw new Error("task step: 304 without a cached answer");
      }
      return { data: res.data as T, etag: res.etag };
    },
    staleTime: live ? 0 : Number.POSITIVE_INFINITY,
    refetchInterval: live && open ? TASK_STEP_LIVE_POLL_MS : false,
    refetchIntervalInBackground: false,
    retry: false,
  });
  return { data: query.data?.data ?? null, loading: query.isFetching && !query.data, error: query.isError };
}
