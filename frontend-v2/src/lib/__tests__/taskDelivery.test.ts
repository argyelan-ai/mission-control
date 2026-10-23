import { describe, it, expect } from "vitest";
import { taskMatchesAgent } from "../taskDelivery";

describe("taskMatchesAgent — the agent filter keeps what the agent delivered", () => {
  it("matches the assigned agent", () => {
    expect(taskMatchesAgent({ assigned_agent_id: "a", delivered_by_agent_id: null }, "a")).toBe(true);
  });
  it("matches the agent that delivered a card now in the operator's review (nobody assigned)", () => {
    expect(taskMatchesAgent({ assigned_agent_id: null, delivered_by_agent_id: "a" }, "a")).toBe(true);
  });
  it("matches both the reviewer holding the card and its builder", () => {
    const t = { assigned_agent_id: "rev", delivered_by_agent_id: "a" };
    expect(taskMatchesAgent(t, "rev")).toBe(true);
    expect(taskMatchesAgent(t, "a")).toBe(true);
  });
  it("does not match anyone else", () => {
    expect(taskMatchesAgent({ assigned_agent_id: "b", delivered_by_agent_id: "c" }, "a")).toBe(false);
    expect(taskMatchesAgent({ assigned_agent_id: null }, "a")).toBe(false);
  });
});
