import { describe, it, expect } from "vitest";
import { formatDuration, formatAgeRounded, secondsBetween, formatUsd, parseTs } from "../format";

describe("parseTs", () => {
  it("reads a naive backend timestamp as UTC, not local time", () => {
    expect(parseTs("2026-09-20T10:00:00")?.toISOString()).toBe("2026-09-20T10:00:00.000Z");
    expect(parseTs("2026-09-20T10:00:00.123456")?.toISOString()).toBe("2026-09-20T10:00:00.123Z");
  });
  it("keeps an explicit offset", () => {
    expect(parseTs("2026-09-20T10:00:00+02:00")?.toISOString()).toBe("2026-09-20T08:00:00.000Z");
    expect(parseTs("2026-09-20T10:00:00Z")?.toISOString()).toBe("2026-09-20T10:00:00.000Z");
  });
  it("returns null for empty or garbage input", () => {
    expect(parseTs(null)).toBeNull();
    expect(parseTs("")).toBeNull();
    expect(parseTs("not a date")).toBeNull();
  });
});

describe("formatDuration", () => {
  it("uses the two largest units, compact", () => {
    expect(formatDuration(0, "en")).toBe("0 s");
    expect(formatDuration(42, "en")).toBe("42 s");
    expect(formatDuration(59.6, "en")).toBe("59 s");
    expect(formatDuration(60, "en")).toBe("1 min");
    expect(formatDuration(45 * 60 + 10, "en")).toBe("45 min");
    expect(formatDuration(3600, "en")).toBe("1 h");
    expect(formatDuration(2 * 3600 + 5 * 60, "en")).toBe("2 h 5 min");
    expect(formatDuration(2 * 86400, "en")).toBe("2 d");
    expect(formatDuration(3 * 86400 + 2 * 3600 + 59 * 60, "en")).toBe("3 d 2 h");
  });
  it("uses German day unit in de", () => {
    expect(formatDuration(3 * 86400 + 2 * 3600, "de")).toBe("3 T 2 h");
  });
  it("returns null for missing or negative input", () => {
    expect(formatDuration(null, "en")).toBeNull();
    expect(formatDuration(undefined, "en")).toBeNull();
    expect(formatDuration(-5, "en")).toBeNull();
  });
});

describe("secondsBetween", () => {
  it("measures from start to end", () => {
    expect(secondsBetween("2026-09-20T10:00:00", "2026-09-20T12:30:00")).toBe(9000);
  });
  it("uses `now` when no end is given", () => {
    const now = new Date("2026-09-20T10:01:00Z");
    expect(secondsBetween("2026-09-20T10:00:00", null, now)).toBe(60);
  });
  it("returns null without a start", () => {
    expect(secondsBetween(null, "2026-09-20T10:00:00")).toBeNull();
  });
});

describe("formatAgeRounded", () => {
  it("rounds to a single unit instead of formatDuration's floored two-unit combo (review finding on PR #756)", () => {
    const now = new Date("2026-10-04T12:00:00Z");
    // 2 d 23 h — formatDuration would floor this to "2 d 23 h"; K10's
    // relative-time form wants the nearest whole day, "3 d".
    const ts = "2026-10-01T13:00:00Z";
    expect(formatAgeRounded(ts, "en", now)).toBe("3 d");
    expect(formatDuration(secondsBetween(ts, null, now)!, "en")).toBe("2 d 23 h");
  });
  it("picks the dominant unit at each boundary", () => {
    const now = new Date("2026-10-04T12:00:00Z");
    expect(formatAgeRounded("2026-10-04T11:50:00Z", "en", now)).toBe("10 min");
    expect(formatAgeRounded("2026-10-04T09:10:00Z", "en", now)).toBe("3 h");
    expect(formatAgeRounded("2026-10-04T11:59:40Z", "en", now)).toBe("20 s");
  });
  it("spells German days out in full, correctly declined for 'vor …' (K10, review finding on PR #756 round 3)", () => {
    // K10's own fixed form is "seit 5 Tagen" — the "T" abbreviation
    // `formatDuration` uses for a standalone duration ("3 T 2 h") read as a
    // typo once embedded in "vor {age}". Dative plural "Tagen" for more
    // than one day, dative singular "Tag" (no "-en") for exactly one.
    const now = new Date("2026-10-04T12:00:00Z");
    expect(formatAgeRounded("2026-10-01T12:00:00Z", "de", now)).toBe("3 Tagen");
    expect(formatAgeRounded("2026-10-03T12:00:00Z", "de", now)).toBe("1 Tag");
    expect(formatAgeRounded("2026-10-01T12:00:00Z", "en", now)).toBe("3 d");
  });
  it("returns null without a parseable timestamp", () => {
    expect(formatAgeRounded(null)).toBeNull();
    expect(formatAgeRounded(undefined)).toBeNull();
  });
});

describe("formatUsd", () => {
  it("formats cents and marks sub-cent amounts", () => {
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd(0.004)).toBe("<$0.01");
    expect(formatUsd(0.05)).toBe("$0.05");
    expect(formatUsd(12.345)).toBe("$12.35");
  });
});
