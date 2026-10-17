import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// The web app manifest and page metadata must describe MC in product terms
// (README.md / PRODUCT.md), not the retired "AI Agent Command Center" claim
// (ADR-085: no permanent fleet with fixed roles).
const manifest = readFileSync(join(__dirname, "..", "manifest.ts"), "utf8");
const layout = readFileSync(join(__dirname, "..", "layout.tsx"), "utf8");

describe("app metadata", () => {
  it("manifest description drops the retired command-center claim", () => {
    expect(manifest).not.toMatch(/AI Agent Command Center/i);
  });

  it("layout metadata description drops the retired command-center claim", () => {
    expect(layout).not.toMatch(/AI Agent Command Center/i);
  });
});
