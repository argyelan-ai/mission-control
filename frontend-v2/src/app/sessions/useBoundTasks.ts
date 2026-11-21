"use client";

import { useCallback, useRef } from "react";
import { useQueries, type UseQueryResult } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Task } from "@/lib/types";

/** The distinct task ids the listed agents are bound to, in list order. */
export function boundTaskIds(agents: ReadonlyArray<{ current_task_id?: string | null }>): string[] {
  const ids: string[] = [];
  for (const a of agents) {
    const id = a.current_task_id;
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

/** A task of another board answers 404 — asking again changes nothing. */
function retryUnlessGone(failureCount: number, error: unknown): boolean {
  if (error instanceof Error && error.message.startsWith("API 404")) return false;
  return failureCount < 2;
}

/**
 * Module-level on purpose: React Query re-runs `combine` (and hands out a
 * new `tasks` array) whenever its function reference changes.
 */
function combineTasks(results: UseQueryResult<Task>[]) {
  return {
    tasks: results.map((r) => r.data).filter((t): t is Task => !!t),
    refetchers: results.map((r) => r.refetch),
    settled: results.every((r) => r.isSuccess || r.isError),
  };
}

/**
 * The tasks the chat page shows: only the ones its agents are bound to (the
 * chat's working-on row, the picker's task title and project group). It used
 * to load the board's whole task list for that — about 1 MB gzip on a busy
 * board, on every chat open — to look up at most one task per agent.
 *
 * Same answers as the list: a task of another board is not found (404), so
 * the lookups by id come back empty for it exactly like `list.find` did.
 * `refetch` asks again for every bound task, `isSuccess` is true once every
 * lookup has settled (found or not), like the list's first load.
 */
export function useBoundTasks(
  boardId: string | null | undefined,
  agents: ReadonlyArray<{ current_task_id?: string | null }>,
): { tasks: Task[]; refetch: () => Promise<unknown>; isSuccess: boolean } {
  const ids = boundTaskIds(agents);
  const { tasks, refetchers, settled } = useQueries({
    queries: ids.map((id) => ({
      queryKey: ["task", boardId, id],
      queryFn: () => api.tasks.get(boardId!, id),
      enabled: !!boardId,
      retry: retryUnlessGone,
    })),
    combine: combineTasks,
  });
  // One stable function, like `useQuery`'s own `refetch`: the page's "bound
  // task missing → ask again" effect lists it as a dependency, and a new
  // function per result change would re-fire it in a loop for a task that
  // stays 404 (another board).
  const latest = useRef(refetchers);
  latest.current = refetchers;
  const refetch = useCallback(() => Promise.all(latest.current.map((f) => f())), []);
  return { tasks, refetch, isSuccess: !!boardId && settled };
}
