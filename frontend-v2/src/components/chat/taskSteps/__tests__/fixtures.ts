/** Synthetic task-step fixtures (spec A, PR-3). No real names, hosts or paths. */
import type { ChatTaskStub, MessageEvent, TaskStepRow, TaskStepsResponse, ToolEvent } from "@/lib/chatTypes";

export const TASK_ID = "00000000-0000-4000-8000-000000000001";
export const TASK_ID_2 = "00000000-0000-4000-8000-000000000002";

/** 2026-10-21 23:00 UTC + `min` minutes (+ `sec`). */
export function at(min: number, sec = 0): string {
  return new Date(Date.UTC(2026, 9, 21, 23, 0, 0) + min * 60_000 + sec * 1000).toISOString();
}

export function stub(over: Partial<ChatTaskStub> = {}): ChatTaskStub {
  return {
    taskId: TASK_ID,
    title: "Build the report page",
    status: "in_progress",
    live: true,
    deliveredAt: at(0),
    startedAt: at(0, 5),
    startKey: at(0, 5),
    endedAt: null,
    assignee: null,
    outcome: null,
    deliverables: 0,
    ...over,
  };
}

let n = 0;
const nid = () => `1:${++n}`;

export function think(ts: string, lead: string, over: Partial<TaskStepRow & { k: "think" }> = {}): TaskStepRow {
  return { id: nid(), ts, k: "think", lead, ...over } as TaskStepRow;
}
export function tool(ts: string, title: string, st: "running" | "done" | "error" = "done", over: object = {}): TaskStepRow {
  return { id: nid(), ts, k: "tool", name: "bash", title, st, ...over } as TaskStepRow;
}
export function text(ts: string, body: string): TaskStepRow {
  return { id: nid(), ts, k: "text", text: body };
}
export function comment(ts: string, body: string): TaskStepRow {
  return { id: nid(), ts, k: "comment", type: "progress", text: body };
}
export function delivery(ts: string): TaskStepRow {
  return { id: "1:0", ts, k: "delivery", lead: "Build the report page", more: true };
}
export function attempt(ts: string, attemptNo: number): TaskStepRow {
  return { id: `${attemptNo}:0`, ts, k: "attempt", n: attemptNo };
}

export function chatMsg(ts: string, body: string, role: "user" | "assistant" = "user"): MessageEvent {
  return { kind: "message", uuid: `m-${ts}-${body.length}`, ts, role, text: body, model: null, sidechain: false };
}
export function chatTool(ts: string, title: string): ToolEvent {
  return {
    kind: "tool", uuid: `t-${ts}`, ts, name: "Bash", title, detail: {}, toolUseId: `tu-${ts}`,
    result: "ok", status: "done", stats: null, sidechain: false,
  };
}

export function page(rows: TaskStepRow[], over: Partial<TaskStepsResponse & { applies: true }> = {}): TaskStepsResponse & { applies: true } {
  return {
    applies: true,
    taskId: TASK_ID,
    status: "in_progress",
    live: true,
    hasSession: true,
    attempts: [{ n: 1, startedAt: at(0) }],
    rows,
    updated: [],
    cursor: `v1.${rows.length}`,
    hasMore: false,
    reset: false,
    omitted: 0,
    liveRow: null,
    ...over,
  };
}
