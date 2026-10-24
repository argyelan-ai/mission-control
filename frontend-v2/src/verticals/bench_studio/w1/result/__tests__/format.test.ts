import { describe, expect, it } from "vitest";
import {
  formatBytes,
  formatClock,
  formatSeconds,
  formatTime,
  formatTokens,
  formatUsd,
  formatWork,
  numLocale,
  statText,
} from "../format";

describe("bench result number formats", () => {
  it("uses the Swiss/British number locales of the app", () => {
    expect(numLocale("de")).toBe("de-CH");
    expect(numLocale("en")).toBe("en-GB");
  });

  it("prints class A compute time as a clock (same as the card)", () => {
    expect(formatClock(66_000)).toBe("1:06");
    expect(formatClock(62_000)).toBe("1:02");
    expect(formatClock(3_725_000)).toBe("1:02:05");
  });

  it("prints class B work time in minutes, never a clock", () => {
    // Shot Sequence: 5 551 s = 92.5 min → the concept's "92 min".
    expect(formatWork(5_551_000)).toBe("92 min");
    expect(formatWork(45_000)).toBe("45 s");
    expect(formatWork(7_000_000)).toBe("1 h 56 min");
  });

  it("picks the form from the time kind", () => {
    expect(formatTime(66_000, "gen_ms")).toBe("1:06");
    expect(formatTime(5_551_000, "active_ms")).toBe("92 min");
  });

  it("shortens token counts like the card", () => {
    expect(formatTokens(6607, "en-GB")).toBe("6.6k");
    expect(formatTokens(6400, "de-CH")).toBe("6.4k");
    expect(formatTokens(264_150, "en-GB")).toBe("264k");
    expect(formatTokens(1_117_881, "en-GB")).toBe("1.12 M");
    expect(formatTokens(980, "en-GB")).toBe("980");
  });

  it("prints seconds, dollars and sizes", () => {
    expect(formatSeconds(410, "en-GB")).toBe("0.4 s");
    expect(formatUsd(0.04)).toBe("0.04");
    expect(formatUsd(0.004)).toBe("0.01");
    expect(formatUsd(1.5)).toBe("1.50");
    expect(formatBytes(17_204, "en-GB")).toBe("17 kB");
    expect(formatBytes(18_800_000, "en-GB")).toBe("18.8 MB");
  });

  it("gives median and range of a stat, and a single value without a range", () => {
    const fmt = (v: number) => formatClock(v);
    expect(
      statText({ measured: true, median: 66_000, min: 62_000, max: 71_000, n: 3, reason: null }, fmt),
    ).toEqual({ median: "1:06", min: "1:02", max: "1:11", n: 3 });
    expect(
      statText({ measured: true, median: 66_000, min: 66_000, max: 66_000, n: 1, reason: null }, fmt),
    ).toEqual({ median: "1:06", min: null, max: null, n: 1 });
    expect(
      statText({ measured: false, median: null, min: null, max: null, n: 1, reason: null }, fmt),
    ).toBeNull();
  });
});
