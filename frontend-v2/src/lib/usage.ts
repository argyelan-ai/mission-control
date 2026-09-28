/**
 * E0 measuring layer in MC (ROADMAP E0): the daily metrics digest (M1–M5),
 * tokens and list-price cost per ISO week × source, and page views per route.
 * Backend: /api/v1/system/daily-metrics, /api/v1/intelligence/costs/by-week,
 * /api/v1/usage/pages. Pure helpers here, so the cards stay thin.
 */

export interface UsageTotals {
  events: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
  cost_usd: number;
  unpriced_events: number;
  local_tokens: number;
  local_output_tokens: number;
  local_share: number | null;
  local_output_share: number | null;
}

export interface UsageSource extends UsageTotals {
  source: string;
}

export interface UsageWeek {
  week: string; // "2026-W39"
  week_start: string; // "2026-09-21"
  partial: boolean;
  totals: UsageTotals;
  sources: UsageSource[];
}

export interface UsageByWeek {
  generated_at: string;
  start: string;
  weeks: UsageWeek[];
}

export interface PageWeek {
  week: string;
  week_start: string;
  total: number;
  dropped: number;
  routes: Record<string, number>;
}

export interface PageViews {
  generated_at: string;
  weeks: PageWeek[];
}

export interface DailyUsageWeek {
  week: string;
  total_tokens: number;
  output_tokens: number;
  local_share: number | null;
  local_output_share: number | null;
  cost_usd: number;
}

export interface DailyMetrics {
  stale_cards: { id: string; title: string; status: string; hours_still: number }[];
  reviews_to_lead_24h: number;
  reviews_total_24h: number;
  double_dispatch_24h: number;
  healer_repeats_24h: number;
  hand_status_changes_24h: number;
  hand_status_changes_by_reason: Record<string, number>;
  usage_week: DailyUsageWeek | { error: true } | null;
  computed_at: string;
}

/** "2026-W39" → 39. */
export function weekNumber(week: string): number {
  return Number(week.split("-W")[1]);
}

/** The usage-week part of the digest, or null when there is none (no rows yet, or the query failed). */
export function usageWeekOf(m: DailyMetrics | undefined): DailyUsageWeek | null {
  const u = m?.usage_week;
  return u && !("error" in u) ? u : null;
}

/**
 * i18n key + value for a source bucket (backend: usage_baseline.py).
 * `agents:<harness>` keeps the harness as a machine value.
 */
export function sourceLabel(source: string): { key: string; harness?: string } {
  if (source.startsWith("agents:")) return { key: "agents", harness: source.slice("agents:".length) };
  if (source === "heads:local") return { key: "headsLocal" };
  if (source === "heads:cloud") return { key: "headsCloud" };
  if (source.startsWith("heads:")) return { key: "headsUnknown" };
  if (source === "operator" || source === "lead") return { key: source };
  return { key: "unattributed" };
}

/** Top `n` sources of a week by cost, then tokens; sources without cost or tokens are left out. */
export function topSources(week: UsageWeek | undefined, n = 3): UsageSource[] {
  return (week?.sources ?? [])
    .filter((s) => s.cost_usd > 0 || s.total_tokens > 0)
    .slice()
    .sort((a, b) => b.cost_usd - a.cost_usd || b.total_tokens - a.total_tokens)
    .slice(0, n);
}

/** Top `n` routes of a week by views. */
export function topRoutes(week: PageWeek | undefined, n = 5): [string, number][] {
  return Object.entries(week?.routes ?? {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, n);
}

/** Money as the list-price equivalent: whole dollars from 100 up, cents below. */
export function formatUsd(value: number, locale: string): string {
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: value >= 100 ? 0 : 2,
    minimumFractionDigits: value >= 100 ? 0 : 2,
  }).format(value);
}

/** Share 0..1 as a percent with one decimal ("4.9 %" / "4,9 %"); null → null (K3: leave it out). */
export function formatShare(share: number | null | undefined, locale: string): string | null {
  if (share === null || share === undefined) return null;
  return new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 1, minimumFractionDigits: share > 0 && share < 0.1 ? 1 : 0 }).format(share);
}
