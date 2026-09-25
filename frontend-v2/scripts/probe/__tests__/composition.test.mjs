import { describe, expect, it } from "vitest";
import {
  HEAD_LIMITS,
  clusterEdges,
  colorKey,
  compositionBudget,
  isUppercaseLabel,
  measuredBuild,
  parseBudgetArgs,
} from "../lib/composition.mjs";

const n = (text, over = {}) => ({
  text,
  fontSize: 13,
  fontWeight: 400,
  color: "rgb(163, 163, 163)",
  left: 16,
  lineStart: true,
  textTransform: "none",
  ...over,
});

// A calm header: title, one state sentence, one next step (variant A shape).
const calm = [
  n("[HEAD-PROBE] Fix add() in scratch repo", { fontSize: 20, fontWeight: 600, color: "rgb(242, 242, 242)" }),
  n("Fehlgeschlagen", { color: "rgb(250, 73, 66)", left: 30, lineStart: false }),
  n("· 3. Versuch vor 18 min", { lineStart: false, left: 140 }),
  n("Ohne Pull Request beendet.", { fontSize: 15, color: "rgb(201, 201, 201)" }),
];

// Today's header (needs-you shot): eyebrow, uppercase kicker, label grid.
const busy = [
  n("AUFGABE · 630BDD4B · AD-HOC", { fontSize: 10, textTransform: "uppercase" }),
  n("Chat scrollt weiter seitwaerts — #634 hat den echten Fall des…", { fontSize: 18 }),
  n("BRAUCHT DICH · BLOCKED 42 MIN", { fontSize: 10, color: "rgb(250, 73, 66)", left: 30 }),
  n("Rex fragt:", { fontSize: 14, left: 30 }),
  n("STATUS", { fontSize: 10, left: 20, textTransform: "uppercase" }),
  n("AGENT", { fontSize: 10, left: 150, lineStart: false, textTransform: "uppercase" }),
  n("ZEIT", { fontSize: 10, left: 260, lineStart: false, textTransform: "uppercase" }),
  n("Blocked", { fontSize: 14, left: 26, color: "rgb(250, 73, 66)" }),
  n("5 T 16 h", { fontSize: 16, left: 260, lineStart: false, fontWeight: 500 }),
  n("Claude: nicht erfasst", { fontSize: 15, left: 262, color: "rgb(138, 138, 138)", fontWeight: 600 }),
  n("Claude: nicht erfasst", { fontSize: 15, left: 262, color: "rgb(138, 138, 138)" }),
];

describe("composition budget", () => {
  it("passes a calm header", () => {
    const r = compositionBudget(calm, { boxDepth: 0, items: 4 });
    expect(r.findings).toEqual([]);
    expect(r.passed).toBe(true);
    expect(r.metrics.fontSizes).toEqual([13, 15, 20]);
    expect(r.metrics.leftEdges).toEqual([16]);
  });

  it("flags the crowded header of today with rule ids", () => {
    const r = compositionBudget(busy, { boxDepth: 2, items: 9 });
    const rules = r.findings.map((f) => f.rule);
    expect(r.passed).toBe(false);
    for (const id of ["K3", "K4", "K5", "K7", "K8", "K9"]) expect(rules).toContain(id);
    expect(r.metrics.uppercase).toHaveLength(5);
    expect(r.metrics.tinyText).toHaveLength(5);
    expect(r.metrics.repeats).toEqual([{ text: "Claude: nicht erfasst", count: 2 }]);
  });

  it("does not count short repeats like names or separators", () => {
    const r = compositionBudget([n("Rex"), n("Rex"), n("·"), n("·")]);
    expect(r.metrics.repeats).toEqual([]);
  });

  it("allows the title to be cut, but not a second text", () => {
    expect(compositionBudget([n("A very long title…", { fontSize: 20 })]).findings).toEqual([]);
    const two = compositionBudget([n("A very long title…", { fontSize: 20 }), n("Branch mc-head/2026…")]);
    expect(two.findings.map((f) => f.rule)).toEqual(["K3"]);
  });

  it("ignores empty text and unknown extras", () => {
    const r = compositionBudget([n("  "), ...calm]);
    expect(r.metrics.textPieces).toBe(4);
    expect(r.metrics.boxDepth).toBeNull();
  });

  it("limits can be loosened for other regions", () => {
    const loose = { ...HEAD_LIMITS, maxUppercase: 1 };
    expect(compositionBudget([n("LÄUFE")], {}, loose).findings).toEqual([]);
    expect(compositionBudget([n("LÄUFE")]).findings.map((f) => f.rule)).toEqual(["K7"]);
  });
});

describe("helpers", () => {
  it("isUppercaseLabel: transform or written in capitals, not numbers", () => {
    expect(isUppercaseLabel(n("Status", { textTransform: "uppercase" }))).toBe(true);
    expect(isUppercaseLabel(n("AUFGABE · C6C8553E"))).toBe(true);
    expect(isUppercaseLabel(n("PR #632"))).toBe(false); // two letters: an abbreviation, not a label
    expect(isUppercaseLabel(n("17 h 29 min"))).toBe(false);
    expect(isUppercaseLabel(n("42"))).toBe(false);
  });
  it("clusterEdges merges sub-pixel and 2 px neighbours only", () => {
    expect(clusterEdges([16, 17, 18, 30, 30.5, 36])).toEqual([16, 30, 36]);
  });
  it("colorKey drops alpha so tints of one token count once", () => {
    expect(colorKey("rgba(242, 242, 242, 0.6)")).toBe("#f2f2f2");
    expect(colorKey("rgb(250, 73, 66)")).toBe("#fa4942");
  });
});

describe("budget cli helpers", () => {
  it("parseBudgetArgs requires url and region and defaults both widths", () => {
    expect(() => parseBudgetArgs(["--region", "main"])).toThrow(/--url/);
    expect(() => parseBudgetArgs(["--url", "http://localhost:3001/tasks"])).toThrow(/--region/);
    const o = parseBudgetArgs(["--url", "http://localhost:3001/tasks", "--region", "[data-region=task-head]", "--strict"]);
    expect(o.widths).toEqual([390, 1440]);
    expect(o.strict).toBe(true);
    expect(() => parseBudgetArgs(["--url", "x", "--region", "y", "--width", "90"])).toThrow(/between/);
    expect(() => parseBudgetArgs(["--url", "x", "--region", "y", "--bogus"])).toThrow(/unknown/);
  });
  it("measuredBuild warns when the deployed build is measured instead of the branch", () => {
    expect(measuredBuild("http://localhost:3001/tasks").kind).toBe("dev-server");
    expect(measuredBuild("http://localhost/tasks").warning).toMatch(/deployed/);
    expect(measuredBuild("file:///tmp/a.html").warning).toBe("");
  });
});
