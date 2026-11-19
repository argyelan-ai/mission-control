import { describe, expect, it } from "vitest";
import { applyStepsPage, runningIds } from "../stepsState";
import { at, page, think, tool } from "./fixtures";

describe("applyStepsPage (spec A §6.1, test 8)", () => {
  it("first page sets rows, cursor, omitted and the etag", () => {
    const a = think(at(1), "Look at the files first");
    const s = applyStepsPage(null, page([a], { omitted: 4, cursor: "v1.9" }), "\"e1\"");
    expect(s.rows).toEqual([a]);
    expect(s.cursor).toBe("v1.9");
    expect(s.omitted).toBe(4);
    expect(s.etag).toBe("\"e1\"");
  });

  it("a delta appends new rows and keeps the old row objects (structural sharing)", () => {
    const a = think(at(1), "Look at the files first");
    const b = tool(at(2), "ls /workspace");
    const s1 = applyStepsPage(null, page([a]), null);
    const s2 = applyStepsPage(s1, page([b], { cursor: "v1.2" }), null);
    expect(s2.rows).toHaveLength(2);
    expect(s2.rows[0]).toBe(s1.rows[0]);
    expect(s2.rows[1]).toBe(b);
    expect(s2.cursor).toBe("v1.2");
    // A delta never resets the count of rows not loaded.
    expect(s2.omitted).toBe(0);
  });

  it("`updated` replaces a row in place (running → done)", () => {
    const a = tool(at(2), "npm test", "running");
    const s1 = applyStepsPage(null, page([a]), null);
    const done = { ...a, st: "done" as const };
    const s2 = applyStepsPage(s1, page([], { updated: [done] }), null);
    expect(s2.rows).toHaveLength(1);
    expect(s2.rows[0]).toEqual(done);
  });

  it("`reset` replaces everything", () => {
    const a = think(at(1), "Old");
    const b = think(at(3), "New");
    const s1 = applyStepsPage(null, page([a]), null);
    const s2 = applyStepsPage(s1, page([b], { reset: true, omitted: 2 }), null);
    expect(s2.rows).toEqual([b]);
    expect(s2.omitted).toBe(2);
  });

  it("an unchanged page returns the previous state object itself", () => {
    const a = think(at(1), "Look at the files first");
    const s1 = applyStepsPage(null, page([a]), "\"e\"");
    const s2 = applyStepsPage(s1, page([], { cursor: s1.cursor }), "\"e\"");
    expect(s2).toBe(s1);
  });

  it("an equal live row keeps its object", () => {
    const live = { block: "thinking" as const, startedAt: at(1), ts: at(2), chars: 300, stale: false };
    const s1 = applyStepsPage(null, page([], { liveRow: live }), null);
    const s2 = applyStepsPage(s1, page([], { liveRow: { ...live } }), null);
    expect(s2.liveRow).toBe(s1.liveRow);
  });

  it("runningIds lists the running tools (the `open` parameter)", () => {
    const a = tool(at(2), "npm test", "running");
    const b = tool(at(3), "ls", "done");
    const s = applyStepsPage(null, page([a, b]), null);
    expect(runningIds(s)).toEqual([a.id]);
  });
});
