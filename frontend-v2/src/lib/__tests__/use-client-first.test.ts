import { readdirSync, readFileSync, statSync } from "fs";
import path from "path";
import { describe, expect, it } from "vitest";

// A "use client" directive after an import is a build error in Next — but tsc
// and vitest do not notice. Codemods that add imports (the colour codemods
// of ADR-087) can put one above the directive; this catches it.
describe('"use client" is the first statement', () => {
  it("no file has code before its directive", () => {
    const SRC = path.resolve(__dirname, "../..");
    const bad: string[] = [];
    const walk = (d: string) => {
      for (const n of readdirSync(d)) {
        const p = path.join(d, n);
        if (statSync(p).isDirectory()) { if (n !== "node_modules") walk(p); continue; }
        if (!/\.(ts|tsx)$/.test(n)) continue;
        const src = readFileSync(p, "utf8");
        const i = src.search(/^["']use client["'];?\s*$/m);
        if (i < 0) continue;
        const before = src.slice(0, i).replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "").trim();
        if (before) bad.push(path.relative(SRC, p));
      }
    };
    walk(SRC);
    expect(bad).toEqual([]);
  });
});
