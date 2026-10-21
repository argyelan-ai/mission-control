/**
 * One clock format for "when did this happen" on a card (critic, incident
 * 21.10.2026: the chat's new-conversation card had built a second one with
 * another English form than the runtimes stage's crash card).
 */
import { describe, it, expect } from "vitest";
import { formatClockTime } from "../clockTime";
import { formatCrashTime } from "@/app/runtimes/stage/crashFormat";

describe("formatClockTime", () => {
  const now = new Date(2026, 9, 19, 21, 0);
  it("today → clock time only", () => {
    const at = new Date(2026, 9, 19, 17, 20).toISOString();
    expect(formatClockTime(at, "en", now)).toBe("17:20");
    expect(formatClockTime(at, "de", now)).toBe("17:20");
  });
  it("another day → day, month and time, day first in both languages", () => {
    const at = new Date(2026, 9, 18, 17, 20).toISOString();
    expect(formatClockTime(at, "en", now)).toMatch(/^18 Oct.*17:20$/);
    expect(formatClockTime(at, "de", now)).toMatch(/^18\. Okt.*17:20$/);
  });
  it("no or broken value → null, never an invented time", () => {
    expect(formatClockTime(null, "en", now)).toBeNull();
    expect(formatClockTime(undefined, "en", now)).toBeNull();
    expect(formatClockTime("not a date", "en", now)).toBeNull();
  });
  it("is the stage's crash time — one implementation, not two", () => {
    expect(formatCrashTime).toBe(formatClockTime);
  });
});
