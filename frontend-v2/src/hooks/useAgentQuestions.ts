"use client";

/**
 * useAgentQuestions — the open questions one agent asked the operator
 * (`GET /agents/{id}/chat/questions`, E11). Harness-neutral: a Claude
 * terminal agent's `mc question` shows up here exactly like an omp agent's.
 *
 * Fresh in two ways: the activity stream says when a question of THIS agent
 * was asked or answered (`clarification.*`, `approval.*`), and a slow poll
 * catches the kinds that raise no activity event (`mc ask` thread questions)
 * and a stream that dropped silently.
 */
import { useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { api, sseUrls } from "@/lib/api";
import type { OperatorQuestion } from "@/lib/chatTypes";
import { useSSE } from "@/lib/sse";

/** Poll interval as the safety net — the stream is the fast path. */
export const QUESTIONS_POLL_MS = 15_000;

const EMPTY: OperatorQuestion[] = [];

export function agentQuestionsKey(agentId: string | null) {
  return ["agent-questions", agentId] as const;
}

/** Activity events that may add or remove a question of `agentId`. */
export function touchesQuestions(event: string, data: Record<string, unknown> | undefined, agentId: string): boolean {
  if (!event.startsWith("clarification.") && !event.startsWith("approval.") && !event.startsWith("install.")) {
    return false;
  }
  const about = data?.agent_id;
  // No agent on the event: cannot rule it out, refetch (cheap, one row set).
  return about == null || about === agentId;
}

export function useAgentQuestions(agentId: string | null) {
  const qc = useQueryClient();
  const enabled = !!agentId;

  const query = useQuery({
    queryKey: agentQuestionsKey(agentId),
    queryFn: async () => (await api.chat.questions(agentId!)).questions,
    enabled,
    refetchInterval: QUESTIONS_POLL_MS,
    refetchOnWindowFocus: true,
  });

  useSSE(sseUrls.activity(), {
    enabled,
    onEvent: (event, data) => {
      if (agentId && touchesQuestions(event, data, agentId)) {
        void qc.invalidateQueries({ queryKey: agentQuestionsKey(agentId) });
      }
    },
  });

  const refresh = useCallback(() => {
    void qc.invalidateQueries({ queryKey: agentQuestionsKey(agentId) });
  }, [qc, agentId]);

  return { questions: query.data ?? EMPTY, loading: query.isLoading, refresh };
}
