import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// ── Token parity: every colour defined for dark must be re-defined for light ──
// The original light-theme gap (PR #537 review) was invisible in the diff: twelve
// --color-* tokens existed only with dark values, so components silently fell
// back to dark status colours on paper (e.g. .text-err #fa4942 on white ≈ 2.9:1).
// This test parses globals.css and fails whenever a dark colour token lacks a
// light override — no manual counting.

const cssPath = resolve(dirname(fileURLToPath(import.meta.url)), "../../styles/globals.css");
const css = readFileSync(cssPath, "utf8");

/** Locate the balanced {...} body that follows a selector match. */
function lightBlockSpan(source: string): { body: string; before: string; after: string } {
  const match = source.match(LIGHT_SELECTOR);
  if (!match || match.index === undefined) throw new Error("light block not found in globals.css");
  const start = match.index + match[0].length;
  let depth = 1;
  let i = start;
  while (depth > 0 && i < source.length) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") depth--;
    i++;
  }
  if (depth !== 0) throw new Error("unbalanced braces in globals.css");
  return {
    body: source.slice(start, i - 1),
    before: source.slice(0, match.index),
    after: source.slice(i),
  };
}

const colorNames = (s: string) =>
  [...s.matchAll(/--color-[a-z0-9-]+(?=\s*:)/g)].map((m) => m[0]);

const LIGHT_SELECTOR = /:root\[data-theme="light"\]\s*\{/;
const { body: lightBlock, before, after } = lightBlockSpan(css);
const withoutLight = before + after;
const darkNames = new Set(colorNames(withoutLight));
const lightNames = new Set(colorNames(lightBlock));

// Deliberately theme-neutral tokens skip the parity check. Every entry needs a
// reason here — silent skips are how the original gap happened.
const EXCEPTIONS: Record<string, string> = {};

describe("light theme token parity (globals.css)", () => {
  it("parses both blocks and finds colour tokens on the dark side", () => {
    expect(darkNames.size).toBeGreaterThan(30);
    expect(lightNames.size).toBeGreaterThan(30);
  });

  it("defines every dark colour token in the light block", () => {
    const missing = [...darkNames].filter((n) => !lightNames.has(n) && !(n in EXCEPTIONS));
    expect(missing).toEqual([]);
  });

  it("keeps the exception list honest — entries must actually exist in dark", () => {
    for (const name of Object.keys(EXCEPTIONS)) {
      expect(darkNames.has(name)).toBe(true);
      expect(EXCEPTIONS[name].length).toBeGreaterThan(5);
    }
  });

  it("gives every light colour override a concrete value", () => {
    for (const name of lightNames) {
      const decl = lightBlock.match(new RegExp(`${name}\\s*:\\s*([^;]+);`));
      expect(decl, `${name} has no value`).not.toBeNull();
      expect(decl![1].trim().length).toBeGreaterThan(0);
    }
  });
});
