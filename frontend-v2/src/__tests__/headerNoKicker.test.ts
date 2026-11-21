/**
 * DESIGN.md K7: no upper-case kicker line ("SYSTEM · SETTINGS",
 * "FLEET · AGENTEN" …) above a page title. Scans every component source for a
 * `.label-sys` element within the four lines before an `<h1`.
 *
 * Sabotage: put `<div className="label-sys mb-2">System · Settings</div>` back
 * above the settings title → this test names the file and line.
 */
import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const SRC = join(__dirname, "..");
// Login is the pre-login brand screen, not an app page; it keeps its accent
// mark until the login redesign decides otherwise.
const ALLOWED = new Set(["app/login/page.tsx"]);

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) return name === "__tests__" ? [] : sources(p);
    return /\.tsx$/.test(name) && !/\.test\.tsx$/.test(name) ? [p] : [];
  });
}

describe("page headers carry no kicker line (K7)", () => {
  it("no .label-sys right above an <h1>", () => {
    const hits: string[] = [];
    for (const file of sources(SRC)) {
      const rel = relative(SRC, file);
      if (ALLOWED.has(rel)) continue;
      const lines = readFileSync(file, "utf-8").split("\n");
      lines.forEach((line, i) => {
        if (!line.includes("<h1")) return;
        for (let j = Math.max(0, i - 4); j < i; j++) {
          if (lines[j].includes("label-sys")) hits.push(`${rel}:${j + 1}`);
        }
      });
    }
    expect(hits).toEqual([]);
  });
});
