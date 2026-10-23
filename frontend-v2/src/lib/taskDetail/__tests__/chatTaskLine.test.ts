import { describe, expect, it } from "vitest";
import { deriveChatTaskLine, deriveStateLine } from "../stateLine";
import { taskFixture } from "./fixtures";
import type { ChatTaskStub } from "@/lib/chatTypes";

const NOW = new Date("2026-09-25T12:00:00Z");
const ago = (m: number) => new Date(NOW.getTime() - m * 60_000).toISOString();

function stub(over: Partial<ChatTaskStub>): ChatTaskStub {
  return {
    taskId: "00000000-0000-4000-8000-000000000001", title: "T", status: "in_progress", live: true,
    deliveredAt: null, startedAt: null, startKey: null, endedAt: null, assignee: null, outcome: null,
    deliverables: 0, ...over,
  };
}

describe("deriveChatTaskLine — the task section's state line (spec A §2.5, S4)", () => {
  it("names the state with the task words and no agent", () => {
    const l = deriveChatTaskLine(stub({ startedAt: ago(42) }), "en", NOW);
    expect(l.word).toEqual({ ns: "tasks", key: "detail.line.task.in_progress" });
    expect(l.detail).toEqual({ key: "for", values: { span: "42 min" } });
  });

  // Same keys, same formatters as the task detail's line: running / result /
  // failed must give the same tone and detail as `deriveStateLine` for the
  // same facts (the agent name is left out on both sides).
  it("running equals deriveStateLine", () => {
    const task = taskFixture({ status: "in_progress", assigned_agent_id: null });
    const ref = deriveStateLine({ task, card: { kind: "running", lastStep: null, startedAt: ago(42), heartbeatAt: null }, agents: [], now: NOW });
    const mine = deriveChatTaskLine(stub({ startedAt: ago(42) }), "en", NOW);
    expect({ tone: mine.tone, detail: mine.detail }).toEqual({ tone: ref.tone, detail: ref.detail });
  });

  it.each(["en", "de"])("result equals deriveStateLine (%s)", (locale) => {
    const task = taskFixture({ status: "done", assigned_agent_id: null });
    const card = {
      kind: "result" as const, resolution: null, resolutionIsFallback: false, prUrl: null, prNumber: null,
      evidenceCount: null, durationSeconds: 64 * 60,
    };
    const ref = deriveStateLine({ task, card, agents: [], locale, now: NOW });
    const mine = deriveChatTaskLine(stub({ status: "done", live: false, startedAt: ago(70), endedAt: ago(6) }), locale, NOW);
    expect({ tone: mine.tone, detail: mine.detail }).toEqual({ tone: ref.tone, detail: ref.detail });
    expect(mine.detail).toEqual({ key: "took", values: { duration: "1 h 4 min" } });
  });

  it.each(["en", "de"])("failed equals deriveStateLine (%s)", (locale) => {
    const task = taskFixture({ status: "failed", assigned_agent_id: null, completed_at: ago(18), updated_at: ago(18) });
    const ref = deriveStateLine({ task, card: { kind: "failed", error: null }, agents: [], locale, now: NOW });
    const mine = deriveChatTaskLine(stub({ status: "failed", live: false, endedAt: ago(18) }), locale, NOW);
    expect({ tone: mine.tone, detail: mine.detail }).toEqual({ tone: ref.tone, detail: ref.detail });
  });

  it("inbox: delivered waits for the first answer; not delivered is not started", () => {
    expect(deriveChatTaskLine(stub({ status: "inbox", deliveredAt: ago(1) }), "en", NOW).detail).toEqual({ key: "waitingFirstAnswer", values: {} });
    expect(deriveChatTaskLine(stub({ status: "inbox" }), "en", NOW).detail).toEqual({ key: "notStarted", values: {} });
  });

  // Round-7 re-check t1 NICE-3: after a rework the line named the time from
  // the FIRST start ("took 9 min", the wait for the review included).
  it("after a rework the line names the current run, and which run it is", () => {
    const review = deriveChatTaskLine(
      stub({ status: "review", live: false, startedAt: ago(9), startKey: ago(9), runStartedAt: ago(1), runs: 2,
        endedAt: new Date(NOW.getTime() - 60_000 + 48_000).toISOString() }),
      "en", NOW,
    );
    expect(review.detail).toEqual({ key: "tookRun", values: { duration: "48 s", n: "2" } });
    const running = deriveChatTaskLine(stub({ startedAt: ago(30), runStartedAt: ago(3), runs: 3 }), "en", NOW);
    expect(running.detail).toEqual({ key: "forRun", values: { span: "3 min", n: "3" } });
  });

  it("one run (or an older backend without the fields) keeps the plain line", () => {
    const one = deriveChatTaskLine(stub({ status: "done", live: false, startedAt: ago(70), runStartedAt: ago(70), runs: 1, endedAt: ago(6) }), "en", NOW);
    expect(one.detail).toEqual({ key: "took", values: { duration: "1 h 4 min" } });
  });

  it("a handed-over task names its holder", () => {
    const l = deriveChatTaskLine(stub({ status: "in_progress", live: false, assignee: "agent-b" }), "en", NOW);
    expect(l.detail).toEqual({ key: "with", values: { name: "agent-b" } });
  });
});
