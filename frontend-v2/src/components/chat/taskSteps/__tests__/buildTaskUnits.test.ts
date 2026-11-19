import { describe, expect, it } from "vitest";
import { buildTaskUnits, chapterSummary, type TaskUnit, type ToolStep } from "../buildTaskUnits";
import { at, attempt, comment, delivery, text, think, tool } from "./fixtures";
import type { TaskStepRow } from "@/lib/chatTypes";

const ctx0 = (min = 0) => ({ lastMarkMs: Date.parse(at(min)) });
const kinds = (units: TaskUnit[]) => units.map((u) => u.u);

describe("buildTaskUnits (spec A §2.4, test 7)", () => {
  it("groups a thought and its tools into one chapter; tools before the first thought stay plain", () => {
    const rows = [tool(at(1), "ls"), think(at(2), "Read the config"), tool(at(2, 10), "cat a"), tool(at(2, 20), "cat b")];
    const { units } = buildTaskUnits(rows, ctx0(), { detailLevel: "normal" });
    expect(kinds(units)).toEqual(["tool", "chapter"]);
    const ch = units[1] as Extract<TaskUnit, { u: "chapter" }>;
    expect(ch.lead).toBe("Read the config");
    expect(ch.tools.map((t) => t.title)).toEqual(["cat a", "cat b"]);
  });

  it("a text / comment row closes the chapter (units stay in time order)", () => {
    const rows = [think(at(1), "Plan"), tool(at(1, 10), "a"), text(at(2), "Halfway"), tool(at(3), "b"), comment(at(4), "done x")];
    const { units } = buildTaskUnits(rows, ctx0(), { detailLevel: "normal" });
    expect(kinds(units)).toEqual(["chapter", "text", "tool", "comment"]);
  });

  it("collapses consecutive chapters with the same lead into one with a count, tools concatenated, durations summed", () => {
    const rows = [
      think(at(1), "Check the server", { ms: 60_000 }),
      tool(at(1, 5), "curl a"),
      think(at(2), "Check the server", { ms: 120_000 }),
      tool(at(2, 5), "curl b"),
    ];
    const { units } = buildTaskUnits(rows, ctx0(), { detailLevel: "normal" });
    expect(units).toHaveLength(1);
    const ch = units[0] as Extract<TaskUnit, { u: "chapter" }>;
    expect(ch.count).toBe(2);
    expect(ch.ms).toBe(180_000);
    expect(ch.tools.map((t) => t.title)).toEqual(["curl a", "curl b"]);
  });

  it("puts a quiet time mark before the first unit ≥ 15 min after the last shown time, carried across calls", () => {
    const first = buildTaskUnits([think(at(5), "A"), think(at(16), "B")], ctx0(), { detailLevel: "normal" });
    expect(kinds(first.units)).toEqual(["chapter", "mark", "chapter"]);
    // Next segment of the same task: the mark at 16 counts as shown.
    const second = buildTaskUnits([think(at(25), "C"), think(at(32), "D")], first.ctx, { detailLevel: "normal" });
    expect(kinds(second.units)).toEqual(["chapter", "mark", "chapter"]);
  });

  it("an attempt separator counts as a shown time", () => {
    const { units } = buildTaskUnits([attempt(at(20), 2), think(at(30), "A")], ctx0(), { detailLevel: "normal" });
    expect(kinds(units)).toEqual(["attempt", "chapter"]);
  });

  it("Kompakt keeps texts, comments and attempts only — no chapters, tools, delivery, system rows or marks", () => {
    const rows: TaskStepRow[] = [
      delivery(at(0)),
      think(at(1), "A"),
      tool(at(1, 5), "x"),
      text(at(20), "Result"),
      comment(at(40), "note"),
      { id: "1:90", ts: at(41), k: "system", what: "mc ack", request: true },
      attempt(at(50), 2),
    ];
    const { units } = buildTaskUnits(rows, ctx0(), { detailLevel: "compact" });
    expect(kinds(units)).toEqual(["text", "comment", "attempt"]);
  });

  it("merges a permission request with its decision into one system unit (N8)", () => {
    const rows: TaskStepRow[] = [
      { id: "1:5", ts: at(1), k: "system", what: "mc ack", request: true },
      { id: "1:6", ts: at(1, 2), k: "system", what: "mc ack", decision: "allow_always" },
    ];
    const { units } = buildTaskUnits(rows, ctx0(), { detailLevel: "normal" });
    expect(units).toHaveLength(1);
    expect(units[0]).toMatchObject({ u: "system", what: "mc ack", decision: "allow_always" });
  });

  it("Ausführlich opens every chapter; Normal opens only the auto id", () => {
    const a = think(at(1), "A");
    const b = think(at(2), "B");
    const v = buildTaskUnits([a, b], ctx0(), { detailLevel: "verbose" });
    expect(v.units.every((u) => u.u === "chapter" && u.open)).toBe(true);
    const n = buildTaskUnits([a, b], ctx0(), { detailLevel: "normal", autoIds: new Set([b.id]) });
    expect(n.units.map((u) => (u.u === "chapter" ? u.open : null))).toEqual([false, true]);
  });

  it("keeps unit objects whose content did not change (identity reuse)", () => {
    const a = think(at(1), "A");
    const t1 = tool(at(1, 5), "x");
    const first = buildTaskUnits([a, t1], ctx0(), { detailLevel: "normal" });
    const prev = new Map(first.units.map((u) => [u.key, u]));
    const b = think(at(3), "B");
    const second = buildTaskUnits([a, t1, b], ctx0(), { detailLevel: "normal", prev });
    expect(second.units[0]).toBe(first.units[0]);
    expect(second.units[1]).not.toBe(first.units[0]);
  });

  it("chapter line 2: the running tool first, at most two titles, +N, failed and running flags", () => {
    const tools = [tool(at(1), "a"), tool(at(2), "b", "error"), tool(at(3), "c"), tool(at(4), "d", "running")] as ToolStep[];
    expect(chapterSummary(tools)).toEqual({ titles: ["d", "a"], more: 2, failed: true, running: true });
    expect(chapterSummary(tools.slice(0, 1))).toEqual({ titles: ["a"], more: 0, failed: false, running: false });
  });
});
