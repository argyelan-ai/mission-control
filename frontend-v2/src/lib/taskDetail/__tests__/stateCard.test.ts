import { describe, it, expect } from "vitest";
import { deriveStateCard } from "../stateCard";
import { approvalFixture, commentFixture, runRecordFixture, taskFixture } from "./fixtures";
import { mkRun } from "@/lib/__tests__/headFixtures";
import type { HeadState } from "@/lib/heads";

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

  it("done without a resolution falls back to the latest agent note and says so", () => {
    const task = taskFixture({ status: "done" });
    const note = commentFixture({ comment_type: "message", content: "Merged and verified live.", created_at: "2026-09-21T09:00:00Z" });
    const reflection = commentFixture({ id: "c2", comment_type: "reflection", content: "lessons", created_at: "2026-09-21T10:00:00Z" });
    const system = commentFixture({ id: "c3", author_type: "system", comment_type: "reflection", content: "auto", created_at: "2026-09-21T11:00:00Z" });
    const card = deriveStateCard({ task, approvals: [], comments: [note, reflection, system], runRecord: null });
    expect(card).toMatchObject({ kind: "result", resolution: "Merged and verified live.", resolutionIsFallback: true });
  });

  it("done with a resolution uses it and is not a fallback", () => {
    const task = taskFixture({ status: "done" });
    const res = commentFixture({ comment_type: "resolution", content: "Fixed.", created_at: "2026-09-20T09:00:00Z" });
    const later = commentFixture({ id: "c2", comment_type: "message", content: "later note", created_at: "2026-09-21T09:00:00Z" });
    const card = deriveStateCard({ task, approvals: [], comments: [res, later], runRecord: null });
    expect(card).toMatchObject({ kind: "result", resolution: "Fixed.", resolutionIsFallback: false });
  });

  it("failed prefers the blocker over a later harmless system note", () => {
    const task = taskFixture({ status: "failed" });
    const blocker = commentFixture({ comment_type: "blocker", content: "Build failed", created_at: "2026-09-21T08:00:00Z" });
    const sys = commentFixture({ id: "c2", author_type: "system", comment_type: "system_notify", content: "Delivered to lead", created_at: "2026-09-21T09:00:00Z" });
    const card = deriveStateCard({ task, approvals: [], comments: [blocker, sys], runRecord: null });
    expect(card).toMatchObject({ kind: "failed", error: "Build failed" });
  });

  it("failed without a blocker takes the last warning event before any system note", () => {
    const task = taskFixture({ status: "failed" });
    const sys = commentFixture({ id: "c2", author_type: "system", comment_type: "system_notify", content: "Delivered to lead" });
    const rr = runRecordFixture({
      schritte: [{ ts: "2026-09-21T08:00:00", quelle: "ereignis", actor_label: null, changed_by: null, text: "Dispatch failed: runtime unreachable" }],
    });
    const card = deriveStateCard({ task, approvals: [], comments: [sys], runRecord: rr });
    expect(card).toMatchObject({ kind: "failed", error: "Dispatch failed: runtime unreachable" });
  });
});

describe("deriveStateCard — head run (B5)", () => {
  const base = { approvals: [], comments: [], runRecord: null };

  it.each<[HeadState, string | null, string]>([
    ["starting", null, "stop"],
    ["running", null, "stop"],
    ["needs_you", null, "answer"],
    ["passed", "https://github.com/o/r/pull/712", "open_pr"],
    ["failed", null, "restart"],
    ["stopped", null, "restart"],
  ])("%s → one main action", (state, prUrl, action) => {
    const task = taskFixture({ status: "in_progress" });
    const card = deriveStateCard({ ...base, task, headRun: mkRun({ state, pr_url: prUrl }) });
    expect(card).toMatchObject({ kind: "head", mainAction: action });
  });

  it("passed on a scratch repo (no PR possible) → branch_pushed, never a dead Open PR", () => {
    const task = taskFixture({ status: "in_progress" });
    const scratch = mkRun({ state: "passed", reason: "scratch_branch_pushed", pr_url: null });
    expect(deriveStateCard({ ...base, task, headRun: scratch })).toMatchObject({ kind: "head", mainAction: "branch_pushed" });
    // a PR still wins when there is one
    const withPr = mkRun({ state: "passed", reason: "scratch_branch_pushed", pr_url: "https://github.com/o/r/pull/7" });
    expect(deriveStateCard({ ...base, task, headRun: withPr })).toMatchObject({ mainAction: "open_pr" });
  });

  it("a head run wins over the task status card", () => {
    const task = taskFixture({ status: "blocked" });
    const card = deriveStateCard({ ...base, task, headRun: mkRun({ state: "failed", reason: "time_limit" }) });
    expect(card?.kind).toBe("head");
  });

  it("silent > 15 min → warn tone but still running", () => {
    const task = taskFixture({ status: "in_progress" });
    const silent = deriveStateCard({ ...base, task, headRun: mkRun({ state: "running", silent_s: 22 * 60 }) });
    expect(silent).toMatchObject({ kind: "head", silentWarn: true, mainAction: "stop", run: { state: "running" } });
    const fresh = deriveStateCard({ ...base, task, headRun: mkRun({ state: "running", silent_s: 14 * 60 }) });
    expect(fresh).toMatchObject({ kind: "head", silentWarn: false });
  });

  it("no head run → the old task cards stay unchanged", () => {
    const task = taskFixture({ status: "in_progress" });
    expect(deriveStateCard({ ...base, task, headRun: null })?.kind).toBe("running");
  });
});
