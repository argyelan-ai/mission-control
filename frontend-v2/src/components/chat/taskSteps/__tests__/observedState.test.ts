import { describe, expect, it } from "vitest";
import { headerAliveness, observedStub, statusLineState } from "../observedState";
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

describe("statusLineState — a review run (round-4 review r1 S2)", () => {
  const working: StateEvent = { kind: "state", status: "task_working", prompt: null };
  it("a review run of this agent keeps the task turn, though no card is live", () => {
    // A reviewer reviews a card: it stays in `review` (not live), the probe says a
    // task turn runs. "Ready" here was a new untruth since #963.
    expect(
      statusLineState(working, { tasksShown: true, tasksKnown: true, liveTask: false, reviewRun: true }),
    ).toBe(working);
  });
  it("without one, the ended task turn still reads as ready", () => {
    expect(
      statusLineState(working, { tasksShown: true, tasksKnown: true, liveTask: false, reviewRun: false })?.status,
    ).toBe("idle");
  });
});

describe("headerAliveness — the header badge says what the status line says (round-4 t1 NICE-1/4)", () => {
  const st = (status: StateEvent["status"]): StateEvent => ({ kind: "state", status, prompt: null });
  it("a line that reads ready gets the dot, also while the transcript is fresh", () => {
    // t1: "Bereit" below, "live" above for 12.9 s — the badge read the raw
    // probe, the line the corrected state.
    expect(headerAliveness("active", st("idle"))).toBe("idle");
  });
  it("a running turn is live, also when the chat's own transcript is quiet", () => {
    // t1 NICE-4: the task ran, the band said "in progress", the badge was grey.
    expect(headerAliveness("idle", st("task_working"))).toBe("active");
    expect(headerAliveness("idle", st("working"))).toBe("active");
  });
  it("an ended session stays ended, every other state keeps the transcript's word", () => {
    expect(headerAliveness("ended", st("task_working"))).toBe("ended");
    expect(headerAliveness("idle", st("waiting_model"))).toBe("idle");
    expect(headerAliveness("active", null)).toBe("active");
  });
});
