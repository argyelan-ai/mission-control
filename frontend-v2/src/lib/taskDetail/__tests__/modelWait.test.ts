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
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";

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

  // Review UI-5 (K10): the chat says "Waiting for the model" in the warning
  // tone; the task line uses the same words and, while the model does not
  // answer, the same tone. A delivered task whose model answers stays calm.
  it("model down → the line warns like the chat does", () => {
    expect(both(wait()).line.tone).toBe("warning");
  });

  it("model answers, first answer pending → the line stays in the inbox tone", () => {
    expect(both(wait({ ready: true, reason: null })).line.tone).toBe("muted");
  });
});

describe("model wait wording (review UI-4 / UI-5)", () => {
  const line = (m: typeof en) => m.tasks.detail.line;

  it("one term for one thing: the task line says what the chat says", () => {
    expect(line(en).waitingModel).toBe("waiting for the model");
    expect(line(de).waitingModel).toBe("wartet auf das Modell");
    // Same words; only the sentence start differs (German nouns keep their capital).
    expect(en.sessions.status.waitingForModel.toLowerCase()).toBe(line(en).waitingModel.toLowerCase());
    expect(de.sessions.status.waitingForModel.toLowerCase()).toBe(line(de).waitingModel.toLowerCase());
    expect(line(en).waitingFirstAnswer).toContain("waiting for the model");
    expect(line(de).waitingFirstAnswer).toContain("wartet auf das Modell");
  });

  // 390 px phone, one truncated line (TaskStateCard): the old German
  // sentence (58 characters with its state word) lost exactly its point
  // ("…des M…"); the English one (55) just fit. 48 leaves room on both.
  it.each([["en", en], ["de", de]] as const)("every new state sentence fits a phone line (%s)", (_l, m) => {
    for (const key of ["waitingModel", "waitingFirstAnswer"] as const) {
      const sentence = `${m.tasks.statusInbox} · ${line(m)[key]}`;
      expect(sentence.length, sentence).toBeLessThanOrEqual(48);
    }
  });
});
