/**
 * buildTimelineItems — helpers (subagents) of one working stretch share one
 * quiet line, and a helper's own report is shown inside that line, not twice;
 * and the end time of an activity run (`endTs`): the time of the agent's own
 * next event that closed it, so the group can say how long the work took.
 */
import { describe, it, expect } from "vitest";
import { buildTimelineItems } from "./buildTimelineItems";
import type { MessageEvent, ToolEvent } from "@/lib/chatTypes";

function tool(ts: string, overrides: Partial<ToolEvent> = {}): ToolEvent {
  return {
    kind: "tool",
    uuid: `t-${ts}`,
    ts,
    name: "Read",
    title: "Read a.py",
    detail: {},
    toolUseId: `tu-${ts}`,
    result: "ok",
    status: "done",
    stats: null,
    sidechain: false,
    ...overrides,
  };
}

function msg(ts: string, role: MessageEvent["role"]): MessageEvent {
  return { kind: "message", uuid: `m-${ts}`, ts, role, text: "x", model: null, sidechain: false };
}

describe("buildTimelineItems — helpers of one working stretch share one line", () => {
  const agent = (ts: string) => tool(ts, { name: "Agent", title: "Agent: job", toolUseId: `ag-${ts}` });

  it("spawns of one stretch become ONE helpers item, even with tools between them", () => {
    const items = buildTimelineItems([
      msg("2026-08-17T09:59:00Z", "user"),
      agent("2026-08-17T10:00:00Z"),
      tool("2026-08-17T10:00:10Z"),
      agent("2026-08-17T10:00:20Z"),
      agent("2026-08-17T10:00:30Z"),
    ]);
    const helpers = items.filter((i) => i.kind === "helpers");
    expect(helpers).toHaveLength(1);
    expect(helpers[0].kind === "helpers" && helpers[0].events.map((e) => e.ts)).toEqual([
      "2026-08-17T10:00:00Z", "2026-08-17T10:00:20Z", "2026-08-17T10:00:30Z",
    ]);
    // The line stands where the first helper was started.
    expect(items.map((i) => i.kind)).toEqual(["single", "helpers", "single"]);
  });

  it("a reply or the operator's next message starts a new stretch", () => {
    const items = buildTimelineItems([
      agent("2026-08-17T10:00:00Z"),
      msg("2026-08-17T10:01:00Z", "assistant"),
      agent("2026-08-17T10:02:00Z"),
      msg("2026-08-17T10:03:00Z", "user"),
      agent("2026-08-17T10:04:00Z"),
    ]);
    expect(items.filter((i) => i.kind === "helpers")).toHaveLength(3);
  });

  it("a helper's own report (teammate message) does not split the line", () => {
    const items = buildTimelineItems([
      agent("2026-08-17T10:00:00Z"),
      msg("2026-08-17T10:00:30Z", "teammate"),
      agent("2026-08-17T10:01:00Z"),
    ]);
    expect(items.filter((i) => i.kind === "helpers")).toHaveLength(1);
  });

  it("an omp task call with helper data is a helpers item too; without it, a plain tool", () => {
    const withData = tool("2026-08-17T10:00:00Z", {
      name: "task",
      helpers: [{ id: "A", name: null, agent: "scout", state: "working", durationMs: null }],
    });
    const without = tool("2026-08-17T10:05:00Z", { name: "task" });
    const items = buildTimelineItems([withData, msg("2026-08-17T10:01:00Z", "assistant"), without]);
    expect(items.map((i) => i.kind)).toEqual(["helpers", "single", "single"]);
  });
});

describe("buildTimelineItems — a helper's report is shown in its line, not twice", () => {
  it("swallows a report matched only by the helper's own id (omp, or Claude without tool id)", () => {
    const call = tool("2026-08-17T10:00:00Z", {
      name: "task",
      toolUseId: "call_1",
      helpers: [{ id: "ScoutA", name: null, agent: "scout", state: "working", durationMs: null }],
    });
    const report = {
      kind: "notification" as const, uuid: "n1", ts: "2026-08-17T10:02:00Z",
      taskId: "ScoutA", toolUseId: null, status: "completed", summary: null,
    };
    const stranger = { ...report, uuid: "n2", taskId: "bg_7" };
    const items = buildTimelineItems([call, report, stranger]);
    expect(items.map((i) => i.kind)).toEqual(["helpers", "single"]);
    expect(items[1].kind === "single" && items[1].event.uuid).toBe("n2");
  });
});

describe("buildTimelineItems — endTs of an activity run", () => {
  it("takes the time of the assistant reply that closed the run", () => {
    const items = buildTimelineItems([
      msg("2026-08-17T09:59:00Z", "user"),
      tool("2026-08-17T10:00:00Z"),
      tool("2026-08-17T10:01:00Z"),
      msg("2026-08-17T10:02:14Z", "assistant"),
    ]);
    const group = items.find((i) => i.kind === "activity");
    expect(group && group.kind === "activity" && group.endTs).toBe("2026-08-17T10:02:14Z");
  });

  it("takes the time of a delegated job that closed the run", () => {
    const items = buildTimelineItems([
      tool("2026-08-17T10:00:00Z"),
      tool("2026-08-17T10:01:00Z"),
      tool("2026-08-17T10:03:00Z", { name: "Agent", title: "Agent: research" }),
    ]);
    const group = items.find((i) => i.kind === "activity");
    expect(group && group.kind === "activity" && group.endTs).toBe("2026-08-17T10:03:00Z");
  });

  it("has no end while the run is still the newest thing", () => {
    const items = buildTimelineItems([tool("2026-08-17T10:00:00Z"), tool("2026-08-17T10:01:00Z")]);
    const group = items.find((i) => i.kind === "activity");
    expect(group && group.kind === "activity" && group.endTs).toBeNull();
  });

  it("has no end when the operator (or another session) spoke next", () => {
    for (const role of ["user", "teammate"] as const) {
      const items = buildTimelineItems([
        tool("2026-08-17T10:00:00Z"),
        tool("2026-08-17T10:01:00Z"),
        msg("2026-08-17T12:00:00Z", role),
      ]);
      const group = items.find((i) => i.kind === "activity");
      expect(group && group.kind === "activity" && group.endTs).toBeNull();
    }
  });
});
