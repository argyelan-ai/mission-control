/**
 * buildTimelineItems — the end time of an activity run (`endTs`): the time of
 * the agent's own next event that closed it, so the group can say how long
 * the work took. Only the agent's own next step counts — an operator message
 * after a pause would add the pause to the work.
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
