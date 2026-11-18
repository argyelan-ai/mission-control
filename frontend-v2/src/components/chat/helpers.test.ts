import { describe, expect, it } from "vitest";
import type { ChatEvent, HelperSpawn, MessageEvent, NotificationEvent, ToolEvent } from "@/lib/chatTypes";
import {
  collectHelpers,
  helperElapsedSeconds,
  helperReports,
  isHelperSpawn,
  summarizeHelpers,
} from "./helpers";

function spawn(id: string, over: Partial<ToolEvent> = {}): ToolEvent {
  return {
    kind: "tool", uuid: `u-${id}`, ts: "2026-11-13T10:00:00Z", name: "Agent",
    title: "Agent: x", detail: { description: `job ${id}` }, toolUseId: id,
    result: "Async agent launched", status: "done", stats: null, sidechain: false,
    ...over,
  };
}

function launched(id: string, agentId: string, state: HelperSpawn["state"] = "working"): ToolEvent {
  return spawn(id, { helpers: [{ id: agentId, name: null, agent: null, state, durationMs: null }] });
}

function note(over: Partial<NotificationEvent>): NotificationEvent {
  return {
    kind: "notification", uuid: `n-${Math.random()}`, ts: "2026-11-13T10:03:00Z",
    taskId: null, toolUseId: null, status: "completed", summary: null, ...over,
  };
}

function omp(helpers: HelperSpawn[]): ToolEvent {
  return spawn("call_1", { name: "task", detail: { tasks: [] }, helpers });
}

const h = (id: string, state: HelperSpawn["state"] = "working"): HelperSpawn => ({
  id, name: null, agent: "scout", state, durationMs: null,
});

describe("isHelperSpawn", () => {
  it("knows Claude Code's Agent/Task and omp's task tool", () => {
    expect(isHelperSpawn(spawn("a"))).toBe(true);
    expect(isHelperSpawn(spawn("a", { name: "Task" }))).toBe(true);
    expect(isHelperSpawn(omp([h("A")]))).toBe(true);
  });

  it("a lowercase task tool WITHOUT helper data is not guessed into a helper", () => {
    expect(isHelperSpawn(spawn("a", { name: "task" }))).toBe(false);
    expect(isHelperSpawn(spawn("a", { name: "Bash" }))).toBe(false);
  });
});

describe("collectHelpers", () => {
  it("a background launch is working until its report arrives — not done because it has a result", () => {
    const events: ChatEvent[] = [launched("t1", "ag1")];
    const [helper] = collectHelpers([launched("t1", "ag1")], helperReports(events));
    expect(helper.state).toBe("working");
  });

  it("the report closes it, with the measured duration", () => {
    const ev = launched("t1", "ag1");
    const events: ChatEvent[] = [ev, note({ toolUseId: "t1", taskId: "ag1", durationMs: 125000 })];
    const [helper] = collectHelpers([ev], helperReports(events));
    expect(helper.state).toBe("done");
    expect(helper.durationMs).toBe(125000);
  });

  it("a report without tool id is matched by the helper's own id", () => {
    const ev = launched("t1", "ag1");
    const events: ChatEvent[] = [ev, note({ taskId: "ag1", status: "failed" })];
    expect(collectHelpers([ev], helperReports(events))[0].state).toBe("failed");
  });

  it("without a measured duration the report's time ends the span", () => {
    const ev = launched("t1", "ag1");
    const events: ChatEvent[] = [ev, note({ toolUseId: "t1", ts: "2026-11-13T10:03:00Z" })];
    expect(collectHelpers([ev], helperReports(events))[0].durationMs).toBe(180000);
  });

  it("an unknown report status changes nothing", () => {
    const ev = launched("t1", "ag1");
    const events: ChatEvent[] = [ev, note({ toolUseId: "t1", status: "paused-ish" })];
    expect(collectHelpers([ev], helperReports(events))[0].state).toBe("working");
  });

  it("a team member that went idle is done", () => {
    const ev = spawn("t1", { helpers: [{ id: "acfd-1", name: "cfd", agent: null, state: "working", durationMs: null }] });
    const idle: MessageEvent = {
      kind: "message", uuid: "m1", ts: "2026-11-13T10:05:00Z", role: "teammate", teammate: "cfd",
      text: '{"type":"idle_notification","from":"cfd","idleReason":"available"}', model: null, sidechain: false,
    };
    const [helper] = collectHelpers([ev], helperReports([ev, idle]));
    expect(helper.state).toBe("done");
    expect(helper.durationMs).toBe(300000);
  });

  it("omp: one call, several helpers, each closed by its own report", () => {
    const ev = omp([h("A"), h("B"), h("C")]);
    const events: ChatEvent[] = [
      ev,
      note({ taskId: "A", toolUseId: "call_1", status: "completed", durationMs: 70000 }),
      note({ taskId: "B", status: "failed" }),
    ];
    const helpers = collectHelpers([ev], helperReports(events));
    expect(helpers.map((x) => x.state)).toEqual(["done", "failed", "working"]);
    expect(helpers.map((x) => x.name)).toEqual(["A", "B", "C"]);
  });

  it("an omp report on the call id alone never closes ALL its helpers", () => {
    const ev = omp([h("A"), h("B")]);
    const events: ChatEvent[] = [ev, note({ toolUseId: "call_1", taskId: null })];
    expect(collectHelpers([ev], helperReports(events)).map((x) => x.state)).toEqual(["working", "working"]);
  });

  it("an older backend without helper data keeps the old reading of the result", () => {
    const running = spawn("t1", { result: null });
    const finished = spawn("t2");
    const failed = spawn("t3", { status: "error" });
    const states = collectHelpers([running, finished, failed], helperReports([])).map((x) => x.state);
    expect(states).toEqual(["working", "done", "failed"]);
  });
});

describe("summarizeHelpers + elapsed", () => {
  it("counts per state", () => {
    const ev = omp([h("A"), h("B", "done"), h("C", "failed"), h("D")]);
    expect(summarizeHelpers(collectHelpers([ev], helperReports([ev])))).toEqual({
      total: 4, working: 2, done: 1, failed: 1,
    });
  });

  it("a working helper's elapsed time runs from its start; a finished one is fixed", () => {
    const now = new Date("2026-11-13T10:10:00Z");
    const ev = launched("t1", "ag1");
    const [working] = collectHelpers([ev], helperReports([ev]));
    expect(helperElapsedSeconds(working, now)).toBe(600);
    const [done] = collectHelpers([ev], helperReports([ev, note({ toolUseId: "t1", durationMs: 90000 })]));
    expect(helperElapsedSeconds(done, now)).toBe(90);
  });
});
