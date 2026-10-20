/**
 * "Copy setup report" (Welle 3, PR 3-4). Privacy-focused: the report must
 * never surface a raw server error, a URL, a reason code or a secret value
 * — only status words, counts and the onboarding timings/error codes from
 * lib/onboardingMetrics.ts.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { api } from "@/lib/api";
import * as onboardingMetrics from "@/lib/onboardingMetrics";
import { buildSetupReport, formatDurationMs, type SetupReportLabels } from "@/lib/setupReport";

const labels: SetupReportLabels = {
  heading: "Mission Control setup report",
  version: "Version",
  unknown: "unknown",
  health: "Health",
  database: "database",
  redis: "redis",
  agentsOnline: "Agents online",
  keysHeading: "Keys",
  noKeys: "No keys configured",
  keyState: { ok: "ok", fail: "fail", never_tested: "never tested" },
  timesHeading: "Onboarding times",
  milestone: {
    login: "Login",
    first_key_green: "First key green",
    first_agent_online: "First agent online",
    first_question_card: "First question card",
  },
  notYet: "not yet",
  errorsHeading: "Recent error codes",
  noErrors: "None",
};

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("formatDurationMs", () => {
  it("formats under a minute as seconds", () => {
    expect(formatDurationMs(42_000)).toBe("42s");
  });

  it("formats a minute or more as minutes and seconds", () => {
    expect(formatDurationMs(125_000)).toBe("2m 5s");
  });
});

describe("buildSetupReport", () => {
  it("includes version, health, key status, times and errors", async () => {
    vi.spyOn(api.system, "version").mockResolvedValue({
      current: "0.9.0",
      latest: null,
      release_url: null,
      update_available: false,
    });
    vi.spyOn(api.system, "status").mockResolvedValue({
      status: "healthy",
      components: {
        database: { status: "ok" },
        redis: { status: "ok" },
        gateway: { status: "ok" },
        watchdog: { status: "ok" },
      },
      resources: null,
      agents: { total: 3, online: 1, offline: 2 },
      uptime_seconds: 120,
      version: "0.9.0",
    } as any);
    vi.spyOn(api.configChecks, "list").mockResolvedValue({
      kinds: [],
      cost_hints: {} as any,
      items: [
        {
          kind: "secret",
          subject_id: "anthropic_api_key",
          label: "Anthropic API key",
          secret_key: "anthropic_api_key",
          configured: true,
          testable: true,
          cost: "free",
          state: "ok",
          reason: null,
          reason_params: {},
          message: null,
          duration_ms: 12,
          checked_at: "2026-10-19T10:00:00.000Z",
          age_seconds: 10,
          value_changed: false,
        },
      ],
    });
    vi.spyOn(onboardingMetrics, "getOnboardingMetrics").mockResolvedValue({
      t0: "2026-10-19T09:00:00.000Z",
      elapsed_ms: { login: 42_000 },
      errors: [{ code: "rejected", at: "2026-10-19T09:01:00.000Z" }],
    });

    const report = await buildSetupReport(labels);

    expect(report).toContain("Mission Control setup report");
    expect(report).toContain("Version: 0.9.0");
    expect(report).toContain("Health: healthy (database: ok, redis: ok)");
    expect(report).toContain("Agents online: 1/3");
    expect(report).toContain("Anthropic API key: ok");
    expect(report).toContain("Login: 42s");
    expect(report).toContain("First key green: not yet");
    expect(report).toContain("rejected @ 2026-10-19T09:01:00.000Z");
    // Never includes a raw server error, a URL or a reason code.
    expect(report).not.toContain("http");
    expect(report).not.toContain("reason");
  });

  it("never throws when every source fails (degraded/offline install)", async () => {
    vi.spyOn(api.system, "version").mockRejectedValue(new Error("offline"));
    vi.spyOn(api.system, "status").mockRejectedValue(new Error("offline"));
    vi.spyOn(api.configChecks, "list").mockRejectedValue(new Error("offline"));
    vi.spyOn(onboardingMetrics, "getOnboardingMetrics").mockRejectedValue(new Error("offline"));

    const report = await buildSetupReport(labels);

    expect(report).toContain("Version: unknown");
    expect(report).toContain("Health: unknown");
    expect(report).toContain("No keys configured");
    expect(report).toContain("None");
  });

  it("shows 'no keys configured' when the secret registry is empty", async () => {
    vi.spyOn(api.system, "version").mockResolvedValue({
      current: "0.9.0",
      latest: null,
      release_url: null,
      update_available: false,
    });
    vi.spyOn(api.system, "status").mockRejectedValue(new Error("offline"));
    vi.spyOn(api.configChecks, "list").mockResolvedValue({ kinds: [], cost_hints: {} as any, items: [] });
    vi.spyOn(onboardingMetrics, "getOnboardingMetrics").mockResolvedValue({
      t0: null,
      elapsed_ms: {},
      errors: [],
    });

    const report = await buildSetupReport(labels);
    expect(report).toContain("No keys configured");
  });
});
