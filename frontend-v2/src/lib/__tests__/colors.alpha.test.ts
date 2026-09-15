import { describe, expect, it } from "vitest";
import { C, alpha, resolveColor } from "../colors";

describe("alpha", () => {
  it("wraps a CSS variable token in color-mix with the given opacity", () => {
    expect(alpha("var(--color-status-error)", 0.15)).toBe(
      "color-mix(in srgb, var(--color-status-error) 15%, transparent)",
    );
  });

  it("works on plain hex too (external brand colors)", () => {
    expect(alpha("#0A66C2", 0.5)).toBe("color-mix(in srgb, #0A66C2 50%, transparent)");
  });

  it("rounds to whole percent", () => {
    expect(alpha("#000", 0.333)).toBe("color-mix(in srgb, #000 33%, transparent)");
  });
});

describe("tokens are CSS variables", () => {
  it("C.accent and status tokens reference --color-* variables", () => {
    expect(C.accent).toMatch(/^var\(--color-/);
    expect(C.error).toMatch(/^var\(--color-/);
    expect(C.bgBase).toMatch(/^var\(--color-/);
  });
});

describe("resolveColor", () => {
  it("returns non-variable values untouched", () => {
    expect(resolveColor("#123456")).toBe("#123456");
  });

  it("resolves a var(--x) against the document's computed style", () => {
    document.documentElement.style.setProperty("--color-test-probe", "#ABCDEF");
    expect(resolveColor("var(--color-test-probe)")).toBe("#ABCDEF");
  });

  it("falls back to the provided default when the variable is unset", () => {
    expect(resolveColor("var(--color-never-set)", "#000000")).toBe("#000000");
  });
});
