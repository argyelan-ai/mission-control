import { describe, expect, it } from "vitest";
import { blockVars, cssText, resolveVar } from "./cssTokens";

/**
 * WCAG contrast of the colour tokens in BOTH modes (ADR-087), computed from
 * the real values in globals.css. Dark = `:root`, light = `:root` overlaid
 * with `:root[data-theme="light"]` (aliases resolve through the overlay,
 * exactly like the browser does).
 */

const css = cssText();
const darkVars = blockVars(":root", css);
const lightOnly = blockVars(':root[data-theme="light"]', css);
const lightVars = new Map([...darkVars, ...lightOnly]);

function hex(value: string): string {
  const v = value.trim().toLowerCase();
  if (/^#[0-9a-f]{6}$/.test(v)) return v;
  if (/^#[0-9a-f]{3}$/.test(v)) return "#" + [...v.slice(1)].map((c) => c + c).join("");
  throw new Error(`not an opaque hex colour: ${value}`);
}
function lum(h: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function ratio(a: string, b: string): number {
  const [x, y] = [lum(hex(a)), lum(hex(b))];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

function hslHex(h: number, s: number, l: number): string {
  const k = (n: number) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return "#" + [f(0), f(8), f(4)].map((x) => Math.round(x * 255).toString(16).padStart(2, "0")).join("");
}

const SURFACES = ["--color-bg-deep", "--color-bg-base", "--color-bg-surface", "--color-bg-elevated", "--color-bg-hover"];

// Tokens used as text: AA (4.5:1) on every surface.
const TEXT = [
  "--color-text-primary", "--color-text-secondary", "--color-text-muted",
  "--color-status-online", "--color-status-error", "--color-status-info",
  "--color-status-warning-text", "--color-accent",
  "--color-p2-txt", "--color-p2-dim", "--color-p2-amb", "--color-p2-ok", "--color-p2-err",
];
// Status hue used for dots/borders/icons (text uses status-warning-text): 3:1.
const GRAPHIC = ["--color-status-warning", "--color-p2-wrn"];

/**
 * Known dark-mode gaps — PRE-EXISTING on main (the surfaces were lifted for
 * daylight legibility, the status hues were not re-tuned). Recorded with the
 * measured value so they can only get better; ADR-087 lists them as follow-up.
 * Dark values are deliberately not changed by the light-mode work.
 */
const DARK_KNOWN: Record<string, number> = {
  "--color-status-online|--color-bg-elevated": 4.49,
  "--color-status-online|--color-bg-hover": 3.92,
  "--color-status-error|--color-bg-surface": 4.39,
  "--color-status-error|--color-bg-elevated": 3.78,
  "--color-status-error|--color-bg-hover": 3.3,
  "--color-status-info|--color-bg-elevated": 3.88,
  "--color-status-info|--color-bg-hover": 3.39,
  "--color-status-warning-text|--color-bg-elevated": 4.4,
  "--color-status-warning-text|--color-bg-hover": 3.85,
  "--color-p2-ok|--color-bg-elevated": 4.49,
  "--color-p2-ok|--color-bg-hover": 3.92,
  "--color-p2-err|--color-bg-surface": 4.39,
  "--color-p2-err|--color-bg-elevated": 3.78,
  "--color-p2-err|--color-bg-hover": 3.3,
};

function check(mode: "dark" | "light", vars: Map<string, string>) {
  const fails: string[] = [];
  const col = (t: string) => resolveVar(`var(${t})`, vars);
  for (const s of SURFACES) {
    for (const t of TEXT) {
      const r = ratio(col(t), col(s));
      const known = mode === "dark" ? DARK_KNOWN[`${t}|${s}`] : undefined;
      if (r < 4.5 && !(known !== undefined && r >= known - 0.01)) fails.push(`${t} on ${s}: ${r.toFixed(2)}`);
    }
    for (const t of GRAPHIC) {
      const r = ratio(col(t), col(s));
      if (r < 3) fails.push(`${t} on ${s}: ${r.toFixed(2)} (<3)`);
    }
  }
  const onAccent = ratio(col("--color-on-accent"), col("--color-accent"));
  if (onAccent < 4.5) fails.push(`on-accent on accent: ${onAccent.toFixed(2)}`);
  const term = ratio(col("--color-term-fg"), col("--color-term"));
  if (term < 7) fails.push(`term-fg on term: ${term.toFixed(2)} (<7)`);
  // text-dim is decoration only (documented) — but must stay perceivable.
  for (const s of SURFACES) {
    const r = ratio(col("--color-text-dim"), col(s));
    if (r < 2.5) fails.push(`text-dim on ${s}: ${r.toFixed(2)} (<2.5)`);
  }
  return fails;
}

describe("theme contrast (WCAG) — both modes", () => {
  it("dark: text tokens clear AA on every surface (except recorded pre-existing gaps)", () => {
    expect(check("dark", darkVars)).toEqual([]);
  });

  it("light: text tokens clear AA on every surface, no exceptions", () => {
    expect(lightOnly.size).toBeGreaterThan(0);
    expect(check("light", lightVars)).toEqual([]);
  });

  it("light: text-dim is readable text on the resting surfaces (it is used for small meta text)", () => {
    const fails = ["--color-bg-deep", "--color-bg-base", "--color-bg-surface", "--color-bg-elevated"]
      .map((sf) => [sf, ratio(resolveVar("var(--color-text-dim)", lightVars), resolveVar(`var(${sf})`, lightVars))] as const)
      .filter(([, r]) => r < 4.5)
      .map(([sf, r]) => `text-dim on ${sf}: ${r.toFixed(2)}`);
    expect(fails).toEqual([]);
  });

  it("every literal dark colour token has a light counterpart (or is dark on purpose)", () => {
    const SAME_IN_BOTH = new Set(["--color-term", "--color-term-fg"]);
    const missing = [...darkVars]
      .filter(([k, v]) => k.startsWith("--color-") && !/var\(/.test(v))
      .map(([k]) => k)
      .filter((k) => !lightOnly.has(k) && !SAME_IN_BOTH.has(k));
    expect(missing).toEqual([]);
  });

  it("syntax-highlighted code keeps a dark ground in both modes (atomOneDark fg ≥4.5)", () => {
    for (const vars of [darkVars, lightVars]) {
      expect(ratio("#abb2bf", resolveVar("var(--color-code-bg)", vars))).toBeGreaterThanOrEqual(4.5);
      expect(ratio(resolveVar("var(--color-code-dim)", vars), resolveVar("var(--color-code-bg)", vars))).toBeGreaterThanOrEqual(3);
    }
  });

  it("light: agent identity colours (hsl, --agent-lightness) clear AA on every surface", () => {
    const L = parseFloat(resolveVar("var(--agent-lightness)", lightVars)) / 100;
    const hues: [number, number][] = [12, 38, 60, 145, 175, 200, 215, 300, 320, 340].map((h) => [h, 0.55]);
    hues.push([260, 0.4]);
    const fails: string[] = [];
    for (const [h, sat] of hues) {
      const c = hslHex(h, sat, L);
      for (const s of SURFACES) {
        const r = ratio(c, resolveVar(`var(${s})`, lightVars));
        if (r < 4.5) fails.push(`hue ${h} on ${s}: ${r.toFixed(2)}`);
      }
    }
    expect(fails).toEqual([]);
  });

  it("light: status text and status hues stay AA on their own 12 % tint (chips) over elevated/hover", () => {
    const mix = (fg: string, bg: string, a: number) =>
      "#" + [1, 3, 5].map((i) => Math.round(parseInt(fg.slice(i, i + 2), 16) * a + parseInt(bg.slice(i, i + 2), 16) * (1 - a)).toString(16).padStart(2, "0")).join("");
    const col = (t: string) => resolveVar(`var(${t})`, lightVars);
    const fails: string[] = [];
    for (const [text, hue] of [["--color-status-warning-text", "--color-status-warning"], ["--color-status-error-text", "--color-status-error"], ["--color-status-online-text", "--color-status-online"], ["--color-status-info", "--color-status-info"],
      // chips paint the status HUE as text on its own tint (Pill, tag chips)
      ["--color-status-warning", "--color-status-warning"], ["--color-status-error", "--color-status-error"], ["--color-status-online", "--color-status-online"]]) {
      for (const s of ["--color-bg-elevated", "--color-bg-hover"]) {
        const r = ratio(col(text), mix(col(hue), col(s), 0.12));
        if (r < 4.5) fails.push(`${text} on 12% ${hue} over ${s}: ${r.toFixed(2)}`);
      }
    }
    expect(fails).toEqual([]);
  });

  it("light mode also sets color-scheme: light (native controls, scrollbars)", () => {
    expect(css).toMatch(/:root\[data-theme="light"\]\s*\{[^}]*color-scheme:\s*light/);
  });
});
