import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// The public login screen must not carry a fixed location (it once showed
// the coordinates of the maintainer's city) or the retired "control room"
// claim (PRINCIPLES §8.1).
const page = readFileSync(join(__dirname, "..", "page.tsx"), "utf8");
const en = JSON.parse(readFileSync(join(__dirname, "../../../../messages/en.json"), "utf8"));
const de = JSON.parse(readFileSync(join(__dirname, "../../../../messages/de.json"), "utf8"));

describe("login screen", () => {
  it("shows no geographic coordinates", () => {
    expect(page).not.toMatch(/\d+\.\d+°\s*[NS]/);
  });

  it("tagline carries the current positioning, not the control-room claim", () => {
    const taglines = JSON.stringify([en, de]).match(/"tagline":"[^"]*"/g) ?? [];
    expect(taglines.join(" ")).not.toMatch(/control room|Leitstand/i);
  });
});
