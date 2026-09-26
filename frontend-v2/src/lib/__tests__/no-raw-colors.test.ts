import { readdirSync, readFileSync, statSync } from "fs";
import path from "path";
import { describe, expect, it } from "vitest";

/**
 * Ratchet (ADR-087): components and pages take colours from tokens
 * (lib/colors.ts, var(--color-…)), never raw hex / rgba() / fixed Tailwind
 * palette classes — a raw colour does not follow the theme switch.
 *
 * The allowlist names every file that still carries raw colours ON PURPOSE,
 * with the exact count and the reason. The count may only go down: more hits
 * fail, fewer hits fail too (lower the number so nobody can spend it again).
 */

const SRC = path.resolve(__dirname, "../..");
const DIRS = ["components", "app"];

const ALLOW: Record<string, { count: number; reason: string }> = {
  "components/chat/Motif.tsx": { count: 14, reason: "canvas brand figure — same blue in both modes (glow scaled per theme)" },
  "components/memory/graphConfig.ts": { count: 20, reason: "categorical community palettes for the canvas, dark + light set (contrast-tested)" },
  "components/memory/VaultTopicsView.tsx": { count: 1, reason: "agent identity pink — categorical, not structural" },
  "components/pages/OfficeView/OrgChart/OrgChartNode.tsx": { count: 4, reason: "silver avatar sphere with dark glyph — an object, reads the same on both grounds" },
  "components/vault/agentColors.ts": { count: 1, reason: "hsl() hue hash; lightness is var(--agent-lightness)" },
  "app/manifest.ts": { count: 2, reason: "PWA manifest needs literal colours (dark = default)" },
};

const RULES: [string, RegExp][] = [
  ["hex", /(?<![\w&/])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])/g],
  ["rgba", /\b(?:rgba?|hsla?)\(\s*\d/g],
  ["tailwind", /(?<![\w-])(?:bg|text|border|ring|from|to|via|fill|stroke|divide|outline|shadow|placeholder|decoration|caret|accent)-(?:white|black|(?:zinc|gray|slate|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3})(?:\/\d+)?(?![\w-])/g],
];

function walk(d: string, out: string[] = []): string[] {
  for (const n of readdirSync(d)) {
    const p = path.join(d, n);
    if (statSync(p).isDirectory()) { if (n !== "__tests__") walk(p, out); }
    else if (/\.(ts|tsx)$/.test(n) && !/\.test\.(ts|tsx)$/.test(n)) out.push(p);
  }
  return out;
}

/** Keep line numbers, drop comments (block, JSX and line comments). */
function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "))
    .replace(/(^|[\s;{}(),])\/\/.*$/gm, "$1");
}

export function scanRawColors(): Map<string, string[]> {
  const hits = new Map<string, string[]>();
  for (const dir of DIRS) {
    for (const file of walk(path.join(SRC, dir))) {
      const rel = path.relative(SRC, file);
      stripComments(readFileSync(file, "utf8")).split("\n").forEach((line, i) => {
        for (const [name, re] of RULES) {
          for (const m of line.match(re) ?? []) {
            const list = hits.get(rel) ?? [];
            list.push(`${rel}:${i + 1} [${name}] ${m}`);
            hits.set(rel, list);
          }
        }
      });
    }
  }
  return hits;
}

describe("no raw colours in components/pages (ratchet)", () => {
  const hits = scanRawColors();

  it("files outside the allowlist use tokens only", () => {
    const stray = [...hits].filter(([f]) => !ALLOW[f]).flatMap(([, l]) => l);
    expect(stray, `\nUse a token (C.*, alpha(C.*, a), var(--color-…)) instead:\n${stray.join("\n")}\n`).toEqual([]);
  });

  it("allowlisted files hold exactly their recorded count (it may only go down)", () => {
    const drift = Object.entries(ALLOW)
      .map(([f, a]) => [f, a.count, hits.get(f)?.length ?? 0] as const)
      .filter(([, allowed, actual]) => allowed !== actual)
      .map(([f, allowed, actual]) =>
        actual > allowed ? `${f}: ${actual} raw colours, allowed ${allowed}` : `${f}: down to ${actual} — lower the allowance from ${allowed}`,
      );
    expect(drift).toEqual([]);
  });
});
