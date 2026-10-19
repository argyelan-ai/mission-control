import { describe, it, expect } from "vitest";
import { crashReasonKey, formatCrashTime } from "../crashFormat";

describe("formatCrashTime", () => {
  const now = new Date(2026, 9, 19, 21, 0);
  it("today → clock time only", () => {
    const at = new Date(2026, 9, 19, 17, 20).toISOString();
    expect(formatCrashTime(at, "en", now)).toBe("17:20");
    expect(formatCrashTime(at, "de", now)).toBe("17:20");
  });
  it("another day → day, month and time", () => {
    const at = new Date(2026, 9, 18, 17, 20).toISOString();
    expect(formatCrashTime(at, "en", now)).toMatch(/18.*Oct.*17:20/);
    expect(formatCrashTime(at, "de", now)).toMatch(/18.*Okt.*17:20/);
  });
  it("no or broken value → null, never an invented time", () => {
    expect(formatCrashTime(null, "en", now)).toBeNull();
    expect(formatCrashTime("not a date", "en", now)).toBeNull();
  });
});

describe("crashReasonKey", () => {
  it("passes known codes, maps anything else to unknown", () => {
    expect(crashReasonKey("killed")).toBe("killed");
    expect(crashReasonKey("memguard")).toBe("memguard");
    expect(crashReasonKey("something_new")).toBe("unknown");
    expect(crashReasonKey(null)).toBe("unknown");
  });
});
