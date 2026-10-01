/**
 * The inbox count — one function decides what waits on the operator; the
 * Inbox page renders its buckets and the phone tab bar shows its count.
 */
import { describe, it, expect } from "vitest";
import { badgeLabel, deriveInbox, openHeadQuestions, type InboxInput } from "../inbox";
import type { Agent, Approval, Task, TaskComment } from "../types";
import { mkRun } from "./headFixtures";

const reviewer = { id: "rev", name: "Reviewer", role: "reviewer", role_canonical: "reviewer" } as unknown as Agent;
const dev = { id: "dev", name: "Dev", role: "developer", role_canonical: "developer" } as unknown as Agent;

function task(o: Partial<Task> = {}): Task {
  return {
    id: "t1",
    title: "Card",
    status: "review",
    assigned_agent_id: null,
    human_review_required: false,
    dispatch_intent: "review_handoff",
    ...o,
  } as unknown as Task;
}
const approval = (id: string) => ({ id, status: "pending" }) as unknown as Approval;
const comment = (author: string | null) => ({ id: `c-${author}`, author_agent_id: author }) as unknown as TaskComment;

function input(o: Partial<InboxInput> = {}): InboxInput {
  return { approvals: [], reviewTasks: [], agents: [reviewer, dev], commentsByTask: {}, headRuns: [], ...o };
}

describe("deriveInbox", () => {
  it("is zero when nothing waits", () => {
    expect(deriveInbox(input()).count).toBe(0);
  });

  it("counts reviews + approvals + open head questions", () => {
    const r = deriveInbox(
      input({
        reviewTasks: [task({ id: "a" })], // nobody holds it → operator
        approvals: [approval("x"), approval("y")],
        headRuns: [mkRun({ run_id: "r1", task_id: "h1", state: "needs_you" })],
      }),
    );
    expect(r.reviews.map((t) => t.id)).toEqual(["a"]);
    expect(r.approvals).toHaveLength(2);
    expect(r.headQuestions.map((h) => h.run_id)).toEqual(["r1"]);
    expect(r.count).toBe(4);
  });

  it("does not count a review held by a reviewer agent", () => {
    const r = deriveInbox(input({ reviewTasks: [task({ id: "a", assigned_agent_id: "rev" })] }));
    expect(r.agentReviews.map((t) => t.id)).toEqual(["a"]);
    expect(r.count).toBe(0);
  });

  it("counts an operator review with an agent only once that agent commented", () => {
    const t = task({ id: "a", assigned_agent_id: "dev" });
    const before = deriveInbox(input({ reviewTasks: [t], commentsByTask: { a: [comment("someone-else")] } }));
    expect(before.count).toBe(0);
    expect(before.waitingForReview).toBe(1);

    const unloaded = deriveInbox(input({ reviewTasks: [t] }));
    expect(unloaded.count).toBe(0);

    const after = deriveInbox(input({ reviewTasks: [t], commentsByTask: { a: [comment("dev")] } }));
    expect(after.count).toBe(1);
    expect(after.waitingForReview).toBe(0);
  });

  it("counts a self-review stall without waiting for a comment", () => {
    const t = task({ id: "a", assigned_agent_id: "rev", dispatch_intent: "root" });
    expect(deriveInbox(input({ reviewTasks: [t] })).count).toBe(1);
  });

  it("counts an explicit human review even when a reviewer agent holds it", () => {
    const t = task({ id: "a", assigned_agent_id: "rev", human_review_required: true, dispatch_intent: "root" });
    expect(deriveInbox(input({ reviewTasks: [t] })).count).toBe(1);
  });
});

describe("openHeadQuestions", () => {
  it("only the newest run per task counts — an answered question is not open", () => {
    const asked = mkRun({ run_id: "old", task_id: "t", state: "needs_you", created_at: "2026-09-30T10:00:00Z" });
    const continued = mkRun({ run_id: "new", task_id: "t", state: "running", created_at: "2026-09-30T11:00:00Z" });
    expect(openHeadQuestions([asked, continued])).toEqual([]);
    expect(openHeadQuestions([continued, asked])).toEqual([]);
  });

  it("ignores runs whose card was deleted and runs in other states", () => {
    const runs = [
      mkRun({ run_id: "a", task_id: "1", state: "needs_you", task_deleted: true }),
      mkRun({ run_id: "b", task_id: "2", state: "passed" }),
      mkRun({ run_id: "c", task_id: "3", state: "failed" }),
      mkRun({ run_id: "d", task_id: "4", state: "needs_you" }),
    ];
    expect(openHeadQuestions(runs).map((r) => r.run_id)).toEqual(["d"]);
  });
});

describe("badgeLabel", () => {
  it("hides at zero and caps at 99+", () => {
    expect(badgeLabel(0)).toBeNull();
    expect(badgeLabel(-1)).toBeNull();
    expect(badgeLabel(17)).toBe("17");
    expect(badgeLabel(99)).toBe("99");
    expect(badgeLabel(100)).toBe("99+");
  });
});
