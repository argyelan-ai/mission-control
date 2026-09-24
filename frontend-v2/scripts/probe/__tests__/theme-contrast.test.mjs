// --theme switch and the text-contrast heuristic (WCAG AA) of the UI probe.
import { describe, expect, it } from "vitest";
import { contrastFindings, contrastRatio, renderTextPixel, toHex } from "../lib/contrast.mjs";
import { evaluateState } from "../lib/findings.mjs";
import { parseArgs, renderMarkdown } from "../lib/report.mjs";
import { themeInitScript } from "../lib/context.mjs";

const vp = { w: 1440, h: 900 };
const W = [255, 255, 255, 1];
const sample = (over = {}) => ({ text: "Hello", fg: [0, 0, 0, 1], chain: [{ bg: W, op: 1 }], fontPx: 14, weight: 400, ...over });

describe("--theme / --contrast arguments", () => {
  it("defaults to no forced theme and no contrast check", () => {
    const o = parseArgs([]);
    expect(o.theme).toBeNull();
    expect(o.contrast).toBe(false);
  });
  it("takes light, dark and system", () => {
    for (const t of ["light", "dark", "system"]) expect(parseArgs(["--theme", t]).theme).toBe(t);
    expect(parseArgs(["--contrast"]).contrast).toBe(true);
  });
  it("rejects anything else", () => {
    expect(() => parseArgs(["--theme", "blue"])).toThrow(/--theme/);
    expect(() => parseArgs(["--theme"])).toThrow(/missing/);
  });
});

describe("themeInitScript", () => {
  const fakeLs = () => {
    const s = {};
    return { s, setItem: (k, v) => (s[k] = v) };
  };
  it("stores the theme choice on the app origin only", () => {
    const a = fakeLs();
    themeInitScript({ key: "mc_theme", theme: "light", origin: "http://app", _loc: { origin: "http://app" }, _ls: a });
    expect(a.s).toEqual({ mc_theme: "light" });
    const b = fakeLs();
    themeInitScript({ key: "mc_theme", theme: "light", origin: "http://app", _loc: { origin: "http://evil" }, _ls: b });
    expect(b.s).toEqual({});
  });
  it("never throws when storage is blocked", () => {
    expect(() => themeInitScript({ key: "k", theme: "dark", origin: "o", _loc: { origin: "o" }, _ls: { setItem: () => { throw new Error("blocked"); } } })).not.toThrow();
  });
});

describe("theme check", () => {
  it("reports a page whose theme was not applied (base state only)", () => {
    const st = { viewport: vp, scrollWidth: 1440, layers: [], theme: "dark", mainTextLength: 500, mainInteractive: 3 };
    expect(evaluateState(st, { isBase: true, expectedTheme: "light" })[0]).toMatchObject({ type: "theme", severity: "high" });
    expect(evaluateState(st, { isBase: true, expectedTheme: "dark" })).toEqual([]);
    expect(evaluateState(st, { isBase: true })).toEqual([]);
  });
});

describe("contrast maths", () => {
  it("black on white is 21:1, same colour is 1:1", () => {
    expect(contrastRatio([0, 0, 0], [255, 255, 255])).toBeCloseTo(21, 5);
    expect(contrastRatio([120, 120, 120], [120, 120, 120])).toBeCloseTo(1, 5);
  });
  it("matches a known pair (#767676 on white ≈ 4.54)", () => {
    expect(contrastRatio([0x76, 0x76, 0x76], [255, 255, 255])).toBeCloseTo(4.54, 2);
  });
  it("composites translucent text and backgrounds", () => {
    // 50 % black text on white → mid grey
    const px = renderTextPixel([0, 0, 0, 0.5], [{ bg: W, op: 1 }]);
    expect(px.text.map(Math.round)).toEqual([128, 128, 128]);
    expect(px.bg).toEqual([255, 255, 255]);
  });
  it("applies an ancestor's opacity to text and its background alike", () => {
    // card (black bg) at 50 % opacity over a white page: text white, bg grey
    const px = renderTextPixel([255, 255, 255, 1], [{ bg: [0, 0, 0, 1], op: 0.5 }, { bg: W, op: 1 }]);
    expect(px.bg.map(Math.round)).toEqual([128, 128, 128]);
    expect(px.text.map(Math.round)).toEqual([255, 255, 255]);
  });
  it("falls back to a white canvas when no layer is opaque", () => {
    const px = renderTextPixel([0, 0, 0, 1], [{ bg: [0, 0, 0, 0], op: 1 }]);
    expect(px.bg).toEqual([255, 255, 255]);
  });
  it("hex output", () => {
    expect(toHex([255, 0, 16.4])).toBe("#FF0010");
  });
});

