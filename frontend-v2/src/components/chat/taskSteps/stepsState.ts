/**
 * One task's steps on the client (spec A §6.1): the rows the chat knows,
 * folded page by page from `GET …/chat/tasks/{task_id}/steps`.
 *
 * Pure and structurally shared: a poll that brings nothing new returns the
 * SAME state object, a poll with one new row keeps every older row object —
 * so only the units that really changed re-render (N5, §6.4).
 */
import type { TaskLiveRow, TaskStepRow, TaskStepsResponse } from "@/lib/chatTypes";

export interface TaskStepsState {
  taskId: string;
  status: string;
  live: boolean;
  hasSession: boolean;
  attempts: { n: number; startedAt: string | null }[];
  /** Oldest first, as the server sent them. */
  rows: TaskStepRow[];
  cursor: string;
  liveRow: TaskLiveRow | null;
  /** Rows older than the first loaded one (S12 `notLoaded`). */
  omitted: number;
  etag: string | null;
}

type Page = Extract<TaskStepsResponse, { applies: true }>;

function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a == null || b == null || typeof a !== "object" || typeof b !== "object") return false;
  const ka = Object.keys(a as object);
  const kb = Object.keys(b as object);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => sameValue((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]));
}

/** Rows equal by value — the server re-sends the same row as a new object. */
export const sameRow = (a: TaskStepRow, b: TaskStepRow) => sameValue(a, b);

export function applyStepsPage(prev: TaskStepsState | null, page: Page, etag: string | null): TaskStepsState {
  let rows: TaskStepRow[];
  let omitted: number;
  if (!prev || page.reset) {
    rows = page.rows;
    omitted = page.omitted;
  } else {
    omitted = prev.omitted;
    rows = prev.rows;
    const index = new Map(rows.map((r, i) => [r.id, i]));
    let copy: TaskStepRow[] | null = null;
    const put = (row: TaskStepRow, append: boolean) => {
      const at = index.get(row.id);
      if (at == null) {
        if (!append) return;
        copy ??= rows.slice();
        index.set(row.id, copy.length);
        copy.push(row);
        return;
      }
      const current = (copy ?? rows)[at];
      if (sameRow(current, row)) return;
      copy ??= rows.slice();
      copy[at] = row;
    };
    for (const row of page.rows) put(row, true);
    for (const row of page.updated) put(row, false);
    if (copy) rows = copy;
  }
  const liveRow = prev && sameValue(prev.liveRow, page.liveRow) ? prev.liveRow : page.liveRow;
  const attempts = prev && sameValue(prev.attempts, page.attempts) ? prev.attempts : page.attempts;
  if (
    prev &&
    rows === prev.rows &&
    liveRow === prev.liveRow &&
    attempts === prev.attempts &&
    omitted === prev.omitted &&
    prev.cursor === page.cursor &&
    prev.status === page.status &&
    prev.live === page.live &&
    prev.hasSession === page.hasSession &&
    prev.etag === etag
  ) {
    return prev;
  }
  return {
    taskId: page.taskId,
    status: page.status,
    live: page.live,
    hasSession: page.hasSession,
    attempts,
    rows,
    cursor: page.cursor,
    liveRow,
    omitted,
    etag,
  };
}

/** Ids of tools still running — sent as `open` so their end comes back. */
export function runningIds(state: TaskStepsState | null): string[] {
  if (!state) return [];
  const out: string[] = [];
  for (const r of state.rows) if (r.k === "tool" && r.st === "running") out.push(r.id);
  return out.slice(-50);
}
