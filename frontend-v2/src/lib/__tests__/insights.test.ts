/** Insights page helpers: heatmap grid, colour levels, week comparison, source split. */
import { describe, expect, it } from "vitest";
import {
  heatLevels,
  heatmapGrid,
  sourceSplit,
  weekSoFar,
  fullWeeks,
  shiftDay,
  boxSplit,
  dayTotals,
} from "../insights";
import { mkUsageByDay, mkDay, mkUsageByWeek, mkSource, mkUsageWeek, mkBox } from "./usageFixtures";

describe("heatLevels", () => {
  it("gives 0 to empty days and splits the rest into four equal groups", () => {
    const level = heatLevels([0, 0, 1, 2, 3, 4, 5, 6, 7, 8]);
    expect(level(0)).toBe(0);
    expect([1, 2, 3, 4, 5, 6, 7, 8].map(level)).toEqual([1, 1, 2, 2, 3, 3, 4, 4]);
  });

  it("keeps one outlier day from greying out all the others", () => {
    const level = heatLevels([10, 11, 12, 13, 5_000_000]);
    expect(level(13)).toBeGreaterThanOrEqual(3);
    expect(level(5_000_000)).toBe(4);
  });

  it("is 0 everywhere when nothing was used", () => {
    expect(heatLevels([0, 0, 0])(0)).toBe(0);
  });
});

describe("heatmapGrid", () => {
  // mkUsageByDay: 40 days ending Wednesday 2026-09-30
  const data = mkUsageByDay();

  it("builds Monday-first week columns ending in today's week", () => {
    const grid = heatmapGrid(data.days, 4, "all");
    expect(grid.columns).toHaveLength(4);
    expect(grid.columns.every((c) => c.length === 7)).toBe(true);
    expect(grid.columns[0][0]?.date).toBe("2026-09-07"); // a Monday
    const last = grid.columns[3];
    expect(last[0]?.date).toBe("2026-09-28");
    expect(last[2]?.date).toBe("2026-09-30"); // today
    expect(last[3]).toBeNull(); // Thursday is still to come
  });

  it("marks days before the data window as empty cells, not missing", () => {
    const grid = heatmapGrid(data.days.slice(-3), 2, "all");
    expect(grid.columns[0][0]).toEqual({ date: "2026-09-21", value: 0, level: 0, inWindow: false });
  });

  it("colours by generated tokens, or only the local ones", () => {
    const days = [mkDay("2026-09-28", 1000, 0), mkDay("2026-09-29", 10, 10), mkDay("2026-09-30", 0, 0)];
    const all = heatmapGrid(days, 1, "all").columns[0];
    const local = heatmapGrid(days, 1, "local").columns[0];
    expect(all.slice(0, 3).map((c) => c?.value)).toEqual([1000, 10, 0]);
    expect(local.slice(0, 3).map((c) => c?.value)).toEqual([0, 10, 0]);
    expect(local[1]?.level).toBe(4);
  });

  it("adds the boxes' direct API tokens to a day in both modes", () => {
    // 1000 via MC (100 of them local) + 500 sent straight to a box's API.
    const withBox = { ...mkDay("2026-09-29", 1000, 100), ...mkBox(600, 100) };
    const days = [mkDay("2026-09-28", 1000, 0), withBox, mkDay("2026-09-30", 0, 0)];
    const all = heatmapGrid(days, 1, "all").columns[0];
    const local = heatmapGrid(days, 1, "local").columns[0];
    expect(all.slice(0, 3).map((c) => c?.value)).toEqual([1000, 1500, 0]);
    expect(local.slice(0, 3).map((c) => c?.value)).toEqual([0, 600, 0]);
  });

  it("labels the column that holds the first of a month", () => {
    const grid = heatmapGrid(data.days, 6, "all");
    expect(grid.months).toEqual([{ col: 1, month: 8 }]); // 1 Sep; 1 Oct is still to come
  });
});

describe("weekSoFar", () => {
  it("compares this week so far with the same weekdays of last week", () => {
    const days = [
      mkDay("2026-09-21", 0, 0, 100), mkDay("2026-09-22", 0, 0, 100), mkDay("2026-09-23", 0, 0, 100),
      mkDay("2026-09-24", 0, 0, 999),
      mkDay("2026-09-28", 0, 0, 50), mkDay("2026-09-29", 0, 0, 100), mkDay("2026-09-30", 0, 0, 0),
    ];
    const w = weekSoFar(days);
    expect(w.cost).toBe(150);
    expect(w.previousCost).toBe(300); // Mon–Wed of last week, not the whole week
    expect(w.change).toBeCloseTo(-0.5);
  });

  it("has no change when last week had nothing", () => {
    expect(weekSoFar([mkDay("2026-09-30", 0, 0, 20)]).change).toBeNull();
  });
});

