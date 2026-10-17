import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// The public login screen must not carry internal fleet/version jargon
// ("Fleet · Live" / "v3") that means nothing to a first-time operator and
// is not a translated string (PRINCIPLES §8.1, i18n requirement).
const page = readFileSync(join(__dirname, "..", "page.tsx"), "utf8");

describe("login screen", () => {
  it("does not show the internal fleet/live status footer", () => {
    expect(page).not.toMatch(/Fleet\s*(·|&middot;)\s*Live/i);
  });

  it("does not show a hardcoded version badge", () => {
    expect(page).not.toMatch(/>\s*v3\s*</);
  });
});
