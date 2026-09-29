import { describe, it, expect } from "vitest";
import { formatDuration, secondsBetween, formatUsd, parseTs } from "../format";

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

describe("formatUsd", () => {
  it("formats cents and marks sub-cent amounts", () => {
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd(0.004)).toBe("<$0.01");
    expect(formatUsd(0.05)).toBe("$0.05");
    expect(formatUsd(12.345)).toBe("$12.35");
  });
});
