/**
 * One shared content edge (DESIGN.md K9): pages start at AppShell's padding
 * and cap only their width with the shared tokens — none centres itself.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { PAGE_READ, PAGE_WIDE } from "../AppShellWidth";

const PAGES = ["insights", "schedule", "files", "inbox", "more"];
const src = (page: string) => readFileSync(resolve(__dirname, `../../../app/${page}/page.tsx`), "utf8");

describe("AppShell content widths", () => {
  it("are left-aligned caps, never centred", () => {
    for (const w of [PAGE_READ, PAGE_WIDE]) {
      expect(w).toMatch(/max-w-/);
      expect(w).not.toMatch(/mx-auto/);
    }
  });

  it.each(PAGES)("%s uses a shared width and does not centre itself", (page) => {
    const s = src(page);
    expect(s).toMatch(/PAGE_(READ|WIDE)/);
    expect(s).not.toMatch(/max-w-[^"`\s]+ mx-auto/);
  });
});
