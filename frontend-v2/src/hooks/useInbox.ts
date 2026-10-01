"use client";

import { useMemo } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { deriveInbox, reviewNeedsComments, type InboxBuckets } from "@/lib/inbox";
import type { Agent } from "@/lib/types";
import type { HeadRun } from "@/lib/heads";

/**
 * Everything that waits on the operator, loaded once and shared: the Inbox
 * page renders the buckets, the phone tab bar shows `count`. Both mount this
 * hook with the same query keys, so React Query serves one set of requests.
 *
 * Comments are only fetched for reviews that wait on the assigned agent's
 * comment — the only case `deriveInbox` reads them.
 */
export function useInbox({ interval = 15_000 }: { interval?: number } = {}): InboxBuckets & {
  agentMap: Record<string, Agent>;
  boardId: string | null;
} {
  const { activeBoardId } = useAppStore();

  const { data: approvals } = useQuery({
    queryKey: ["approvals"],
    queryFn: api.approvals.list,
    refetchInterval: interval,
  });

  const { data: reviewTasks } = useQuery({
    queryKey: ["review-tasks", activeBoardId],
    queryFn: () => api.tasks.list(activeBoardId!, { status: "review" }),
    enabled: !!activeBoardId,
    refetchInterval: interval,
  });

  const { data: agents } = useQuery({
    queryKey: ["agents", activeBoardId],
    queryFn: () => api.agents.list(activeBoardId ?? undefined),
    enabled: !!activeBoardId,
  });

  // Heads answer 404 `heads_disabled` while the feature is off — then there
  // are simply no head questions.
  const { data: heads } = useQuery<{ runs: HeadRun[] }>({
    queryKey: ["heads", "runs", "all"],
    queryFn: () => api.heads.list(),
    refetchInterval: interval,
    retry: false,
  });

  const agentMap = useMemo<Record<string, Agent>>(
    () => Object.fromEntries((agents ?? []).map((a) => [a.id, a])),
    [agents],
  );

  const needComments = (reviewTasks ?? []).filter((task) =>
    reviewNeedsComments(task, task.assigned_agent_id ? agentMap[task.assigned_agent_id] : null),
  );
  const commentQueries = useQueries({
    queries: needComments.map((task) => ({
      queryKey: ["task-comments", activeBoardId, task.id],
      queryFn: () => api.tasks.comments.list(activeBoardId!, task.id),
      enabled: !!activeBoardId,
      staleTime: 30_000,
    })),
  });
  const commentsByTask = Object.fromEntries(needComments.map((task, i) => [task.id, commentQueries[i]?.data]));

  const buckets = deriveInbox({
    approvals: approvals ?? [],
    reviewTasks: reviewTasks ?? [],
    agents: agents ?? [],
    commentsByTask,
    headRuns: Array.isArray(heads?.runs) ? heads.runs : [],
  });

  return { ...buckets, agentMap, boardId: activeBoardId };
}
