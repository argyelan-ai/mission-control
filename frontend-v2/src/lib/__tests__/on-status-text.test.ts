import { readdirSync, readFileSync, statSync } from "fs";
import path from "path";
import { describe, expect, it } from "vitest";
import { cssText, blockVars } from "./cssTokens";

/**
 * Guard (ADR-087): text on a SOLID status fill (danger buttons, "end call")
 * must use `C.onStatus`, never `C.textPrimary`. In dark both are the same
 * light tone; in light text-primary is ink — ink on a dark red is 2.2:1
 * (found by the UI probe's contrast check on the bench "remove" button).
 */

const SRC = path.resolve(__dirname, "../..");
const STATUS = "(?:error|online|warning|info)";
// `background: C.error,` next to `color: C.textPrimary` (either order)
const BG = `background(?:Color)?:\\s*C\\.${STATUS}`;
const FG = `color:\\s*C\\.textPrimary\\b`;
const RE = new RegExp(`${BG}\\s*,\\s*${FG}|${FG}\\s*,\\s*${BG}\\b`);
// Same trap on the accent fill: accent is off-cream in dark and ink in light,
// text-primary is light in dark and ink in light — invisible in BOTH modes.
const BG_A = `background(?:Color)?:\\s*C\\.accent\\b`;
const RE_ACCENT = new RegExp(`${BG_A}\\s*,\\s*${FG}|${FG}\\s*,\\s*${BG_A}`);

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = path.join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name === "__tests__" || name === "node_modules") continue;
      walk(p, out);
    } else if (/\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

describe("text on solid status fills", () => {
  it("uses C.onStatus, not C.textPrimary", () => {
    const hits: string[] = [];
    for (const f of walk(SRC)) {
      const src = readFileSync(f, "utf8").replace(/\s+/g, " ");
      if (RE.test(src)) hits.push(path.relative(SRC, f));
    }
    expect(hits).toEqual([]);
  });

  it("text on the accent fill uses C.onAccent, not C.textPrimary", () => {
    const hits: string[] = [];
    for (const f of walk(SRC)) {
      const src = readFileSync(f, "utf8").replace(/\s+/g, " ");
      if (RE_ACCENT.test(src)) hits.push(path.relative(SRC, f));
    }
    expect(hits).toEqual([]);
  });

  it("the rule catches the pattern (self-check)", () => {
    expect(RE.test("style={{ background: C.error, color: C.textPrimary }}")).toBe(true);
    expect(RE.test("style={{ background: C.error, color: C.onStatus }}")).toBe(false);
    expect(RE.test("style={{ color: C.textPrimary, background: C.error }}")).toBe(true);
    expect(RE_ACCENT.test("style={{ backgroundColor: C.accent, color: C.textPrimary }}")).toBe(true);
    expect(RE_ACCENT.test("style={{ background: C.accentSubtle, color: C.textPrimary }}")).toBe(false);
  });

  it("on-status equals text-primary in dark and is white in light", () => {
    const css = cssText();
    const dark = blockVars(":root", css);
    const light = blockVars(':root[data-theme="light"]', css);
    expect(dark.get("--color-on-status")).toBe("var(--color-text-primary)");
    expect(light.get("--color-on-status")?.toLowerCase()).toBe("#ffffff");
  });
});
