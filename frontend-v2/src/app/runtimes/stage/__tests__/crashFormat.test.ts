import { describe, it, expect } from "vitest";
import { crashLikelyOutOfMemory, crashReasonKey, formatCrashTime } from "../crashFormat";
import en from "../../../../../messages/en.json";
import de from "../../../../../messages/de.json";

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
  it("passes ADR-096 codes, maps anything else to unknown", () => {
    expect(crashReasonKey("oom")).toBe("oom");
    expect(crashReasonKey("crash_loop")).toBe("crash_loop");
    expect(crashReasonKey("memguard")).toBe("unknown"); // not an ADR code
    expect(crashReasonKey("crashed")).toBe("unknown");
    expect(crashReasonKey(null)).toBe("unknown");
  });
  it("an exit with a code gets the code wording", () => {
    expect(crashReasonKey("exit", 137)).toBe("exitCode");
    expect(crashReasonKey("exit", null)).toBe("exit");
  });
});

describe("crashLikelyOutOfMemory", () => {
  // Exit 137 is a SIGKILL: on a GPU box almost always the kernel OOM killer
  // or a recipe's memory guard. The card adds "probably out of memory" —
  // a hint on the "exit" reason only, never a second reason code.
  it("exit 137 → the hint", () => {
    expect(crashLikelyOutOfMemory("exit", 137)).toBe(true);
  });
  it("any other exit code, or none → no hint", () => {
    expect(crashLikelyOutOfMemory("exit", 1)).toBe(false);
    expect(crashLikelyOutOfMemory("exit", 0)).toBe(false);
    expect(crashLikelyOutOfMemory("exit", null)).toBe(false);
    expect(crashLikelyOutOfMemory("exit", undefined)).toBe(false);
  });
  it("reasons that already name memory (or another cause) never get it twice", () => {
    for (const code of ["oom", "kernel_oom", "guard", "member_down", "crash_loop", "unknown", null]) {
      expect(crashLikelyOutOfMemory(code, 137)).toBe(false);
    }
  });
});

describe("memory hint wording (operator decision 2026-10-20)", () => {
  const stage = (m: unknown) =>
    ((m as Record<string, Record<string, Record<string, string>>>).runtimes.stage);
  it("DE and EN wrap the resolved reason and add the hint", () => {
    expect(stage(de).crashReasonLikelyOom).toBe("{reason}, vermutlich Speicher voll");
    expect(stage(en).crashReasonLikelyOom).toBe("{reason}, probably out of memory");
  });
});
