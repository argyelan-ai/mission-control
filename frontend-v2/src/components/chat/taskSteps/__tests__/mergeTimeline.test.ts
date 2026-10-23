import { describe, expect, it } from "vitest";
import { mergeTimeline, type MergeTask, type MergedEntry } from "../mergeTimeline";
import type { TaskUnit } from "../buildTaskUnits";
import { at, chatMsg, chatTool, stub, think, tool, text, TASK_ID, TASK_ID_2 } from "./fixtures";
import { buildTaskUnits } from "../buildTaskUnits";
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
    // The band stays open past the chat's own tools (no operator message):
    // its live row stays IN it — no empty trailing head below (r1 SHOULD-1).
    expect(shape(r.entries)).toEqual(["chat", "seg", "chat"]);
    expect(units(r.entries[1]).map((u) => u.u)).toEqual(["text", "live"]);
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

  it("work after the message: 'continues' while the task runs, the title alone once it has finished (r1 MUST-2)", () => {
    const rows = [think(at(1), "A"), think(at(8), "B")];
    const running = merge([chatMsg(at(5), "x")], [task(rows)]);
    expect(segHeads(running.entries)).toEqual(["seg:full", "chat", "cont:continues"]);
    const done = stub({ status: "done", live: false, endedAt: at(9) });
    const r = merge([chatMsg(at(5), "x")], [task(rows, { stub: done })], { prev: running });
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:title"]);
    expect(units(r.entries[2]).map((u) => u.u)).toEqual(["chapter", "end"]);
  });

  it.each(["review", "done", "failed", "aborted"])(
    "%s: no band with work rows says 'continues' any more — live, without a reload (r1 MUST-2)",
    (status) => {
      const rows = [think(at(1), "A"), tool(at(2), "t1"), think(at(6), "B"), tool(at(7), "t2"), think(at(9), "C")];
      const chat = [chatMsg(at(5), "x"), chatMsg(at(8), "y")];
      const r = merge(chat, [task(rows, { stub: stub({ status, live: false, endedAt: at(10) }) })]);
      expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:title", "chat", "cont:title"]);
    },
  );

  it.each(["blocked", "waiting", "user_test"])("%s with work rows below the message: title only (r1 MUST-2)", (status) => {
    const r = merge([chatMsg(at(5), "x")], [task([think(at(1), "A"), think(at(6), "B")], { stub: stub({ status }) })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:title"]);
  });

  it("a running task's live row below the message says 'continues'", () => {
    const live = { block: "thinking" as const, startedAt: at(9), ts: at(12), chars: 900, stale: false };
    const r = merge([chatMsg(at(11), "Status?")], [task([think(at(10), "A")], { liveRow: live })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:continues"]);
  });

  it.each(["blocked", "waiting", "user_test"])(
    "%s (waits for the operator) below the message: title only — the state line stays in the first band (K3)",
    (status) => {
      const s = stub({ status, live: true });
      const r = merge([chatMsg(at(5), "x")], [task([think(at(1), "A")], { stub: s })]);
      expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:title"]);
    },
  );

  it("a task waiting for the operator stays title-only even while a live row shows", () => {
    const live = { block: "text" as const, ts: at(6), text: "Which one?", stale: false };
    const r = merge([chatMsg(at(5), "x")], [task([think(at(1), "A")], { stub: stub({ status: "waiting" }), liveRow: live })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:title"]);
  });

  it("a delivered task still in the inbox whose agent already writes: 'continues' (NICE-1)", () => {
    const s = stub({ status: "inbox", live: true });
    const live = { block: "thinking" as const, startedAt: at(6), ts: at(7), chars: 120, stale: false };
    const r = merge([chatMsg(at(5), "x")], [task([think(at(1), "A")], { stub: s, liveRow: live })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:continues"]);
  });

  it("an end time equal to the next message's time: the chat goes first, the end line below it", () => {
    const review = stub({ status: "review", live: false, endedAt: at(5) });
    const r = merge([chatMsg(at(5), "x")], [task([think(at(1), "A")], { stub: review })]);
    expect(segHeads(r.entries)).toEqual(["seg:full", "chat", "cont:title"]);
    expect(units(r.entries[2]).map((u) => u.u)).toEqual(["end"]);
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
    expect(segHeads(r.entries)).toEqual(["card", "chat", "cont:title", "chat", "cont:title"]);
  });

  it("the head is part of a segment's identity: a state flip rebuilds the entry (N5)", () => {
    const rows = [think(at(1), "A")];
    const chat = [chatMsg(at(5), "x")];
    const r1 = merge(chat, [task(rows, { stub: stub({ status: "in_progress" }) })]);
    const r2 = merge(chat, [task(rows, { stub: stub({ status: "blocked" }) })], { prev: r1 });
    expect(segHeads(r1.entries)[2]).toBe("cont:continues");
    expect(segHeads(r2.entries)[2]).toBe("cont:title");
    expect(r2.entries[2]).not.toBe(r1.entries[2]);
  });
});

/*
 * Round-1 user test 22.10.2026 (t1 SHOULD-1, r2 M3): while a task ran, the
 * chat session's own tool lines landed between its rows, and every one of
 * them cut the task into a new band with the same head — 6 to 10 bands, an
 * empty head at the bottom, after a reload ONE card. Only the operator's own
 * message splits a band; the chat's other lines wait below the band.
 */
describe("mergeTimeline — one band until the operator writes", () => {
  const live = { block: "thinking" as const, startedAt: at(9), ts: at(9, 30), chars: 300, stale: false };
  const kinds = (entries: MergedEntry[]) =>
    entries.map((e) => (e.kind === "segment" ? `${e.first ? "seg" : "cont"}:${e.head}` : e.kind === "chat" ? `chat:${e.item.kind}` : e.kind));

  it("the chat's own tool lines between task rows do not split the band; they stand below it", () => {
    const rows = [think(at(1), "Set up"), tool(at(2), "mkdir"), tool(at(4), "Edit index.html"), tool(at(6), "ls"), think(at(8), "Check")];
    const chat = [chatTool(at(3), "ls deliverables"), chatTool(at(5), "cat index.html"), chatTool(at(7), "mc task-get")];
    const r = merge(chat, [task(rows, { liveRow: live })]);
    expect(kinds(r.entries)).toEqual(["seg:full", "chat:activity"]);
    expect(units(r.entries[0]).map((u) => u.u)).toEqual(["chapter", "chapter", "live"]);
    const chatItem = r.entries[1];
    expect(chatItem.kind === "chat" && chatItem.item.kind === "activity" && chatItem.item.events.length).toBe(3);
  });

  it("only an operator message splits: one band before it, at most one 'continues' band after it", () => {
    const rows = [think(at(1), "A"), tool(at(2), "t1"), tool(at(4), "t2"), think(at(6), "B"), tool(at(8), "t3"), tool(at(10), "t4")];
    const chat = [chatTool(at(3), "c1"), chatMsg(at(5), "How far?"), chatTool(at(7), "c2"), chatMsg(at(9), "Fast fertig.", "assistant"), chatTool(at(11), "c3")];
    const r = merge(chat, [task(rows, { liveRow: { ...live, ts: at(12) } })]);
    expect(kinds(r.entries)).toEqual(["seg:full", "chat:single", "chat:single", "cont:continues", "chat:single", "chat:single", "chat:single"]);
    expect(r.entries.filter((e) => e.kind === "segment")).toHaveLength(2);
    // The live row belongs to the open band, never to an empty band below.
    expect(units(r.entries[3]).map((u) => u.u)).toEqual(["chapter", "live"]);
  });

  it("no empty head at the bottom: a live task's tail stays in its band when only chat tools follow", () => {
    const r = merge([chatTool(at(5), "c1"), chatTool(at(6), "c2")], [task([think(at(1), "A")], { liveRow: live })]);
    expect(kinds(r.entries)).toEqual(["seg:full", "chat:activity"]);
    expect(units(r.entries[0]).map((u) => u.u)).toEqual(["chapter", "live"]);
  });

  it("a task that ends while only chat tools follow keeps its end line in its band", () => {
    const review = stub({ status: "review", live: false, endedAt: at(8) });
    const r = merge([chatTool(at(5), "c1"), chatTool(at(9), "c2")], [task([think(at(1), "A"), tool(at(6), "t")], { stub: review })]);
    expect(kinds(r.entries)).toEqual(["seg:full", "chat:activity"]);
    expect(units(r.entries[0]).map((u) => u.u)).toEqual(["chapter", "end"]);
  });

  it("live and reloaded build the same steps: the band's units equal one build over all rows (r1 NICE-3)", () => {
    // Live, the chat's tool lines used to cut the chapter: "Edit index.html +33"
    // became a lone tool row with its diff badge, while the card after the
    // reload folds it into the chapter — two different pictures of one row.
    const rows = [
      think(at(1), "Create the dir, then write index.html"),
      tool(at(2), "mkdir"),
      tool(at(4), "Edit index.html", "done", { name: "edit", stats: { additions: 33, deletions: 0 } }),
      tool(at(6), "ls -la"),
    ];
    const r = merge([chatTool(at(3), "c1"), chatTool(at(5), "c2")], [task(rows, { stub: stub({ status: "review", live: false, endedAt: at(7) }) })]);
    const band = units(r.entries[0]).filter((u) => u.u !== "end");
    const card = buildTaskUnits(rows, { lastMarkMs: Date.parse(at(0, 5)) }, { detailLevel: "normal" }).units;
    expect(band).toEqual(card);
    expect(band).toHaveLength(1);
  });

  it("another task's row closes the band; the held chat lines go between the two tasks", () => {
    const other = task([think(at(4), "Other")], { stub: stub({ taskId: TASK_ID_2, title: "Other task", status: "done", live: false, endedAt: at(5) }) });
    const r = merge([chatTool(at(3), "c1")], [task([think(at(1), "A"), think(at(6), "B")], { stub: stub({ status: "review", live: false, endedAt: at(7) }) }), other]);
    expect(r.entries.map((e) => (e.kind === "segment" ? e.taskId.slice(-1) : e.kind))).toEqual(["1", "chat", "2", "1"]);
  });

  it("an older operator message before the task's first row changes nothing (it is no split)", () => {
    const r = merge([chatMsg(at(0, 1), "Go"), chatTool(at(3), "c1")], [task([think(at(1), "A"), think(at(4), "B")])]);
    expect(kinds(r.entries)).toEqual(["chat:single", "seg:full", "chat:single"]);
  });
});

describe("mergeTimeline — a band never stands empty (round-3 t1 NICE-1)", () => {
  it("the live row of a new band under the operator's message is marked alone", () => {
    const r = merge([chatMsg(at(11), "Status?")], [task([think(at(10), "A")])]);
    const tail = units(r.entries[2]);
    expect(tail.map((u) => u.u)).toEqual(["live"]);
    expect((tail[0] as Extract<TaskUnit, { u: "live" }>).alone).toBe(true);
  });

  // PR #963 review NICE-1: a live task that waits for the operator has
  // nobody thinking — the band must not say "Thinking…".
  it.each(["blocked", "waiting", "user_test"] as const)("a %s task's new band is not marked alone", (status) => {
    const r = merge([chatMsg(at(11), "Status?")], [task([think(at(10), "A")], { stub: stub({ status }) })]);
    const live = units(r.entries[2]).find((u) => u.u === "live") as Extract<TaskUnit, { u: "live" }> | undefined;
    expect(live?.alone).toBeFalsy();
  });

  it("next to work rows the live row is not alone", () => {
    const r = merge([chatMsg(at(1), "Go")], [task([think(at(10), "A")])]);
    const live = units(r.entries[1]).find((u) => u.u === "live") as Extract<TaskUnit, { u: "live" }>;
    expect(live.alone).toBeFalsy();
  });

  it("stays the same object across merges (no re-render of the band)", () => {
    const tasks = [task([think(at(10), "A")])];
    const first = merge([chatMsg(at(11), "Status?")], tasks);
    const again = merge([chatMsg(at(11), "Status?")], tasks, { prev: first });
    expect(again.entries[2]).toBe(first.entries[2]);
  });
});
