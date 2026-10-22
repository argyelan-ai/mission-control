import { describe, expect, it } from "vitest";
import { mergeTimeline, type MergeTask, type MergedEntry } from "../mergeTimeline";
import type { TaskUnit } from "../buildTaskUnits";
import { at, chatMsg, chatTool, stub, think, tool, text, TASK_ID } from "./fixtures";
import type { TaskStepRow, TimelineChatEvent } from "@/lib/chatTypes";

function task(rows: TaskStepRow[], over: Partial<MergeTask> = {}): MergeTask {
  return {
    stub: stub(),
    shape: "segments",
    frozenAtMs: null,
    rows,
    liveRow: null,
    omitted: 0,
    hasSession: true,
    detached: false,
    ...over,
  };
}

function merge(chat: TimelineChatEvent[], tasks: MergeTask[], extra: Partial<Parameters<typeof mergeTimeline>[0]> = {}) {
  return mergeTimeline({ chatEvents: chat, tasks, detailLevel: "normal", queued: 0, ...extra });
}

const shape = (entries: MergedEntry[]) =>
  entries.map((e) => (e.kind === "chat" ? "chat" : e.kind === "card" ? "card" : e.first ? "seg" : "cont"));
const units = (e: MergedEntry): TaskUnit[] => (e.kind === "segment" ? e.units : []);

