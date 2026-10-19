/**
 * Onboarding metrics (Welle 3, PR 3-4) — times a handful of first-run
 * moments, stored under the existing per-user `settings` KV, nothing sent
 * anywhere outward. See lib/onboardingMetrics.ts for the privacy rule this
 * enforces (fixed error codes only, never raw text).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { api } from "@/lib/api";
import {
  _resetOnboardingStartForTests,
  getOnboardingMetrics,
  markOnboardingStart,
  recordOnboardingError,
  recordOnboardingMilestone,
} from "@/lib/onboardingMetrics";

beforeEach(() => {
  vi.restoreAllMocks();
  _resetOnboardingStartForTests();
  window.localStorage.clear();
});

describe("markOnboardingStart", () => {
  it("sets the t0 marker once", () => {
    markOnboardingStart();
    const first = window.localStorage.getItem("mc_onboarding_t0");
    expect(first).not.toBeNull();
    markOnboardingStart();
    expect(window.localStorage.getItem("mc_onboarding_t0")).toBe(first);
  });
});

describe("recordOnboardingMilestone", () => {
  it("stores an elapsed time relative to t0 for a fresh install", async () => {
    vi.spyOn(api.settings, "get").mockRejectedValue(new Error("API 404: Setting not found"));
    const setSpy = vi.spyOn(api.settings, "set").mockResolvedValue({ key: "x", value: {} });

    markOnboardingStart();
    await recordOnboardingMilestone("login");

    expect(setSpy).toHaveBeenCalledTimes(1);
    const [key, value] = setSpy.mock.calls[0];
    expect(key).toBe("onboarding_metrics");
    expect(typeof (value as any).elapsed_ms.login).toBe("number");
    expect((value as any).elapsed_ms.login).toBeGreaterThanOrEqual(0);
  });

  it("keeps the first time and never overwrites a milestone", async () => {
    let stored: any = { t0: new Date().toISOString(), elapsed_ms: { login: 42 }, errors: [] };
    vi.spyOn(api.settings, "get").mockImplementation(async () => ({ key: "onboarding_metrics", value: stored }));
    const setSpy = vi.spyOn(api.settings, "set").mockImplementation(async (_k, v) => {
      stored = v;
      return { key: "onboarding_metrics", value: v };
    });

    await recordOnboardingMilestone("login");

    expect(setSpy).not.toHaveBeenCalled();
    expect(stored.elapsed_ms.login).toBe(42);
  });

  it("falls back to 'now' as t0 when no browser marker exists, instead of throwing", async () => {
    vi.spyOn(api.settings, "get").mockRejectedValue(new Error("API 404: Setting not found"));
    vi.spyOn(api.settings, "set").mockResolvedValue({ key: "x", value: {} });

    await expect(recordOnboardingMilestone("first_agent_online")).resolves.toBeUndefined();
  });

  it("never throws when the backend is unreachable", async () => {
    vi.spyOn(api.settings, "get").mockRejectedValue(new Error("offline"));
    vi.spyOn(api.settings, "set").mockRejectedValue(new Error("offline"));

    await expect(recordOnboardingMilestone("first_key_green")).resolves.toBeUndefined();
  });
});

describe("recordOnboardingError", () => {
  it("appends a fixed code with a timestamp", async () => {
    vi.spyOn(api.settings, "get").mockRejectedValue(new Error("API 404: Setting not found"));
    const setSpy = vi.spyOn(api.settings, "set").mockResolvedValue({ key: "x", value: {} });

    await recordOnboardingError("rejected");

    const [, value] = setSpy.mock.calls[0];
    expect((value as any).errors).toEqual([{ code: "rejected", at: expect.any(String) }]);
  });

  it("caps the error list at the most recent 10 entries", async () => {
    let stored: any = {
      t0: null,
      elapsed_ms: {},
      errors: Array.from({ length: 10 }, (_, i) => ({ code: `old_${i}`, at: "2026-01-01T00:00:00.000Z" })),
    };
    vi.spyOn(api.settings, "get").mockImplementation(async () => ({ key: "onboarding_metrics", value: stored }));
    vi.spyOn(api.settings, "set").mockImplementation(async (_k, v) => {
      stored = v;
      return { key: "onboarding_metrics", value: v };
    });

    await recordOnboardingError("newest");

    expect(stored.errors).toHaveLength(10);
    expect(stored.errors[9]).toEqual({ code: "newest", at: expect.any(String) });
    expect(stored.errors[0].code).toBe("old_1");
  });

  it("never throws when the backend is unreachable", async () => {
    vi.spyOn(api.settings, "get").mockRejectedValue(new Error("offline"));
    vi.spyOn(api.settings, "set").mockRejectedValue(new Error("offline"));

    await expect(recordOnboardingError("timeout")).resolves.toBeUndefined();
  });
});

describe("getOnboardingMetrics", () => {
  it("returns an empty shape on a fresh install (404)", async () => {
    vi.spyOn(api.settings, "get").mockRejectedValue(new Error("API 404: Setting not found"));

    expect(await getOnboardingMetrics()).toEqual({ t0: null, elapsed_ms: {}, errors: [] });
  });

  it("passes through a stored value", async () => {
    const stored = { t0: "2026-10-19T10:00:00.000Z", elapsed_ms: { login: 1000 }, errors: [] };
    vi.spyOn(api.settings, "get").mockResolvedValue({ key: "onboarding_metrics", value: stored });

    expect(await getOnboardingMetrics()).toEqual(stored);
  });
});
