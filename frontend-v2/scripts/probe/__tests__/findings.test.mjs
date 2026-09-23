import { describe, expect, it } from "vitest";
import {
  coveredVerdict,
  dedupeFindings,
  evaluateState,
  horizontalOverflow,
  isEmptyLayer,
  isProbeNoise,
  offViewportSides,
  rankFindings,
  smallTargets,
} from "../lib/findings.mjs";
import { parseArgs, renderMarkdown, slug } from "../lib/report.mjs";

const vp = { w: 390, h: 844 };
const layer = (over = {}) => ({
  kind: "menu",
  label: "Menu",
  rect: { left: 10, top: 10, right: 200, bottom: 300 },
  samples: [],
  text: "Item",
  media: false,
  interactive: 2,
  clippedText: 0,
  targets: [],
  ...over,
});

describe("geometry heuristics", () => {
  it("offViewportSides names every side that sticks out", () => {
    expect(offViewportSides({ left: 0, top: 0, right: 390, bottom: 844 }, vp)).toEqual([]);
    expect(offViewportSides({ left: -20, top: 5, right: 420, bottom: 900 }, vp)).toEqual(["left", "right", "bottom"]);
  });
  it("coveredVerdict flags a layer when 2+ on-screen points hit something else", () => {
    expect(coveredVerdict([{ inViewport: true, inside: true }, { inViewport: true, inside: false }]).flagged).toBe(false);
    expect(coveredVerdict([{ inViewport: true, inside: false }, { inViewport: true, inside: false }, { inViewport: false, inside: false }])).toEqual({ covered: 2, onScreen: 2, flagged: true });
  });
  it("horizontalOverflow ignores sub-pixel noise", () => {
    expect(horizontalOverflow(391, 390)).toBe(0);
    expect(horizontalOverflow(430, 390)).toBe(40);
  });
  it("smallTargets only judges phone widths", () => {
    const t = [{ label: "x", w: 30, h: 30 }, { label: "ok", w: 44, h: 44 }];
    expect(smallTargets(t, 390).map((x) => x.label)).toEqual(["x"]);
    expect(smallTargets(t, 1440)).toEqual([]);
  });
  it("isEmptyLayer needs text, media or controls", () => {
    expect(isEmptyLayer({ text: " ", media: false, interactive: 0 })).toBe(true);
    expect(isEmptyLayer({ text: "", media: true, interactive: 0 })).toBe(false);
  });
});

describe("evaluateState", () => {
  it("reports a menu that sticks out and hides behind the page", () => {
    const f = evaluateState({
      viewport: vp,
      scrollWidth: 390,
      layers: [layer({ rect: { left: 300, top: 10, right: 520, bottom: 300 }, samples: [{ inViewport: true, inside: false }, { inViewport: true, inside: false }] })],
    });
    expect(f.map((x) => x.type).sort()).toEqual(["covered", "offscreen"]);
  });
  it("does not flag inline expansions that run below the fold", () => {
    const f = evaluateState({ viewport: vp, scrollWidth: 390, layers: [layer({ kind: "inline", rect: { left: 0, top: 700, right: 390, bottom: 1600 } })] });
    expect(f).toEqual([]);
  });
  it("reports sideways scroll, clipping, empty layers, esc and small targets", () => {
    const f = evaluateState({
      viewport: vp,
      scrollWidth: 450,
      escClosed: false,
      layers: [layer({ text: "", interactive: 0, clippedBy: "div.card", clippedText: 1, targets: [{ label: "x", w: 20, h: 20 }] })],
    });
    // esc is not judged at phone width (no hardware key), see review.test.mjs
    expect(f.map((x) => x.type).sort()).toEqual(["clipped", "clipped-text", "empty", "h-scroll", "small-target"]);
  });
  it("tells a real Escape failure from a synthetic-only close", () => {
    const base = { viewport: { w: 1440, h: 900 }, scrollWidth: 1440, layers: [] };
    expect(evaluateState({ ...base, escClosed: false })[0]).toMatchObject({ type: "esc", severity: "medium" });
    expect(evaluateState({ ...base, escClosed: "synthetic-only" })[0]).toMatchObject({ type: "esc", severity: "medium" });
    expect(evaluateState({ ...base, escClosed: "synthetic-only" })[0].message).toMatch(/synthetic/);
    expect(evaluateState({ ...base, escClosed: false })[0].message).not.toMatch(/synthetic/);
    expect(evaluateState({ ...base, escClosed: true })).toEqual([]);
    expect(evaluateState({ ...base, escClosed: null })).toEqual([]);
  });
  it("ignores backdrops and small targets in inline re-renders", () => {
    const f = evaluateState({ viewport: vp, scrollWidth: 390, layers: [layer({ backdrop: true, text: "", interactive: 0, rect: { left: 0, top: 0, right: 390, bottom: 844 } }), layer({ kind: "inline", targets: [{ label: "x", w: 10, h: 10 }] })] });
    expect(f).toEqual([]);
  });
  it("page-level checks only run on the base state", () => {
    const st = { viewport: vp, scrollWidth: 390, layers: [], targets: [{ label: "i", w: 24, h: 24 }], mainTextLength: 0, mainInteractive: 0 };
    expect(evaluateState(st)).toEqual([]);
    expect(evaluateState(st, { isBase: true }).map((x) => x.type).sort()).toEqual(["empty", "small-target"]);
  });
  it("drops console noise caused by the probe's own write lock", () => {
    expect(isProbeNoise("Failed to load resource: net::ERR_FAILED")).toBe(true);
    expect(isProbeNoise("WebSocket connection to 'ws://localhost/ws' failed: x")).toBe(true);
    const f = evaluateState({ viewport: vp, scrollWidth: 390, layers: [], consoleErrors: ["net::ERR_FAILED", "TypeError: x is undefined"] });
    expect(f.map((x) => x.message)).toEqual(["TypeError: x is undefined"]);
  });
});

