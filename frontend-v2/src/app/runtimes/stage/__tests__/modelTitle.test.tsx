/**
 * shortModelTitle/shortHostName — Kurzname-Regeln aus der Live-Sichtprüfung
 * 06.09.2026 (Zone 1 Titel + Zone 3 Mitgliedername).
 */
import { describe, it, expect } from "vitest";
import { shortModelTitle, shortHostName } from "../modelTitle";

describe("shortModelTitle", () => {
  it("Mark's exact example: engine + two trailing quant/mtp tokens + box-count parens", () => {
    expect(shortModelTitle("Qwen3.8 Flash Next NVFP4 MTP3 — vLLM (2× Spark)")).toBe("Qwen3.8 Flash Next");
  });

  it("cuts at the first em-dash, keeps everything before it otherwise untouched", () => {
    expect(shortModelTitle("DeepSeek V4 Flash Vision — SGLang")).toBe("DeepSeek V4 Flash Vision");
  });

  it("single trailing quant token without an em-dash", () => {
    expect(shortModelTitle("GLM 5.3 Flash EXL3")).toBe("GLM 5.3 Flash");
  });

  it("trailing box-count parens without an em-dash", () => {
    expect(shortModelTitle("Qwen3.8 27B (1× Spark)")).toBe("Qwen3.8 27B");
  });

  it("a plain name with none of the noise passes through unchanged", () => {
    expect(shortModelTitle("Claude Opus")).toBe("Claude Opus");
  });

  it("never returns an empty string — falls back to the full name", () => {
    expect(shortModelTitle("NVFP4")).toBe("NVFP4");
  });
});

describe("shortHostName", () => {
  it("strips a technical-hostname parenthetical", () => {
    expect(shortHostName("GX10 (gx10-dd72)")).toBe("GX10");
  });

  it("a plain box name passes through unchanged", () => {
    expect(shortHostName("DGX Spark")).toBe("DGX Spark");
  });
});
