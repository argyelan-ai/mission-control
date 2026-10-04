"use client";

/**
 * The one shared "recent heads" fetch (bauplan `heads-sichtbar` PR 2 §3.2:
 * "Ein Abruf … Von Chats, PR 3 (Aufgaben/Eingang) geteilt — nie je Zeile
 * abfragen"). `GET /heads?recent_days=` already does the active/ended split
 * server-side (`routers/heads.py::list_heads`'s own docstring) — this hook
 * only adds the one thing every caller needs and none of them should
 * recompute on its own: the NEWEST run per task, so a task row, the task
 * detail's "Zuletzt" line and an inbox row can each do a plain `byTask.get
 * (taskId)` instead of filtering the whole list themselves.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { HEAD_POLL_MS, sortRunsNewestFirst, type HeadRun } from "@/lib/heads";
import { useHeadsEnabled } from "./useHeadsEnabled";

/** Matches the Chats list's own cutoff (bauplan §2 "Lebenslauf": "fertig 7
 *  Tage; dann Archiv") — the backend's own default when this is left out,
 *  named explicitly here so every caller of this hook agrees with the
 *  Archive sheet about where the line is. */
export const HEAD_RUNS_RECENT_DAYS = 7;

const EMPTY_RUNS: HeadRun[] = [];
const EMPTY_BY_TASK = new Map<string, HeadRun>();

export interface UseHeadRunsResult {
  /** Active + ended-within-window runs, in whatever order the backend
   *  returned them — callers that care about display order run this
   *  through `sortHeadsForList` themselves. */
  runs: HeadRun[];
  /** How many ended runs were left out for being older than the window —
   *  the Archive line's count. */
  archivedCount: number;
  /** The newest run for a task id, or `undefined` when that task has never
   *  had one (or only ones older than the window). */
  byTask: Map<string, HeadRun>;
  isLoading: boolean;
  error: Error | null;
}

export function useHeadRuns(): UseHeadRunsResult {
  const headsEnabled = useHeadsEnabled();
  const enabled = headsEnabled === true;
  const q = useQuery({
    queryKey: ["heads", "runs", "recent"],
    queryFn: () => api.heads.list({ recentDays: HEAD_RUNS_RECENT_DAYS }),
    enabled,
    // Polls regardless of whether any single run is active: this one query
    // also has to notice a BRAND NEW head appearing (nothing was active a
    // moment ago) and a run leaving the 7-day window — neither is "this run
    // is active" from the caller's point of view. `useHeadTranscript`'s own
    // 5s poll (bauplan §3.2) is the one that stops once ITS run has ended.
    refetchInterval: enabled ? HEAD_POLL_MS : false,
    staleTime: HEAD_POLL_MS,
  });

  const runs = q.data?.runs ?? EMPTY_RUNS;
  const byTask = useMemo(() => {
    if (runs.length === 0) return EMPTY_BY_TASK;
    const map = new Map<string, HeadRun>();
    // Newest first, first write per task wins — later (older) runs for the
    // same task are simply skipped.
    for (const run of sortRunsNewestFirst(runs)) {
      if (run.task_id && !map.has(run.task_id)) map.set(run.task_id, run);
    }
    return map;
  }, [runs]);

  return {
    runs,
    archivedCount: q.data?.archived_count ?? 0,
    byTask,
    isLoading: q.isLoading,
    error: q.error as Error | null,
  };
}
