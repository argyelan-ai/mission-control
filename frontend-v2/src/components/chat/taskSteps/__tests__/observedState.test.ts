import { describe, expect, it } from "vitest";
import { observedStub, statusLineState } from "../observedState";
import type { TaskStepsState } from "../stepsState";
import type { StateEvent, TaskStepRow } from "@/lib/chatTypes";
import { at, attempt, delivery, stub, think, tool, TASK_ID } from "./fixtures";

function steps(rows: TaskStepRow[], over: Partial<TaskStepsState> = {}): TaskStepsState {
  return {
    taskId: TASK_ID, status: "in_progress", live: true, hasSession: true, attempts: [], rows,
    cursor: "v1", liveRow: null, omitted: 0, etag: null, ...over,
  };
}

const inbox = () => stub({ status: "inbox", live: true, startedAt: null });

describe("observedStub (round-3 t1 NICE-2/3)", () => {
  it("newer steps replace the stub's status and liveness", () => {
    const s = observedStub(stub(), steps([], { status: "review", live: false }), 1000, 2000);
    expect(s.status).toBe("review");
    expect(s.live).toBe(false);
  });

  it("older steps never win — the stub was fetched after them", () => {
    const base = stub({ status: "review", live: false });
    expect(observedStub(base, steps([], { status: "in_progress" }), 2000, 1000)).toBe(base);
  });

  it("delivered + work rows after the delivery → in progress, started at the first work row", () => {
    const s = observedStub(inbox(), steps([delivery(at(0)), think(at(1), "ACK"), tool(at(1, 5), "mc ack")], { status: "inbox" }), 0, 1);
    expect(s.status).toBe("in_progress");
    expect(s.startedAt).toBe(at(1));
  });

  it("rows of an earlier attempt do not count — a re-delivered task waits again", () => {
    const base = inbox();
    expect(observedStub(base, steps([think(at(1), "old"), attempt(at(5), 2)], { status: "inbox" }), 0, 1)).toBe(base);
  });

  it("a live row alone also means the agent works", () => {
    const live = { block: "thinking" as const, startedAt: at(1), ts: at(1, 3), chars: 10, stale: false };
    expect(observedStub(inbox(), steps([], { status: "inbox", liveRow: live }), 0, 1).status).toBe("in_progress");
  });

  it("nothing to change → the same object (memo-friendly)", () => {
    const base = stub();
    expect(observedStub(base, steps([think(at(1), "x")]), 0, 1)).toBe(base);
    expect(observedStub(base, null, 0, 0)).toBe(base);
  });
});

describe("statusLineState", () => {
  const working: StateEvent = { kind: "state", status: "task_working", prompt: null };
  it("a task turn with no live task left reads as ready", () => {
    expect(statusLineState(working, { tasksShown: true, tasksKnown: true, liveTask: false })?.status).toBe("idle");
  });
  it("keeps the probe's word while a task is live, the stubs are unknown, or the chat has no task sections", () => {
    expect(statusLineState(working, { tasksShown: true, tasksKnown: true, liveTask: true })).toBe(working);
    expect(statusLineState(working, { tasksShown: true, tasksKnown: false, liveTask: false })).toBe(working);
    expect(statusLineState(working, { tasksShown: false, tasksKnown: true, liveTask: false })).toBe(working);
  });
  it("never touches another state", () => {
    const waiting: StateEvent = { kind: "state", status: "waiting_model", prompt: null };
    expect(statusLineState(waiting, { tasksShown: true, tasksKnown: true, liveTask: false })).toBe(waiting);
  });
});