describe("mergeTimeline (spec A §6.2)", () => {
  it("1 · task rows before and after a chat message → two segments with the message between (D2)", () => {
    const r = merge(
      [chatMsg(at(5), "How far are you?")],
      [task([think(at(1), "A"), tool(at(1, 30), "ls"), think(at(8), "B")])],
    );
    expect(shape(r.entries)).toEqual(["seg", "chat", "cont"]);
  });

  it("2 · M2a: a chapter whose tools continue after the message is split there", () => {
    const r = merge(
      [chatMsg(at(11), "Status?")],
      [task([think(at(10), "Run the tests"), tool(at(10, 30), "t1"), tool(at(12), "t2"), tool(at(14), "t3")])],
    );
    expect(shape(r.entries)).toEqual(["seg", "chat", "cont"]);
    const first = units(r.entries[0]).find((u) => u.u === "chapter") as Extract<TaskUnit, { u: "chapter" }>;
    expect(first.tools.map((t) => t.title)).toEqual(["t1"]);
    // Tools after the message: plain tool rows below it, no fake chapter.
    expect(units(r.entries[2]).map((u) => (u.u === "tool" ? u.row.title : u.u))).toEqual(["t2", "t3", "live"]);
  });

  it("3 · M2b: the live row of a task goes below a newer chat message (trailing continuation)", () => {
    const live = { block: "thinking" as const, startedAt: at(9), ts: at(12), chars: 900, stale: false };
    const r = merge([chatMsg(at(11), "Status?")], [task([think(at(10), "A")], { liveRow: live })]);
    expect(shape(r.entries)).toEqual(["seg", "chat", "cont"]);
    expect(units(r.entries[2]).map((u) => u.u)).toEqual(["live"]);
    expect(units(r.entries[0]).some((u) => u.u === "live")).toBe(false);
  });

  it("3b · the live row stays in the last segment when nothing came after it", () => {
    const live = { block: "thinking" as const, startedAt: at(9), ts: at(12), chars: 900, stale: false };
    const r = merge([chatMsg(at(1), "Go")], [task([think(at(10), "A")], { liveRow: live })]);
    expect(shape(r.entries)).toEqual(["chat", "seg"]);
    expect(units(r.entries[1]).map((u) => u.u)).toEqual(["chapter", "live"]);
  });

  it("4 · M2c: a chat tool group with a task row between its events becomes two chat items", () => {
    const r = merge(
      [chatTool(at(1), "a"), chatTool(at(2), "b"), chatTool(at(4), "c"), chatTool(at(5), "d")],
      [task([text(at(3), "Halfway")])],
    );
    // (the live row then trails below the newest chat item)
    expect(shape(r.entries)).toEqual(["chat", "seg", "chat", "cont"]);
    for (const i of [0, 2]) {
      const e = r.entries[i];
      expect(e.kind === "chat" && e.item.kind).toBe("activity");
    }
  });

  it("5 · a finished task not seen live is one card at its start; seen live it stays segments + end line (D6)", () => {
    const done = stub({ status: "done", live: false, endedAt: at(30), outcome: "Report ready" });
    const asCard = merge([chatMsg(at(40), "thanks")], [task([], { stub: done, shape: "card", frozenAtMs: Date.parse(at(30)) })]);
    expect(shape(asCard.entries)).toEqual(["card", "chat"]);
    const seenLive = merge([], [task([think(at(1), "A")], { stub: done, shape: "segments" })]);
    expect(shape(seenLive.entries)).toEqual(["seg"]);
    expect(units(seenLive.entries[0]).map((u) => u.u)).toEqual(["chapter", "end"]);
  });

  it("6 · M4: a card that goes live again stays a card; new rows after its freeze go below as segments", () => {
    const relive = stub({ status: "in_progress", live: true, endedAt: at(30) });
    const rows = [think(at(1), "Old work"), think(at(50), "Rework")];
    const r = merge([chatMsg(at(40), "please redo")], [task(rows, { stub: relive, shape: "card", frozenAtMs: Date.parse(at(30)) })]);
    expect(shape(r.entries)).toEqual(["card", "chat", "cont"]);
    const lead = units(r.entries[2]).map((u) => (u.u === "chapter" ? u.lead : u.u));
    expect(lead).toContain("Rework");
    expect(lead).not.toContain("Old work");
  });

  it("6b · M4: a vanished stub keeps its content and shows the detached row instead of the live row", () => {
    const r = merge([], [task([think(at(1), "A")], { detached: true, liveRow: null })]);
    expect(units(r.entries[0]).map((u) => u.u)).toEqual(["chapter", "detached"]);
  });

  it("a live task without rows yet shows its header with the pending row", () => {
    const r = merge([], [task([], { hasSession: false })]);
    expect(shape(r.entries)).toEqual(["seg"]);
    expect(units(r.entries[0]).map((u) => u.u)).toEqual(["pending"]);
  });

  it("the pending row is left out while the chat says it waits for the model (K3)", () => {
    const r = merge([], [task([], { hasSession: false })], { hidePending: true });
    expect(units(r.entries[0]).map((u) => u.u)).toEqual([]);
  });

  it("queued tasks show under the live row of the newest live task", () => {
    const r = merge([], [task([think(at(1), "A")])], { queued: 2 });
    expect(units(r.entries[0]).map((u) => u.u)).toEqual(["chapter", "live", "queued"]);
  });

  it("names the rows not loaded at the top of the first segment (S12)", () => {
    const r = merge([], [task([think(at(1), "A")], { omitted: 12 })]);
    expect(units(r.entries[0])[0]).toMatchObject({ u: "notLoaded", count: 12 });
  });

  it("ties: a chat event sorts before a task row with the same time", () => {
    const r = merge([chatMsg(at(5), "x")], [task([think(at(5), "A")])]);
    expect(shape(r.entries)).toEqual(["chat", "seg"]);
  });

  it("a row with a missing time inherits the previous row's time", () => {
    const rows = [think(at(1), "A"), { ...tool(at(1), "x"), ts: null } as TaskStepRow];
    const r = merge([chatMsg(at(3), "later")], [task(rows)]);
    expect(shape(r.entries)).toEqual(["seg", "chat", "cont"]);
  });

  it("reuses segment objects whose units did not change (N5)", () => {
    const rows = [think(at(1), "A")];
    const chat = [chatMsg(at(5), "x")];
    const r1 = merge(chat, [task(rows)]);
    const r2 = merge(chat, [task(rows)], { prev: r1 });
    expect(r2.entries[0]).toBe(r1.entries[0]);
    expect(r2.entries[1]).toBe(r1.entries[1]);
  });

  it("segment keys are stable and carry the task id", () => {
    const r = merge([], [task([think(at(1), "A")])]);
    expect(r.entries[0].key.startsWith(`task:${TASK_ID}:seg:`)).toBe(true);
  });

  it("countNewer counts chat items and task units newer than a time", () => {
    const r = merge(
      [chatMsg(at(1), "a"), chatMsg(at(9), "b")],
      [task([think(at(2), "A"), think(at(8), "B"), think(at(10), "C")])],
    );
    expect(r.countNewer(Date.parse(at(7)))).toBe(3);
    expect(r.newestTs).toBe(Date.parse(at(10)));
  });
});

/*
 * Live 22.10.2026: a task finished (review) BEFORE the operator wrote; the
 * band after his message held only the end line, yet said "· continues".
 * The header of a later segment follows the task's real state at that point.
 */
