"use client";

/**
 * The steps of the chat's tasks (spec A §4.2/§6.1): one query per task,
 * key `["chat-task-steps", agentId, taskId]` — never through `useChatStream`
 * or the SSE tailer (#898).
 *
 * First page up to 1,000 rows; then only the delta behind the cursor, with
 * `open` = the running tools (their end comes back in `updated`). A page
 * with `hasMore` is followed at once (S11). A live task polls every 2 s
 * (10 s while it waits for the operator); when it stops being live, one
 * final fetch, then quiet. A card fetches once, when it is opened.
 * Nothing runs while the tab is hidden or the chat is not on screen.
 */
import { useEffect, useRef } from "react";
import { useQueries, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { applyStepsPage, runningIds, type TaskStepsState } from "@/components/chat/taskSteps/stepsState";

export const TASK_STEPS_POLL_MS = 2_000;
export const TASK_STEPS_WAITING_POLL_MS = 10_000;
export const TASK_STEPS_PAGE = 1_000;
/** Pages followed in one go when the server says `hasMore`. */
const MAX_FOLLOW = 10;
/** Live statuses where the task waits on the operator, not the model. */
const WAITING = new Set(["waiting", "blocked", "user_test"]);

export const taskStepsKey = (agentId: string, taskId: string) => ["chat-task-steps", agentId, taskId] as const;

export interface TaskStepsSpec {
  taskId: string;
  /** Live here (polls). */
  live: boolean;
  status: string;
  /** Fetch at all (a card only once it is opened). */
  load: boolean;
}

export interface TaskStepsEntry {
  state: TaskStepsState | null;
  /** The first page (or an error) is in. */
  settled: boolean;
  loading: boolean;
  error: boolean;
  /** `{"applies": false}` from the server. */
  notApplicable: boolean;
  /** When the current answer came in (ms; 0 = none yet). */
  updatedAt: number;
  retry(): void;
}

/** Sentinel for an `applies: false` answer (kept apart from "not loaded"). */
const NOT_APPLICABLE = { notApplicable: true } as const;
type Stored = TaskStepsState | typeof NOT_APPLICABLE;

export async function fetchTaskSteps(qc: QueryClient, agentId: string, taskId: string): Promise<Stored> {
  const key = taskStepsKey(agentId, taskId);
  const cached = qc.getQueryData<Stored>(key);
  let state: TaskStepsState | null = cached && !("notApplicable" in cached) ? cached : null;
  for (let i = 0; i < MAX_FOLLOW; i++) {
    const res = await api.chat.taskSteps(agentId, taskId, {
      limit: TASK_STEPS_PAGE,
      after: state ? state.cursor : null,
      open: runningIds(state),
      etag: state?.etag ?? null,
    });
    if (res === null) {
      if (state) return state;
      throw new Error("task steps: 304 without a cached page");
    }
    if (!res.data.applies) return NOT_APPLICABLE;
    state = applyStepsPage(state, res.data, res.etag);
    if (!res.data.hasMore) break;
  }
  return state as TaskStepsState;
}

export function stepsInterval(spec: TaskStepsSpec): number | false {
  if (!spec.live) return false;
  return WAITING.has(spec.status) ? TASK_STEPS_WAITING_POLL_MS : TASK_STEPS_POLL_MS;
}

export function useTaskSteps(
  agentId: string | null,
  specs: readonly TaskStepsSpec[],
  onScreen: boolean,
): Map<string, TaskStepsEntry> {
  const qc = useQueryClient();
  const results = useQueries({
    queries: specs.map((spec) => ({
      queryKey: taskStepsKey(agentId ?? "", spec.taskId),
      enabled: !!agentId && onScreen && spec.load,
      queryFn: () => fetchTaskSteps(qc, agentId as string, spec.taskId),
      refetchInterval: stepsInterval(spec),
      refetchIntervalInBackground: false,
      refetchOnWindowFocus: spec.live,
      staleTime: spec.live ? 0 : Number.POSITIVE_INFINITY,
      retry: false,
    })),
  });

  // Live → not live: one final fetch picks up the last rows (§6.1).
  const wasLive = useRef(new Set<string>());
  useEffect(() => {
    if (!agentId) return;
    const now = new Set(specs.filter((s) => s.live).map((s) => s.taskId));
    for (const id of wasLive.current) {
      if (!now.has(id)) void qc.invalidateQueries({ queryKey: taskStepsKey(agentId, id) });
    }
    wasLive.current = now;
  }, [agentId, specs, qc]);

  const out = new Map<string, TaskStepsEntry>();
  specs.forEach((spec, i) => {
    const r = results[i];
    const data = r?.data;
    const notApplicable = !!data && "notApplicable" in data;
    out.set(spec.taskId, {
      state: data && !notApplicable ? (data as TaskStepsState) : null,
      settled: !!r && (r.isFetched || r.isError),
      loading: !!r && r.isFetching && !r.data,
      error: !!r && r.isError,
      notApplicable,
      updatedAt: r?.dataUpdatedAt ?? 0,
      retry: () => void r?.refetch(),
    });
  });
  return out;
}
