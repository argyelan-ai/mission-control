import { describe, it, expect } from "vitest";
import { formatUptimeParts, pad2 } from "../uptimeFormat";

describe("formatUptimeParts", () => {
  const now = Date.parse("2026-09-07T12:00:00Z");

  it("< 1h → hours 0, minutes < 60", () => {
    const since = new Date(now - 34 * 60_000).toISOString();
    expect(formatUptimeParts(since, now)).toEqual({ hours: 0, minutes: 34 });
  });

  it(">= 1h → hours/minutes split (2h41)", () => {
    const since = new Date(now - (2 * 60 + 41) * 60_000).toISOString();
    expect(formatUptimeParts(since, now)).toEqual({ hours: 2, minutes: 41 });
  });

  it("exactly on the hour → minutes 0", () => {
    const since = new Date(now - 3 * 60 * 60_000).toISOString();
    expect(formatUptimeParts(since, now)).toEqual({ hours: 3, minutes: 0 });
  });

  it("floors partial minutes (no rounding up)", () => {
    const since = new Date(now - 59_999).toISOString(); // 59.999s ago
    expect(formatUptimeParts(since, now)).toEqual({ hours: 0, minutes: 0 });
  });

  it("null/undefined → null (state word fallback, no invented duration)", () => {
    expect(formatUptimeParts(null, now)).toBeNull();
    expect(formatUptimeParts(undefined, now)).toBeNull();
  });

  it("unparsable string → null", () => {
    expect(formatUptimeParts("not-a-date", now)).toBeNull();
  });

  it("empty string → null", () => {
    expect(formatUptimeParts("", now)).toBeNull();
  });

  it("a timestamp in the future (clock drift) → null, never negative uptime", () => {
    const since = new Date(now + 60_000).toISOString();
    expect(formatUptimeParts(since, now)).toBeNull();
  });

  it("defaults `now` to Date.now() when omitted", () => {
    const since = new Date(Date.now() - 5 * 60_000).toISOString();
    const result = formatUptimeParts(since);
    expect(result).not.toBeNull();
    expect(result!.hours).toBe(0);
    expect(result!.minutes).toBeGreaterThanOrEqual(4);
    expect(result!.minutes).toBeLessThanOrEqual(5);
  });
});

describe("pad2", () => {
  it("pads single digits to two", () => {
    expect(pad2(3)).toBe("03");
  });

  it("leaves two-digit numbers unchanged", () => {
    expect(pad2(41)).toBe("41");
  });

  it("leaves zero as '00'", () => {
    expect(pad2(0)).toBe("00");
  });
});
