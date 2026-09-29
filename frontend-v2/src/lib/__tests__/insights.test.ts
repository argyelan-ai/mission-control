/** Insights page helpers: heatmap grid, colour levels, week comparison, source split. */
import { describe, expect, it } from "vitest";
import {
  heatLevels,
  heatmapGrid,
  sourceSplit,
  weekSoFar,
  fullWeeks,
  shiftDay,
} from "../insights";
import { mkUsageByDay, mkDay, mkUsageByWeek, mkSource, mkUsageWeek } from "./usageFixtures";

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
