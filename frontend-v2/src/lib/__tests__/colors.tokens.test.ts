import { readdirSync, readFileSync, statSync } from "fs";
import path from "path";
import { describe, expect, it } from "vitest";
import { C, LANE, P2, STATUS, STATUS_TEXT, WORKSPACE_COLORS, XTERM_THEME } from "../colors";
import { blockVars, resolveVar } from "./cssTokens";

// ADR-087: every UI colour token is a CSS variable whose value lives in the
// plain `:root` block of globals.css (NOT in Tailwind's `@theme`, which tree-
// shakes variables it does not see used in CSS).

function flatten(obj: Record<string, unknown>, prefix = ""): [string, string][] {
  return Object.entries(obj).flatMap(([k, v]) =>
    typeof v === "string" ? [[prefix + k, v] as [string, string]] : flatten(v as Record<string, unknown>, `${prefix}${k}.`),
  );
}

const root = blockVars(":root");

describe("colour tokens are CSS variables", () => {
  const groups = { C, P2, STATUS, LANE, STATUS_TEXT } as Record<string, Record<string, unknown>>;
  for (const [g, obj] of Object.entries(groups)) {
    it(`${g}: every token is var(--color-…) and defined in :root`, () => {
      for (const [k, v] of flatten(obj)) {
        const m = /^var\((--color-[a-z0-9-]+)\)$/.exec(v);
        expect(m, `${g}.${k} = ${v}`).not.toBeNull();
        expect(root.has(m![1]), `${g}.${k}: ${m![1]} missing in :root`).toBe(true);
        expect(() => resolveVar(v, root), `${g}.${k}`).not.toThrow();
      }
    });
  }

  it("every var(--color-…) referenced in src is defined in :root", () => {
    const SRC = path.resolve(__dirname, "../..");
    const used = new Set<string>();
    const walk = (d: string) => {
      for (const n of readdirSync(d)) {
        const p = path.join(d, n);
        if (statSync(p).isDirectory()) { if (n !== "__tests__") walk(p); continue; }
        if (!/\.(tsx?|css)$/.test(n) || /\.test\./.test(n)) continue;
        for (const m of readFileSync(p, "utf8").matchAll(/var\((--color-[a-z0-9-]+)/g)) used.add(m[1]);
      }
    };
    walk(SRC);
    const missing = [...used].filter((v) => !root.has(v));
    expect(missing).toEqual([]);
  });

  it("board colours stay literal hex — they are saved to the database", () => {
    for (const c of WORKSPACE_COLORS) expect(c).toMatch(/^#[0-9A-F]{6}$/i);
  });

  it("the terminal theme stays literal (xterm cannot read CSS variables)", () => {
    for (const [k, v] of Object.entries(XTERM_THEME)) expect(v, k).toMatch(/^#[0-9A-F]{6}$/i);
  });
});
