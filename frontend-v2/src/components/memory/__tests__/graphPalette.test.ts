import { afterEach, describe, expect, it } from "vitest";
import { COMMUNITY_PALETTE, COMMUNITY_PALETTE_LIGHT, resolveGraphPalette } from "../graphConfig";
import { blockVars, resolveVar } from "@/lib/__tests__/cssTokens";

// The memory graph draws on a canvas: every colour must be resolved to a real
// value (never var()), per theme, and stay visible on that theme's ground.

function lum(h: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
const ratio = (a: string, b: string) => {
  const [x, y] = [lum(a), lum(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
};

const dark = blockVars(":root");
const light = new Map([...dark, ...blockVars(':root[data-theme="light"]')]);

afterEach(() => {
  document.documentElement.removeAttribute("style");
});

describe("graph canvas palette", () => {
  it("resolves every colour to a concrete value — canvas cannot read var()", () => {
    for (const [k, v] of dark) if (k.startsWith("--color-")) document.documentElement.style.setProperty(k, resolveVar(v, dark));
    const p = resolveGraphPalette("dark");
    const all = [p.selected, p.label, p.edge, p.edgeHover, p.edgeFiltered, p.edgeFaded, ...Object.values(p.type), ...p.community];
    for (const c of all) expect(c, c).not.toMatch(/var\(/);
  });

  it("follows the theme: light values after a switch", () => {
    for (const [k, v] of light) if (k.startsWith("--color-")) document.documentElement.style.setProperty(k, resolveVar(v, light));
    const p = resolveGraphPalette("light");
    expect(p.label.toLowerCase()).toBe(resolveVar("var(--color-text-primary)", light).toLowerCase());
    expect(p.community[1]).toBe(COMMUNITY_PALETTE_LIGHT[1]);
  });

  it.each([
    ["dark", COMMUNITY_PALETTE, dark],
    ["light", COMMUNITY_PALETTE_LIGHT, light],
  ] as const)("%s: community colours stand out from the ground (≥3:1)", (_mode, palette, vars) => {
    const ground = resolveVar("var(--color-bg-deep)", vars);
    for (const c of palette) {
      const hex = c.startsWith("var(") ? resolveVar(c, vars) : c;
      expect(ratio(hex, ground), `${c} on ${ground}`).toBeGreaterThanOrEqual(3);
    }
  });
});
