import { describe, it, expect } from "vitest";
import de from "../../../messages/de.json";
import en from "../../../messages/en.json";

// Round-2 Q2 (r2 S1, t2 NICE-2): one word per effort level — the bench
// dialog, the bench entry and the chat effort menu all read it from
// common.effortLevel. No second list of level words anywhere.

const LEVELS = [
  "default", "auto", "none", "off", "on", "minimal", "low", "medium", "high", "xhigh", "max", "ultracode",
];

describe("effort level words — one source (common.effortLevel)", () => {
  it("names every level the bench and the chat harnesses use, in both languages", () => {
    for (const level of LEVELS) {
      expect(en.common.effortLevel[level as keyof typeof en.common.effortLevel], `en ${level}`).toBeTruthy();
      expect(de.common.effortLevel[level as keyof typeof de.common.effortLevel], `de ${level}`).toBeTruthy();
    }
  });

  it("the German UI says Standard / Aus, the English one Default / Off", () => {
    expect(de.common.effortLevel.default).toBe("Standard");
    expect(de.common.effortLevel.off).toBe("Aus");
    expect(en.common.effortLevel.default).toBe("Default");
    expect(en.common.effortLevel.off).toBe("Off");
  });

  it("the bench keeps no second list of level words", () => {
    expect((de.bench.effort as Record<string, unknown>).level).toBeUndefined();
    expect((en.bench.effort as Record<string, unknown>).level).toBeUndefined();
  });

  it("the chat's 'default' button starts with the same word as the level", () => {
    expect(de.sessions.effortDefault.startsWith(de.common.effortLevel.default)).toBe(true);
    expect(en.sessions.effortDefault.startsWith(en.common.effortLevel.default)).toBe(true);
  });

  it("no bench text appends an English effort word to a label any more", () => {
    const dialog = de.bench.newChallengeDialog as Record<string, unknown>;
    expect(dialog.labelEffortSuffix).toBeUndefined();
  });
});