describe("contrastFindings", () => {
  it("passes AA text and flags normal text under 4.5:1 as high", () => {
    const ok = contrastFindings([sample()]);
    expect(ok.findings).toEqual([]);
    expect(ok.checked).toBe(1);
    const bad = contrastFindings([sample({ fg: [150, 150, 150, 1], text: "Faint" })]);
    expect(bad.findings).toHaveLength(1);
    expect(bad.findings[0]).toMatchObject({ type: "contrast", severity: "high" });
    expect(bad.findings[0].message).toMatch(/#969696 on #FFFFFF = 2\.9\d?:1 \(AA needs 4\.5:1\)/);
    expect(bad.findings[0].detail.examples).toEqual(["Faint"]);
  });
  it("large text only needs 3:1 (24px, or 18.66px bold)", () => {
    const grey = [140, 140, 140, 1]; // ≈ 3.4:1 on white
    expect(contrastFindings([sample({ fg: grey, fontPx: 24 })]).findings).toEqual([]);
    expect(contrastFindings([sample({ fg: grey, fontPx: 19, weight: 700 })]).findings).toEqual([]);
    expect(contrastFindings([sample({ fg: grey, fontPx: 19, weight: 400 })]).findings).toHaveLength(1);
  });
  it("groups one colour pair into one finding with a few examples", () => {
    const s = ["a", "b", "c", "d", "e"].map((t) => sample({ fg: [150, 150, 150, 1], text: t }));
    const r = contrastFindings(s);
    expect(r.findings).toHaveLength(1);
    expect(r.findings[0].detail.examples).toEqual(["a", "b", "c"]);
    expect(r.findings[0].detail.count).toBe(5);
  });
  it("skips samples over background images/gradients and counts them", () => {
    const r = contrastFindings([sample({ fg: [150, 150, 150, 1], chain: [{ bg: [0, 0, 0, 0], op: 1, img: true }, { bg: W, op: 1 }] })]);
    expect(r.findings).toEqual([]);
    expect(r.uncertain).toBe(1);
    expect(r.checked).toBe(0);
  });
  it("ignores an image far behind an opaque card", () => {
    const r = contrastFindings([sample({ chain: [{ bg: W, op: 1 }, { bg: [0, 0, 0, 0], op: 1, img: true }] })]);
    expect(r.uncertain).toBe(0);
    expect(r.checked).toBe(1);
  });
  it("skips text that is (nearly) faded out — hidden or mid-transition, not a readable colour", () => {
    const r = contrastFindings([sample({ fg: [150, 150, 150, 1], chain: [{ bg: [0, 0, 0, 0], op: 0.05 }, { bg: W, op: 1 }] })]);
    expect(r).toMatchObject({ checked: 0, findings: [], hidden: 1 });
    // a real dim (opacity 0.6) is still judged
    expect(contrastFindings([sample({ fg: [150, 150, 150, 1], chain: [{ bg: [0, 0, 0, 0], op: 0.6 }, { bg: W, op: 1 }] })]).findings).toHaveLength(1);
  });
  it("skips invisible text (fully transparent)", () => {
    expect(contrastFindings([sample({ fg: [0, 0, 0, 0] })]).checked).toBe(0);
  });
  it("evaluateState turns state.contrast into findings", () => {
    const f = evaluateState({ viewport: vp, scrollWidth: 1440, layers: [], contrast: [sample({ fg: [150, 150, 150, 1] })] });
    expect(f.map((x) => x.type)).toEqual(["contrast"]);
  });
});

describe("report", () => {
  it("names the forced theme and the contrast totals", () => {
    const md = renderMarkdown({
      base: "b",
      startedAt: "s",
      finishedAt: "f",
      widths: [1440],
      theme: "light",
      contrast: true,
      skippedRoutes: [],
      writes: { blocked: 0, passed: 0, websocketsRefused: 0, samples: [] },
      pages: [{ path: "/", width: 1440, counts: { opened: 1, candidates: 1, contrastChecked: 40, contrastUncertain: 2 }, findings: [], states: [] }],
    });
    expect(md).toContain("- Theme: light");
    expect(md).toContain("40 text element(s) checked, 2 skipped");
  });
});

describe("crashedPage", () => {
  it("turns a crashed browser tab into a high finding instead of ending the run", async () => {
    const { crashedPage } = await import("../lib/report.mjs");
    const r = crashedPage({ path: "/bench", pattern: "/bench" }, 1440, new Error("page.evaluate: Target crashed \n  at x"));
    expect(r).toMatchObject({ path: "/bench", width: 1440, crashed: true, counts: {}, states: [] });
    expect(r.findings[0]).toMatchObject({ type: "crash", severity: "high", page: "/bench", width: 1440 });
    expect(r.findings[0].message).toBe("probe could not finish this page: page.evaluate: Target crashed");
  });
});

describe("storage key", () => {
  it("is the app's own key", async () => {
    const { THEME_STORAGE_KEY } = await import("../lib/context.mjs");
    const app = await import("../../../src/lib/themeScript.ts");
    expect(THEME_STORAGE_KEY).toBe(app.THEME_STORAGE_KEY);
  });
});