describe("mergeTimeline — continuation head follows the task's state", () => {
  const segHeads = (entries: MergedEntry[]) =>
    entries.map((e) => (e.kind === "segment" ? `${e.first ? "seg" : "cont"}:${e.head}` : e.kind));

  it("a task that ended before the operator's message keeps its end line above it — no second band", () => {
    const review = stub({ status: "review", live: false, endedAt: at(1, 22), outcome: "Bench written" });
    const r = merge(
      [chatMsg(at(3), "Looks good?"), chatMsg(at(3, 20), "Yes, see the result.", "assistant")],
      [task([think(at(0, 10), "Run the bench"), tool(at(1, 10), "bench.sh")], { stub: review })],
    );
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "chat"]);
    expect(units(r.entries[0]).map((u) => u.u)).toEqual(["chapter", "end"]);
  });

  it("an end line that arrives after an interleaved message stands below it under the title only — never 'continues'", () => {
    const review = stub({ status: "review", live: false, endedAt: at(6) });
    const r = merge([chatMsg(at(5), "How far are you?")], [task([think(at(1), "A"), tool(at(2), "t1")], { stub: review })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:title"]);
    expect(units(r.entries[2]).map((u) => u.u)).toEqual(["end"]);
  });

  it("a finished task without an end time keeps the end line below the newer message, title only", () => {
    const done = stub({ status: "done", live: false, endedAt: null });
    const r = merge([chatMsg(at(5), "x")], [task([think(at(1), "A")], { stub: done })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:title"]);
  });

  it.each(["done", "review", "failed", "aborted"])("%s after the message: title-only band with the end line", (status) => {
    const s = stub({ status, live: false, endedAt: at(9) });
    const r = merge([chatMsg(at(5), "x")], [task([think(at(1), "A")], { stub: s })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:title"]);
  });

  it.each(["done", "review", "failed", "aborted"])("%s before the message: end line in the last segment", (status) => {
    const s = stub({ status, live: false, endedAt: at(4) });
    const r = merge([chatMsg(at(5), "x")], [task([think(at(1), "A")], { stub: s })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat"]);
    expect(units(r.entries[0]).map((u) => u.u)).toEqual(["chapter", "end"]);
  });

  it("work after the message still says 'continues', also when the task has finished since", () => {
    const done = stub({ status: "done", live: false, endedAt: at(9) });
    const r = merge([chatMsg(at(5), "x")], [task([think(at(1), "A"), think(at(8), "B")], { stub: done })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:continues"]);
    expect(units(r.entries[2]).map((u) => u.u)).toEqual(["chapter", "end"]);
  });

  it("a running task's live row below the message says 'continues'", () => {
    const live = { block: "thinking" as const, startedAt: at(9), ts: at(12), chars: 900, stale: false };
    const r = merge([chatMsg(at(11), "Status?")], [task([think(at(10), "A")], { liveRow: live })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:continues"]);
  });

  it.each(["blocked", "waiting", "user_test"])("%s (waits for the operator) below the message: title + its state, not 'continues'", (status) => {
    const s = stub({ status, live: true });
    const r = merge([chatMsg(at(5), "x")], [task([think(at(1), "A")], { stub: s })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:full"]);
  });

  it("a delivered task not started yet: the pending row below the message is not 'continues'", () => {
    const s = stub({ status: "inbox", live: true, startedAt: null, startKey: at(0) });
    const r = merge([chatMsg(at(5), "x")], [task([], { stub: s, hasSession: false })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:title"]);
    expect(units(r.entries[2]).map((u) => u.u)).toEqual(["pending"]);
  });

  it("a vanished task below the message: title only, never 'continues'", () => {
    const r = merge([chatMsg(at(5), "x")], [task([think(at(1), "A")], { detached: true })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:title"]);
  });

  it("a card task finished again after its rework: the end line band says the title only", () => {
    const s = stub({ status: "review", live: false, endedAt: at(60) });
    const r = merge(
      [chatMsg(at(40), "please redo"), chatMsg(at(55), "and?")],
      [task([think(at(1), "Old"), think(at(50), "Rework")], { stub: s, shape: "card", frozenAtMs: Date.parse(at(30)), endLine: true })],
    );
    expect(segHeads(r.entries)).toEqual(["card", "chat", "cont:continues", "chat", "cont:title"]);
  });

  it("the head is part of a segment's identity: a state flip rebuilds the entry (N5)", () => {
    const rows = [think(at(1), "A")];
    const chat = [chatMsg(at(5), "x")];
    const r1 = merge(chat, [task(rows, { stub: stub({ status: "in_progress" }) })]);
    const r2 = merge(chat, [task(rows, { stub: stub({ status: "blocked" }) })], { prev: r1 });
    expect(segHeads(r2.entries)[2]).toBe("cont:full");
    expect(r2.entries[2]).not.toBe(r1.entries[2]);
  });
});
