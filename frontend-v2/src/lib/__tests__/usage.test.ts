import { describe, expect, it } from "vitest";
import {
  formatShare,
  formatUsd,
  sourceLabel,
  topRoutes,
  topSources,
  usageWeekOf,
  weekNumber,
  type DailyMetrics,
  type UsageWeek,
} from "../usage";
import { mkUsageWeek, mkSource, mkDaily } from "./usageFixtures";

describe("usage helpers", () => {
  it("reads the ISO week number", () => {
    expect(weekNumber("2026-W09")).toBe(9);
    expect(weekNumber("2026-W40")).toBe(40);
  });

  it("maps source buckets to i18n keys", () => {
    expect(sourceLabel("operator")).toEqual({ key: "operator" });
    expect(sourceLabel("lead")).toEqual({ key: "lead" });
    expect(sourceLabel("agents:cli-bridge")).toEqual({ key: "agents", harness: "cli-bridge" });
    expect(sourceLabel("heads:local")).toEqual({ key: "headsLocal" });
    expect(sourceLabel("heads:cloud")).toEqual({ key: "headsCloud" });
    expect(sourceLabel("heads:unknown")).toEqual({ key: "headsUnknown" });
    expect(sourceLabel("unattributed")).toEqual({ key: "unattributed" });
    expect(sourceLabel("something-new")).toEqual({ key: "unattributed" });
  });

  it("picks the top sources by cost, then tokens, and drops empty ones", () => {
    const week: UsageWeek = mkUsageWeek({
      sources: [
        mkSource("lead", 5, 100),
        mkSource("operator", 40, 10),
        mkSource("heads:local", 0, 900),
        mkSource("unattributed", 0, 0),
      ],
    });
    expect(topSources(week, 3).map((s) => s.source)).toEqual(["operator", "lead", "heads:local"]);
    expect(topSources(week, 1).map((s) => s.source)).toEqual(["operator"]);
    expect(topSources(week, 10).map((s) => s.source)).not.toContain("unattributed");
    expect(topSources(undefined)).toEqual([]);
  });

  it("picks the top routes by views", () => {
    const week = { week: "2026-W40", week_start: "2026-09-28", total: 9, dropped: 0, routes: { "/": 2, "/sessions": 5, "/tasks": 1, "/inbox": 1 } };
    expect(topRoutes(week, 2)).toEqual([["/sessions", 5], ["/", 2]]);
    expect(topRoutes(undefined)).toEqual([]);
  });

  it("formats money and shares per locale", () => {
    expect(formatUsd(1234.4, "en")).toBe("$1,234");
    expect(formatUsd(12.345, "en")).toBe("$12.35");
    expect(formatShare(0.049, "en")).toBe("4.9%");
    expect(formatShare(0.25, "en")).toBe("25%");
    expect(formatShare(null, "en")).toBeNull();
  });

  it("returns the usage week only when there is one", () => {
    const base: DailyMetrics = mkDaily();
    expect(usageWeekOf(base)?.week).toBe("2026-W40");
    expect(usageWeekOf({ ...base, usage_week: null })).toBeNull();
    expect(usageWeekOf({ ...base, usage_week: { error: true } })).toBeNull();
    expect(usageWeekOf(undefined)).toBeNull();
  });
});
