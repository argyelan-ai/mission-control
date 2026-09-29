import { readdirSync, readFileSync, statSync } from "fs";
import path from "path";
import { describe, expect, it } from "vitest";

/**
 * Guard (ADR-087): the ONE switch between the themes is `data-theme` on
 * <html> (set before first paint by lib/themeScript.ts). A static
 * `class="dark"` on <html> stays there in light mode too, so any Tailwind
 * `dark:` utility would silently apply in light. Therefore: no `dark` class
 * on <html>, and no `dark:` variant anywhere — use tokens, or the `light:`
 * variant (globals.css) for the rare light-only override.
 */

const SRC = path.resolve(__dirname, "../..");

function walk(d: string, out: string[] = []): string[] {
  for (const n of readdirSync(d)) {
    const p = path.join(d, n);
    if (statSync(p).isDirectory()) { if (n !== "__tests__" && n !== "node_modules") walk(p, out); }
    else if (/\.(ts|tsx|js|jsx|mjs)$/.test(n) && !/\.test\./.test(n)) out.push(p);
  }
  return out;
}

describe("no dark class / dark: variant", () => {
  it("the root layout does not put a static dark class on <html>", () => {
    const layout = readFileSync(path.join(SRC, "app/layout.tsx"), "utf8");
    expect(layout).not.toMatch(/className=["'{`][^"'}`]*\bdark\b/);
  });

  it("no source file uses the Tailwind dark: variant", () => {
    const hits: string[] = [];
    for (const file of walk(SRC)) {
      readFileSync(file, "utf8").split("\n").forEach((line, i) => {
        // `dark:` as a class prefix: preceded by start, space, quote or backtick
        // and followed by a utility name (not an object key like `{ dark: Moon }`).
        if (/(?:^|[\s"'`])dark:[a-z[!-]/.test(line) && /className|class=|cn\(|clsx\(|cva\(/.test(line)) {
          hits.push(`${path.relative(SRC, file)}:${i + 1} ${line.trim().slice(0, 120)}`);
        }
      });
    }
    expect(hits, `\n${hits.join("\n")}\n`).toEqual([]);
  });

  it("globals.css defines no .dark selector or dark custom variant", () => {
    const css = readFileSync(path.join(SRC, "styles/globals.css"), "utf8");
    expect(css).not.toMatch(/\.dark\b/);
    expect(css).not.toMatch(/@custom-variant\s+dark\b/);
  });
});
