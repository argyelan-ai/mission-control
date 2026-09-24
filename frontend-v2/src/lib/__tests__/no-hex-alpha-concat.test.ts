import { readdirSync, readFileSync, statSync } from "fs";
import path from "path";
import { describe, expect, it } from "vitest";

/**
 * Guard (ADR-087): colours are CSS variables (`var(--color-…)`), so the old
 * trick of appending a two-digit hex alpha to a colour — `${C.error}33`,
 * `${cfg.color}1A`, `C.warning + "66"` — silently yields INVALID CSS
 * (`var(--color-status-error)33`). No type error, no merge conflict, the
 * browser just drops the declaration. Use `alpha(color, 0.2)` from lib/colors.
 *
 * The template-literal rule is deliberately broad (any `${…}NN`, not only
 * `${C.x}NN`): most real cases hold the colour in a local variable or prop.
 */

const SRC = path.resolve(__dirname, "../..");

// Real non-colour uses of `${…}NN` would go here, each with a reason.
// Target: empty.
const ALLOW: { file: string; includes: string; reason: string }[] = [];

const RULES: { name: string; re: RegExp }[] = [
  // `${anything}` followed by exactly two hex digits (not a longer word/number)
  { name: "template hex-alpha suffix", re: /\$\{[^{}`]+\}[0-9a-fA-F]{2}(?![0-9a-zA-Z_-])/ },
  // colour + "NN" string concatenation
  { name: "string-plus hex-alpha suffix", re: /[A-Za-z0-9_\])]\s*\+\s*["'`][0-9a-fA-F]{2}["'`]/ },
  // parsing a colour token as hex (NaN once it is a CSS variable)
  { name: "hex parsing of a colour", re: /\bhexToRgb\s*\(/ },
  // rgba() assembled from interpolated channels — a hard-coded copy of a
  // token as numbers, which never follows the theme
  { name: "rgba() from interpolated channels", re: /rgba?\(\s*\$\{/ },
];

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = path.join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name === "__tests__" || name === "node_modules") continue;
      walk(p, out);
    } else if (/\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name)) {
      out.push(p);
    }
  }
  return out;
}

export function findHexAlphaConcat(root = SRC): string[] {
  const hits: string[] = [];
  for (const file of walk(root)) {
    const rel = path.relative(root, file);
    readFileSync(file, "utf8").split("\n").forEach((line, i) => {
      const t = line.trim();
      if (t.startsWith("//") || t.startsWith("*") || t.startsWith("/*")) return;
      for (const rule of RULES) {
        if (!rule.re.test(line)) continue;
        if (ALLOW.some((a) => a.file === rel && line.includes(a.includes))) continue;
        hits.push(`${rel}:${i + 1} [${rule.name}] ${t.slice(0, 140)}`);
      }
    });
  }
  return hits;
}

describe("no hex-alpha concatenation on colours", () => {
  it("no source file appends a hex alpha suffix to a colour — use alpha()", () => {
    const hits = findHexAlphaConcat();
    expect(hits, `\n${hits.join("\n")}\n`).toEqual([]);
  });

  it("every allowlist entry still matches something (no stale exemptions)", () => {
    for (const a of ALLOW) {
      const text = readFileSync(path.join(SRC, a.file), "utf8");
      expect(text.includes(a.includes), `${a.file}: ${a.reason}`).toBe(true);
    }
  });
});
