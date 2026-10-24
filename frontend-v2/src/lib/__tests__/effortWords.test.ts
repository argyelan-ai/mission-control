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
    // W1 (24.10.2026): the legacy bench words are gone; the W1 shared words
    // (benchShared.level) say the same word as common.effortLevel, only in
    // lower case where they sit inside a sentence ("high (default)").
    expect("effort" in de.bench).toBe(false);
    expect("effort" in en.bench).toBe(false);
    for (const lang of [de, en]) {
      for (const [level, word] of Object.entries(lang.benchShared.level)) {
        const common = lang.common.effortLevel[level as keyof typeof lang.common.effortLevel];
        expect(word.toLowerCase(), level).toBe(common.toLowerCase());
      }
    }
  });

  it("the chat's 'default' button starts with the same word as the level", () => {
    expect(de.sessions.effortDefault.startsWith(de.common.effortLevel.default)).toBe(true);
    expect(en.sessions.effortDefault.startsWith(en.common.effortLevel.default)).toBe(true);
  });

  it("no bench text appends an English effort word to a label any more", () => {
    // The legacy "New challenge" dialog and its words are removed (W1).
    expect("newChallengeDialog" in de.bench).toBe(false);
    expect("newChallengeDialog" in en.bench).toBe(false);
  });
});
