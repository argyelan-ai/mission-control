import { describe, it, expect } from "vitest";
import { deriveStateLine, formatSpan } from "../stateLine";
import { deriveStateCard } from "../stateCard";
import { agentFixture, approvalFixture, commentFixture, runRecordFixture, taskFixture } from "./fixtures";
import { mkRun } from "@/lib/__tests__/headFixtures";
import type { Task } from "@/lib/types";

const NOW = new Date("2026-09-25T12:00:00Z");
const minutesAgo = (m: number) => new Date(NOW.getTime() - m * 60_000).toISOString();
const agents = [agentFixture(), agentFixture({ id: "agent-2", name: "beta" })];

function line(task: Task, opts: Partial<Parameters<typeof deriveStateCard>[0]> = {}, locale = "en") {
  const card = deriveStateCard({ task, approvals: [], comments: [], runRecord: null, ...opts });
  return deriveStateLine({ task, card, agents, locale, now: NOW });
}

describe("formatSpan — one unit, for 'for …' / '… ago'", () => {
  it.each([
    [30, "en", "<1 min"],
    [42 * 60, "en", "42 min"],
    [3 * 3600 + 59 * 60, "en", "3 h"],
    [86400, "en", "1 day"],
    [6 * 86400 + 5 * 3600, "en", "6 days"],
    [86400, "de", "1 Tag"],
    [6 * 86400, "de", "6 Tagen"],
  ])("%i s (%s) → %s", (s, locale, out) => {
    expect(formatSpan(s, locale)).toBe(out);
  });

  it("gives null for no value", () => {
    expect(formatSpan(null)).toBeNull();
  });
});

describe("deriveStateLine — one word, at most one '·', exactly one time", () => {
  it("needs you: the asker and since when", () => {
    const task = taskFixture({ status: "blocked", assigned_agent_id: "agent-1", blocked_at: minutesAgo(42) });
    const l = line(task, { comments: [commentFixture({ comment_type: "blocker", author_agent_id: "agent-2" })] });
    expect(l.tone).toBe("error");
    expect(l.word).toEqual({ ns: "tasks", key: "statusBlocked" });
    expect(l.detail).toEqual({ key: "askedAgoNamed", values: { name: "beta", span: "42 min" } });
  });

  it("needs you via an approval asks in the approval agent's name", () => {
    const task = taskFixture({ status: "waiting", blocked_at: minutesAgo(5) });
    const l = line(task, { approvals: [approvalFixture({ agent_id: "agent-1" })] });
    expect(l.tone).toBe("info");
    expect(l.detail).toEqual({ key: "askedAgoNamed", values: { name: "alpha", span: "5 min" } });
  });

  it("running: the agent and for how long", () => {
    const task = taskFixture({ status: "in_progress", assigned_agent_id: "agent-1", dispatched_at: minutesAgo(42) });
    expect(line(task).detail).toEqual({ key: "forNamed", values: { name: "alpha", span: "42 min" } });
  });

  it("done: who and how long it took — the duration, not the age", () => {
    const task = taskFixture({ status: "done", assigned_agent_id: "agent-1" });
    const rr = runRecordFixture();
    const l = line(task, { runRecord: { ...rr, zeiten: { ...rr.zeiten, dauer_sekunden: 2 * 3600 + 17 * 60 } } });
    expect(l.tone).toBe("online");
    expect(l.detail).toEqual({ key: "tookNamed", values: { name: "alpha", duration: "2 h 17 min" } });
  });

  it("failed: when it ended", () => {
    const task = taskFixture({ status: "failed", completed_at: minutesAgo(18) });
    expect(line(task).detail).toEqual({ key: "endedAgo", values: { span: "18 min" } });
  });

  it("inbox: not started; review: with whom", () => {
    expect(line(taskFixture({ status: "inbox" })).detail).toEqual({ key: "notStarted", values: {} });
    expect(line(taskFixture({ status: "review", assigned_agent_id: "agent-2" })).detail).toEqual({ key: "with", values: { name: "beta" } });
    expect(line(taskFixture({ status: "review" })).detail).toBeNull();
  });

  it("a head run wins: its own state word and time", () => {
    const task = taskFixture({ status: "review" });
    const passed = line(task, { headRun: mkRun({ state: "passed", exited_at: minutesAgo(9) }) });
    expect(passed.word).toEqual({ ns: "heads", key: "state.passed" });
    expect(passed.tone).toBe("online");
    expect(passed.detail).toEqual({ key: "finishedAgo", values: { span: "9 min" } });

    const failed = line(task, { headRun: mkRun({ state: "failed", exited_at: minutesAgo(18) }) });
    expect(failed.tone).toBe("error");
    expect(failed.detail).toEqual({ key: "endedAgo", values: { span: "18 min" } });

    const silent = line(task, { headRun: mkRun({ state: "running", silent_s: 22 * 60 }) });
    expect(silent.tone).toBe("warning");
    expect(silent.detail).toEqual({ key: "silentFor", values: { span: "22 min" } });
  });

  it("German days read as dative after seit/vor", () => {
    const task = taskFixture({ status: "blocked", blocked_at: minutesAgo(6 * 24 * 60) });
    expect(line(task, {}, "de").detail).toEqual({ key: "askedAgo", values: { span: "6 Tagen" } });
  });
});
