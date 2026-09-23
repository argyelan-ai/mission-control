import { describe, it, expect } from "vitest";
import { deriveStateCard } from "../stateCard";
import { approvalFixture, commentFixture, runRecordFixture, taskFixture } from "./fixtures";

describe("deriveStateCard", () => {
  it("blocked with an open approval → NEEDS YOU carrying that approval", () => {
    const task = taskFixture({ status: "blocked", blocked_at: "2026-09-21T08:00:00" });
    const approval = approvalFixture({ task_id: task.id, description: "Card needs a flip to review" });
    const other = approvalFixture({ id: "ap-other", task_id: "someone-else" });
    const card = deriveStateCard({ task, approvals: [other, approval], comments: [], runRecord: null });
    expect(card).toMatchObject({
      kind: "needs_you",
      approval: { id: approval.id },
      reason: "Card needs a flip to review",
      since: "2026-09-21T08:00:00",
    });
  });

  it("waiting without an approval → NEEDS YOU from the latest blocker comment", () => {
    const task = taskFixture({ status: "waiting" });
    const older = commentFixture({ comment_type: "blocker", content: "old question", created_at: "2026-09-20T08:00:00Z" });
    const newer = commentFixture({ id: "c2", comment_type: "blocker", content: "Which branch?", created_at: "2026-09-21T08:00:00Z" });
    const noise = commentFixture({ id: "c3", comment_type: "progress", content: "still here", created_at: "2026-09-22T08:00:00Z" });
    const card = deriveStateCard({ task, approvals: [], comments: [older, noise, newer], runRecord: null });
    expect(card).toMatchObject({ kind: "needs_you", approval: null, reason: "Which branch?" });
  });

  it("ignores resolved approvals", () => {
    const task = taskFixture({ status: "user_test" });
    const resolved = approvalFixture({ task_id: task.id, status: "approved" });
    const card = deriveStateCard({ task, approvals: [resolved], comments: [], runRecord: null });
    expect(card).toMatchObject({ kind: "needs_you", approval: null });
  });

  it("in_progress → RUNNING with last step and heartbeat", () => {
    const task = taskFixture({
      status: "in_progress",
      dispatched_at: "2026-09-22T08:00:00Z",
      ack_at: "2026-09-22T08:01:00Z",
      last_activity_at: "2026-09-22T09:00:00Z",
    });
    const step = commentFixture({ comment_type: "progress", content: "Running the test suite\nsecond line" });
    const card = deriveStateCard({ task, approvals: [], comments: [step], runRecord: null });
    expect(card).toMatchObject({
      kind: "running",
      lastStep: "Running the test suite",
      startedAt: "2026-09-22T08:01:00Z",
      heartbeatAt: "2026-09-22T09:00:00Z",
    });
  });

  it("done → RESULT with resolution, PR fallback, evidence count and duration", () => {
    const task = taskFixture({ status: "done", pr_url: "https://example.test/pr/638", pr_number: 638 });
    const resolution = commentFixture({ comment_type: "resolution", content: "Fixed the scroll.\nAdded a test." });
    const card = deriveStateCard({ task, approvals: [], comments: [resolution], runRecord: runRecordFixture() });
    expect(card).toMatchObject({
      kind: "result",
      resolution: "Fixed the scroll.\nAdded a test.",
      prUrl: "https://example.test/pr/638",
      prNumber: 638,
      evidenceCount: 3,
      durationSeconds: 3 * 86400 + 2 * 3600,
    });
  });

  it("failed → FAILED with the latest error in plain words", () => {
    const task = taskFixture({ status: "failed" });
    const err = commentFixture({ comment_type: "blocker", content: "Build failed: missing module x" });
    const card = deriveStateCard({ task, approvals: [], comments: [err], runRecord: null });
    expect(card).toMatchObject({ kind: "failed", error: "Build failed: missing module x" });
  });

  it("aborted → FAILED as well", () => {
    const card = deriveStateCard({ task: taskFixture({ status: "aborted" }), approvals: [], comments: [], runRecord: null });
    expect(card).toMatchObject({ kind: "failed", error: null });
  });

  it("inbox → no card (facts row says Not started)", () => {
    expect(deriveStateCard({ task: taskFixture({ status: "inbox" }), approvals: [], comments: [], runRecord: null })).toBeNull();
  });
});
