import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { BASELINE, SRC_DIR, compare, countRules, countThemeWeights, scan } from "../ratchet.mjs";

describe("design ratchet — rules", () => {
  it("counts free values and leaves tokens alone", () => {
    const src = `
      <div className="text-[13px] text-sm px-2.5 px-3 gap-[10px] tracking-[0.14em] leading-[1.3] mt-5 -mt-5"
           style={{ fontSize: 12, padding: 6, marginTop: "10px", color: "#FA4942", background: "rgba(0,0,0,.5)" }} />
      const ok = "text-xs p-4 gap-2 mt-6 tracking-wide C.textMuted";
      const pr = "PR #632"; // a number, not a colour
      const hex8 = "#EBE8DE1A"; const short = "#fff";`;
    expect(countRules(src)).toEqual({
      "font-free": 1,
      "font-inline": 1,
      "space-offscale": 3, // px-2.5, mt-5, -mt-5
      "space-free": 1,
      "space-inline": 2,
      "color-literal": 4, // #FA4942, rgba(, #EBE8DE1A, #fff — not "#632"
      "tracking-free": 2,
    });
  });

  it("does not read unrelated words as spacing", () => {
    expect(countRules(`const x = "step-5 group-5 max-w-5 top-level"; line-clamp-5`)["space-offscale"]).toBe(0);
  });

  it("theme guard: numeric --font-* weights and font-weight: var(--font-*)", () => {
    expect(countThemeWeights("--font-sans: 'General Sans';\n--font-semibold: 600;\nbody{font-weight: var(--font-body)}")).toEqual({ "theme-font-weight": 2 });
    expect(countThemeWeights("--font-weight-semibold: 600;\nbody{font-weight: var(--font-weight-normal)}")).toEqual({ "theme-font-weight": 0 });
  });
});

describe("design ratchet — compare", () => {
  const base = { "font-free": 10, "color-literal": 5 };
  it("green when equal", () => expect(compare({ "font-free": 10, "color-literal": 5 }, base)).toEqual([]));
  it("red when a new free value appears", () => expect(compare({ "font-free": 11, "color-literal": 5 }, base)[0]).toMatch(/font-free: 11 > baseline 10/));
  it("red when lower, so the win gets locked in", () => expect(compare({ "font-free": 9, "color-literal": 5 }, base)[0]).toMatch(/--update/));
  it("red for a rule without baseline", () => expect(compare({ "font-free": 10, "color-literal": 5, x: 1 }, base)[0]).toMatch(/no baseline/));
});

describe("design ratchet — this repository", () => {
  it("src/ holds exactly the baseline (no new free values, improvements locked in)", () => {
    const { totals } = scan(SRC_DIR);
    const problems = compare(totals, JSON.parse(readFileSync(BASELINE, "utf8")));
    // On red: `npm run design:ratchet -- --files` lists the files per rule.
    expect(problems).toEqual([]);
  });
});