describe("ranking + dedupe", () => {
  it("sorts high first and merges repeats with a count", () => {
    const a = { page: "/t", width: 390, type: "h-scroll", severity: "high", message: "m", shot: "a.png" };
    const b = { page: "/t", width: 390, type: "small-target", severity: "low", message: "s" };
    const d = dedupeFindings([b, a, { ...a, shot: "b.png" }]);
    expect(d.find((x) => x.type === "h-scroll")).toMatchObject({ count: 2, shots: ["a.png", "b.png"] });
    expect(rankFindings(d).map((x) => x.severity)).toEqual(["high", "low"]);
  });
});

describe("cli + report", () => {
  it("parses args with defaults outside the repo", () => {
    const o = parseArgs(["--route", "/tasks,/agents", "--width", "390", "--base", "http://localhost/"], new Date("2026-01-02T03:04:05Z"));
    expect(o).toMatchObject({ base: "http://localhost", routes: ["/tasks", "/agents"], widths: [390] });
    expect(o.out).toMatch(/mc-ui-probe[\\/]2026-01-02T03-04-05$/);
    expect(parseArgs([]).widths).toEqual([1440, 390]);
    expect(() => parseArgs(["--width", "abc"])).toThrow();
    expect(() => parseArgs(["--bogus"])).toThrow(/unknown/);
    expect(() => parseArgs(["--out"])).toThrow(/missing/);
  });
  it("slug is filesystem safe", () => {
    expect(slug("Mehr … Optionen / Ä")).toBe("mehr-optionen-a");
    expect(slug("")).toBe("x");
  });
  it("report shows opened X of Y and the write counter", () => {
    const md = renderMarkdown({
      base: "http://localhost",
      startedAt: "s",
      finishedAt: "f",
      widths: [390],
      skippedRoutes: [],
      writes: { blocked: 3, passed: 0, websocketsRefused: 1, samples: ["POST http://localhost/api/v1/x"] },
      pages: [{ path: "/tasks", width: 390, counts: { opened: 4, candidates: 10, guarded: 2, noChange: 3, navigated: 0, failed: 1, nestedOpened: 0 }, findings: [], states: [{ status: "guarded", label: "Delete", reason: "guarded:delete" }] }],
    });
    expect(md).toContain("| `/tasks` | 390 | 4 | 10 | 40% |");
    expect(md).toContain("**0 passed**");
    expect(md).toContain('"Delete" (delete)');
  });
});
