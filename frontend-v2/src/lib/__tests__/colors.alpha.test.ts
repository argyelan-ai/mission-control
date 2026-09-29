import { describe, expect, it } from "vitest";
import { alpha, resolveColor } from "../colors";

// alpha() replaces the old `${C.x}NN` hex-suffix trick. With colour tokens as
// CSS variables (`var(--color-…)`) a hex suffix produces invalid CSS without
// any type or merge error — color-mix works for variables AND plain hex.
describe("alpha", () => {
  it("wraps a CSS variable token in color-mix with the given opacity", () => {
    expect(alpha("var(--color-status-error)", 0.15)).toBe(
      "color-mix(in srgb, var(--color-status-error) 15%, transparent)",
    );
  });

  it("works on plain hex too (external brand colours)", () => {
    expect(alpha("#0A66C2", 0.5)).toBe("color-mix(in srgb, #0A66C2 50%, transparent)");
  });

  it("rounds to whole percent", () => {
    expect(alpha("#000", 0.333)).toBe("color-mix(in srgb, #000 33%, transparent)");
  });

  it("clamps out-of-range opacity to 0–100%", () => {
    expect(alpha("#000", 1.4)).toBe("color-mix(in srgb, #000 100%, transparent)");
    expect(alpha("#000", -0.2)).toBe("color-mix(in srgb, #000 0%, transparent)");
  });
});

describe("resolveColor", () => {
  it("returns non-variable values untouched", () => {
    expect(resolveColor("#123456")).toBe("#123456");
  });

  it("resolves a var(--x) against the document's computed style", () => {
    document.documentElement.style.setProperty("--color-test-probe", "#ABCDEF");
    expect(resolveColor("var(--color-test-probe)")).toBe("#ABCDEF");
    document.documentElement.style.removeProperty("--color-test-probe");
  });

  it("falls back to the provided default when the variable is unset", () => {
    expect(resolveColor("var(--color-never-set)", "#000000")).toBe("#000000");
  });

  it("resolves variables nested inside alpha() for canvas use", () => {
    document.documentElement.style.setProperty("--color-test-probe", "#ABCDEF");
    expect(resolveColor(alpha("var(--color-test-probe)", 0.2))).toBe(
      "color-mix(in srgb, #ABCDEF 20%, transparent)",
    );
    document.documentElement.style.removeProperty("--color-test-probe");
  });
});
