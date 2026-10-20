/**
 * Onboarding metrics (Welle 3, PR 3-4) — times a handful of first-run
 * moments, stored under the existing per-user `settings` KV, nothing sent
 * anywhere outward. See lib/onboardingMetrics.ts for the privacy rule this
 * enforces (fixed error codes only, never raw text).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { AUTH_TOKEN_KEY, api } from "@/lib/api";
import {
  _resetOnboardingStartForTests,
  flushPendingOnboardingMetrics,
  getOnboardingMetrics,
  markOnboardingStart,
  recordOnboardingError,
  recordOnboardingMilestone,
} from "@/lib/onboardingMetrics";

beforeEach(() => {
  vi.restoreAllMocks();
  _resetOnboardingStartForTests();
  window.localStorage.clear();
  // Every milestone/error call site other than login/page.tsx's catch block
  // (first_key_green, first_agent_online, first_question_card, and the
  // post-login "login" milestone itself) runs with a session already in
  // place — set the token here so these tests match that, and let the
  // "without a session token" tests below clear it explicitly.
  window.localStorage.setItem(AUTH_TOKEN_KEY, "test-token");
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

// Bug #870: a wrong password/registration attempt has no session token yet
// (login/page.tsx calls recordOnboardingError from its catch block, before
// any token is stored). lib/api.ts treats a 401 outside "/auth/" as an
// expired session and sends the whole page to /login, which would wipe the
// error message the operator was just shown and lose the code. These tests
// run the real request() (mocked fetch, not a mocked api.settings) to prove
// no 401 round-trip — and so no redirect — happens while logged out.
describe("recordOnboardingError without a session token", () => {
  const realFetch = global.fetch;
  const realLocation = window.location;

  beforeEach(() => {
    // The outer beforeEach sets a token for the (post-login) default case —
    // remove it: this describe block is the pre-login state.
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      text: async () => "Unauthorized",
      json: async () => ({ detail: "Unauthorized" }),
    } as unknown as Response);
    // jsdom's window.location.href is not assignable without this.
    Object.defineProperty(window, "location", {
      value: { ...realLocation, href: realLocation.href },
      writable: true,
    });
  });

  afterEach(() => {
    global.fetch = realFetch;
    Object.defineProperty(window, "location", {
      value: realLocation,
      writable: true,
    });
  });

  it("never calls the API, so the 401 handler in lib/api.ts never fires", async () => {
    expect(window.localStorage.getItem(AUTH_TOKEN_KEY)).toBeNull();

    await recordOnboardingError("login_failed");

    expect(global.fetch).not.toHaveBeenCalled();
    expect(window.location.href).toBe(realLocation.href);
  });

  it("buffers the error locally instead", async () => {
    await recordOnboardingError("login_failed");

    const raw = window.localStorage.getItem("mc_onboarding_pending");
    expect(raw).not.toBeNull();
    const pending = JSON.parse(raw!);
    expect(pending.errors).toEqual([{ code: "login_failed", at: expect.any(String) }]);
  });
});

describe("flushPendingOnboardingMetrics", () => {
  it("merges a buffered pre-login error into the real metrics once a session exists", async () => {
    // Step 1: no token — buffers locally (same as login/page.tsx's catch).
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
    await recordOnboardingError("login_failed");
    expect(window.localStorage.getItem("mc_onboarding_pending")).not.toBeNull();

    // Step 2: token now exists (login succeeded) — flush.
    window.localStorage.setItem(AUTH_TOKEN_KEY, "tok");
    let stored: any = { t0: null, elapsed_ms: {}, errors: [] };
    vi.spyOn(api.settings, "get").mockImplementation(async () => ({
      key: "onboarding_metrics",
      value: stored,
    }));
    const setSpy = vi.spyOn(api.settings, "set").mockImplementation(async (_k, v) => {
      stored = v;
      return { key: "onboarding_metrics", value: v };
    });

    await flushPendingOnboardingMetrics();

    expect(setSpy).toHaveBeenCalledTimes(1);
    expect(stored.errors).toEqual([{ code: "login_failed", at: expect.any(String) }]);
    expect(window.localStorage.getItem("mc_onboarding_pending")).toBeNull();
  });

  it("is a no-op when nothing was buffered", async () => {
    window.localStorage.setItem(AUTH_TOKEN_KEY, "tok");
    const setSpy = vi.spyOn(api.settings, "set");

    await flushPendingOnboardingMetrics();

    expect(setSpy).not.toHaveBeenCalled();
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
