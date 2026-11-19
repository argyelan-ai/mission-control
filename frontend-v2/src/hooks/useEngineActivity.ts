"use client";

/**
 * Is the agent's engine working although the live row went quiet?
 * (31.10.2026: a model holding a tool call's arguments streams nothing for
 * minutes — the row said "silent" while the model wrote a 51 KB file.)
 *
 * Asked only while `enabled` (the row is quiet), every 10 s. The answer is
 * per ENGINE, not per agent. Unknown (no agent, an error, `engine: null`, an
 * answer older than `FRESH_MS`) is never busy.
 */
import { useContext } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { TaskStepsAgentContext } from "./useTaskStepDetail";

export const ENGINE_POLL_MS = 10_000;
/** An answer older than this no longer says "busy" (polling stopped, tab hidden). */
export const ENGINE_FRESH_MS = 30_000;

export const engineActivityKey = (agentId: string) => ["chat-engine", agentId] as const;

export function useEngineBusy(enabled: boolean): boolean {
  const agentId = useContext(TaskStepsAgentContext);
  const query = useQuery({
    queryKey: engineActivityKey(agentId ?? ""),
    enabled: enabled && !!agentId,
    queryFn: () => api.chat.engine(agentId as string),
    staleTime: 0,
    refetchInterval: enabled ? ENGINE_POLL_MS : false,
    refetchIntervalInBackground: false,
    retry: false,
  });
  if (!enabled || !query.data?.engine || query.isError) return false;
  return query.data.engine.busy === true && Date.now() - query.dataUpdatedAt < ENGINE_FRESH_MS;
}