describe("fullWeeks", () => {
  it("leaves the running week out of a trend line", () => {
    const weeks = fullWeeks(mkUsageByWeek().weeks);
    expect(weeks.map((w) => w.week)).toEqual(["2026-W35", "2026-W36", "2026-W37", "2026-W38", "2026-W39"]);
  });
});

describe("sourceSplit", () => {
  it("keeps the biggest sources and folds the rest into one row", () => {
    const week = mkUsageWeek({
      sources: [
        mkSource("operator", 60, 1), mkSource("lead", 20, 1), mkSource("heads:local", 10, 1),
        mkSource("agents:cli-bridge", 6, 1), mkSource("agents:grok", 3, 1), mkSource("unattributed", 1, 1),
      ],
    });
    const split = sourceSplit(week, 4);
    expect(split.rows.map((r) => r.source)).toEqual(["operator", "lead", "heads:local", "agents:cli-bridge"]);
    expect(split.rest).toEqual({ count: 2, cost_usd: 4, share: 0.04 });
    expect(split.rows[0].share).toBeCloseTo(0.6);
  });

  it("returns nothing for a week without cost", () => {
    expect(sourceSplit(mkUsageWeek({ sources: [] })).rows).toEqual([]);
  });
});

describe("shiftDay", () => {
  it("moves inside the window and stops at its ends", () => {
    const days = mkUsageByDay().days;
    expect(shiftDay(days, "2026-09-29", 1)).toBe("2026-09-30");
    expect(shiftDay(days, "2026-09-30", 1)).toBe("2026-09-30");
    expect(shiftDay(days, days[0].date, -1)).toBe(days[0].date);
  });
});

describe("boxSplit", () => {
  it("splits the box total into via MC and external, cached apart", () => {
    const week = mkUsageWeek({ totals: { ...mkUsageWeek().totals, ...mkBox(1_200_000, 480_000, 14_800_000) } });
    const s = boxSplit(week)!;
    expect(s.total).toBe(1_200_000);
    expect(s.viaMc).toBe(480_000);
    expect(s.external).toBe(720_000);
    expect(s.cached).toBe(14_800_000);
    expect(s.externalShare).toBeCloseTo(0.6);
    expect(s.clamped).toBe(false);
  });

  it("via MC never exceeds the box total when MC counted more", () => {
    const week = mkUsageWeek({ totals: { ...mkUsageWeek().totals, ...mkBox(100, 500, 0, true) } });
    const s = boxSplit(week)!;
    expect(s.viaMc).toBe(100);
    expect(s.external).toBe(0);
    expect(s.clamped).toBe(true);
  });

  it("is null before counting started", () => {
    expect(boxSplit(mkUsageWeek())).toBeNull();
    expect(boxSplit(undefined)).toBeNull();
  });
});

describe("dayTotals", () => {
  it("counts direct API tokens as local and keeps the local share consistent", () => {
    const t = dayTotals({ ...mkDay("2026-09-29", 1000, 100), ...mkBox(600, 100) });
    expect(t).toEqual({ all: 1500, local: 600, external: 500, localShare: 0.4 });
  });

  it("is MC's own numbers on days without box data (no made-up numbers)", () => {
    expect(dayTotals(mkDay("2026-09-29", 1000, 100))).toEqual({ all: 1000, local: 100, external: 0, localShare: 0.1 });
    const before = { ...mkDay("2026-09-29", 1000, 100), box: null, via_mc_local: null, external: null };
    expect(dayTotals(before)).toEqual({ all: 1000, local: 100, external: 0, localShare: 0.1 });
  });

  it("has a day used only through the direct API", () => {
    expect(dayTotals({ ...mkDay("2026-09-29", 0, 0), ...mkBox(800, 0) })).toEqual({
      all: 800, local: 800, external: 800, localShare: 1,
    });
    expect(dayTotals(mkDay("2026-09-29", 0, 0)).localShare).toBeNull();
  });
});
