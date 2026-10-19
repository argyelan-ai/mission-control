/**
 * Delivered is not working (follow-up to ADR-097): an assigned inbox task
 * whose model does not answer — or that is delivered and waits for the
 * model's first answer — says so, instead of "not started".
 */
import { describe, it, expect } from "vitest";
import { deriveStateCard } from "../stateCard";
import { deriveStateLine } from "../stateLine";
import { agentFixture, taskFixture } from "./fixtures";
import type { TaskModelWait, TaskStatus } from "@/lib/types";

const wait = (over: Partial<TaskModelWait> = {}): TaskModelWait => ({
  applies: true, delivered: true, ready: false, runtime: "BOX-A :8000",
  reason: "Runtime 'BOX-A :8000' antwortet nicht", ...over,
});

function both(modelWait: TaskModelWait | null, status: TaskStatus = "inbox") {
  const task = taskFixture({ status, assigned_agent_id: "agent-1" });
  const card = deriveStateCard({ task, approvals: [], comments: [], runRecord: null, modelWait });
  return { card, line: deriveStateLine({ task, card, agents: [agentFixture()] }) };
}

describe("model wait on the task detail", () => {
  it("model down → WAITING FOR MODEL card with the runtime, line says it waits for its model", () => {
    const { card, line } = both(wait());
    expect(card).toMatchObject({ kind: "waiting_model", ready: false, runtime: "BOX-A :8000" });
    expect(line.detail).toEqual({ key: "waitingModel", values: {} });
  });

  it("held before delivery (model down, not delivered) → still waiting for the model", () => {
    const { card } = both(wait({ delivered: false }));
    expect(card).toMatchObject({ kind: "waiting_model", ready: false });
  });

  it("delivered, model answers, no first answer yet → the line says so", () => {
    const { card, line } = both(wait({ ready: true, reason: null }));
    expect(card).toMatchObject({ kind: "waiting_model", ready: true });
    expect(line.detail).toEqual({ key: "waitingFirstAnswer", values: {} });
  });

  it("not delivered and the model answers → plain 'not started'", () => {
    const { card, line } = both(wait({ delivered: false, ready: true, reason: null }));
    expect(card).toBeNull();
    expect(line.detail).toEqual({ key: "notStarted", values: {} });
  });

  it("no model-wait data or not applicable → unchanged", () => {
    expect(both(null).card).toBeNull();
    expect(both(wait({ applies: false })).card).toBeNull();
  });

  it("only for inbox tasks", () => {
    expect(both(wait(), "in_progress").card?.kind).toBe("running");
  });
});
