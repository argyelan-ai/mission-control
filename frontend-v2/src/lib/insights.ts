/**
 * Insights page: pure helpers for the heatmap (tokens per day), the week
 * comparison in the hero and the source split. Backend:
 * /api/v1/intelligence/costs/by-day and /by-week (app/services/usage_baseline.py),
 * both counted in the browser's time zone.
 */

import type { UsageTotals, UsageWeek } from "./usage";

export interface UsageDay extends UsageTotals {
  date: string; // "2026-09-30", a calendar day in `tz`
  generated_tokens: number; // input + output, no cache
  local_generated_tokens: number;
  top_source: string | null;
}

export interface UsageByDay {
  generated_at: string;
  start: string;
  tz: string;
  days: UsageDay[]; // every day of the window, oldest first, today last
}

export type HeatMetric = "all" | "local";
export type HeatLevel = 0 | 1 | 2 | 3 | 4;

export interface HeatCell {
  date: string;
  value: number;
  level: HeatLevel;
  inWindow: boolean; // false: before the data window (drawn empty)
}

/** The browser's IANA zone, so days end at the operator's midnight. */
export function browserTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
  } catch {
    return undefined;
  }
}

export function dayValue(day: UsageDay, metric: HeatMetric): number {
  return metric === "local" ? day.local_generated_tokens : day.generated_tokens;
}

/**
 * Colour level per value by quartiles of the days that had any usage — every
 * level holds about as many days, so one huge day does not grey out the rest.
 * 0 = nothing, 4 = the busiest quarter (always including the maximum).
 */
export function heatLevels(values: number[]): (v: number) => HeatLevel {
  const used = values.filter((v) => v > 0).sort((a, b) => a - b);
  if (used.length === 0) return () => 0;
  const q = (p: number) => used[Math.floor((used.length - 1) * p)];
  const [q1, q2, q3, max] = [q(0.25), q(0.5), q(0.75), used[used.length - 1]];
  return (v) => {
    if (v <= 0) return 0;
    if (v >= max) return 4;
    if (v <= q1) return 1;
    if (v <= q2) return 2;
    if (v <= q3) return 3;
    return 4;
  };
}

const DAY_MS = 86_400_000;
const utc = (iso: string) => Date.parse(`${iso}T00:00:00Z`);
const iso = (ms: number) => new Date(ms).toISOString().slice(0, 10);
/** 0 = Monday … 6 = Sunday. */
const weekday = (ms: number) => (new Date(ms).getUTCDay() + 6) % 7;

/**
 * `weeks` Monday-first columns of 7 cells, the last column holding today (the
 * last day in `days`). Days after today are null; days before the window are
 * empty cells. `months` names the column that holds the 1st of a month that
 * has begun (month 0 = January).
 */
export function heatmapGrid(days: UsageDay[], weeks: number, metric: HeatMetric) {
  const byDate = new Map(days.map((d) => [d.date, d]));
  const today = days.length ? utc(days[days.length - 1].date) : Date.now();
  const firstMonday = today - weekday(today) * DAY_MS - (weeks - 1) * 7 * DAY_MS;
  const level = heatLevels(days.map((d) => dayValue(d, metric)));

  const columns: (HeatCell | null)[][] = [];
  const months: { col: number; month: number }[] = [];
  for (let col = 0; col < weeks; col++) {
    const cells: (HeatCell | null)[] = [];
    for (let row = 0; row < 7; row++) {
      const ms = firstMonday + (col * 7 + row) * DAY_MS;
      const date = iso(ms);
      if (ms > today) {
        cells.push(null);
        continue;
      }
      if (new Date(ms).getUTCDate() === 1) months.push({ col, month: new Date(ms).getUTCMonth() });
      const day = byDate.get(date);
      const value = day ? dayValue(day, metric) : 0;
      cells.push({ date, value, level: day ? level(value) : 0, inWindow: !!day });
    }
    columns.push(cells);
  }
  return { columns, months };
}

/** The next/previous day inside the window (stops at both ends). */
export function shiftDay(days: UsageDay[], date: string, step: 1 | -1): string {
  const i = days.findIndex((d) => d.date === date);
  if (i < 0) return days[days.length - 1]?.date ?? date;
  return days[Math.min(days.length - 1, Math.max(0, i + step))].date;
}

/**
 * This week so far (Monday up to today) against the same weekdays of last
 * week — a running week is never compared with a whole one.
 */
export function weekSoFar(days: UsageDay[]) {
  const empty = { cost: 0, previousCost: 0, change: null as number | null };
  if (days.length === 0) return empty;
  const today = utc(days[days.length - 1].date);
  const monday = today - weekday(today) * DAY_MS;
  let cost = 0;
  let previousCost = 0;
  for (const d of days) {
    const ms = utc(d.date);
    if (ms >= monday && ms <= today) cost += d.cost_usd;
    else if (ms >= monday - 7 * DAY_MS && ms <= today - 7 * DAY_MS) previousCost += d.cost_usd;
  }
  return { cost, previousCost, change: previousCost > 0 ? cost / previousCost - 1 : null };
}

/** Finished weeks only: a trend line must not dip at the running week. */
export function fullWeeks(weeks: UsageWeek[]): UsageWeek[] {
  return weeks.filter((w) => !w.partial);
}

export interface SourceRow {
  source: string;
  cost_usd: number;
  share: number;
}

/** Top `n` sources of a week by cost with their share; the rest folded into one row. */
export function sourceSplit(week: UsageWeek | undefined, n = 4) {
  const sources = (week?.sources ?? []).filter((s) => s.cost_usd > 0).sort((a, b) => b.cost_usd - a.cost_usd);
  const total = sources.reduce((sum, s) => sum + s.cost_usd, 0);
  if (total <= 0) return { rows: [] as SourceRow[], rest: null };
  const rows = sources.slice(0, n).map((s) => ({ source: s.source, cost_usd: s.cost_usd, share: s.cost_usd / total }));
  const others = sources.slice(n);
  const restCost = others.reduce((sum, s) => sum + s.cost_usd, 0);
  return {
    rows,
    rest: others.length ? { count: others.length, cost_usd: restCost, share: restCost / total } : null,
  };
}

/** Tokens short: 1.2M / 340k / 900 in the operator's locale. */
export function formatTokens(value: number, locale: string): string {
  return new Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}
