// Made-up numbers for tests and screenshots — never real usage.
import type { DailyMetrics, PageViews, UsageByWeek, UsageSource, UsageTotals, UsageWeek } from "../usage";
import type { UsageByDay, UsageDay } from "../insights";

export function mkTotals(cost: number, tokens: number, localOutputShare: number | null = 0.1): UsageTotals {
  const out = Math.round(tokens / 50);
  return {
    events: tokens > 0 || cost > 0 ? 10 : 0,
    input_tokens: Math.round(tokens / 20),
    output_tokens: out,
    cache_read_tokens: tokens - Math.round(tokens / 20) - out,
    cache_write_tokens: 0,
    total_tokens: tokens,
    cost_usd: cost,
    unpriced_events: 0,
    local_tokens: 0,
    local_output_tokens: localOutputShare === null ? 0 : Math.round(out * localOutputShare),
    local_share: tokens ? 0.05 : null,
    local_output_share: tokens ? localOutputShare : null,
  };
}

export function mkSource(source: string, cost: number, tokens: number): UsageSource {
  return { source, ...mkTotals(cost, tokens) };
}

export function mkUsageWeek(over: Partial<UsageWeek> = {}): UsageWeek {
  return { week: "2026-W40", week_start: "2026-09-28", partial: true, totals: mkTotals(120, 400_000), sources: [], ...over };
}

export function mkUsageByWeek(): UsageByWeek {
  const costs = [310, 42, 265, 480, 355, 120];
  const shares = [0.02, 0, 0.04, 0.12, 0.15, 0.2];
  return {
    generated_at: "2026-10-01T09:00:00Z",
    start: "2026-08-24T00:00:00Z",
    weeks: costs.map((cost, i) => ({
      week: `2026-W${35 + i}`,
      week_start: "2026-08-24",
      partial: i === costs.length - 1,
      totals: mkTotals(cost, cost * 1_000_000, shares[i]),
      sources:
        i === costs.length - 1
          ? [mkSource("operator", 84, 9e7), mkSource("heads:local", 0.4, 6e6), mkSource("lead", 21, 2e7), mkSource("agents:cli-bridge", 9, 4e6)]
          : [],
    })),
  };
}

export function mkPageViews(): PageViews {
  return {
    generated_at: "2026-10-01T09:00:00Z",
    weeks: [{ week: "2026-W40", week_start: "2026-09-28", total: 57, dropped: 0, routes: { "/tasks": 19, "/": 16, "/inbox": 9, "/insights": 6, "/runtimes": 4, "/settings": 3 } }],
  };
}

export function mkDaily(over: Partial<DailyMetrics> = {}): DailyMetrics {
  return {
    stale_cards: [{ id: "c1", title: "Card", status: "review", hours_still: 6.5 }, { id: "c2", title: "Card 2", status: "in_progress", hours_still: 5 }],
    reviews_to_lead_24h: 2,
    reviews_total_24h: 5,
    double_dispatch_24h: 0,
    healer_repeats_24h: 1,
    hand_status_changes_24h: 4,
    hand_status_changes_by_reason: {},
    usage_week: { week: "2026-W40", total_tokens: 1e8, output_tokens: 1e6, local_share: 0.05, local_output_share: 0.2, cost_usd: 120 },
    computed_at: "2026-10-01T09:00:00Z",
    ...over,
  };
}

export function mkDay(date: string, generated: number, localGenerated: number, cost = 0): UsageDay {
  const totals = mkTotals(cost, generated * 30, generated ? localGenerated / generated : null);
  return {
    date,
    ...totals,
    generated_tokens: generated,
    local_generated_tokens: localGenerated,
    top_source: generated ? "operator" : null,
  };
}

/** 40 made-up days ending Wednesday 2026-09-30, quieter on weekends. */
export function mkUsageByDay(): UsageByDay {
  const end = Date.UTC(2026, 8, 30);
  const days: UsageDay[] = [];
  for (let i = 39; i >= 0; i--) {
    const d = new Date(end - i * 86_400_000);
    const weekend = d.getUTCDay() === 0 || d.getUTCDay() === 6;
    const generated = ((i * 7919) % 97) * (weekend ? 20_000 : 90_000);
    days.push(mkDay(d.toISOString().slice(0, 10), generated, Math.round(generated * 0.12), generated / 4000));
  }
  return { generated_at: "2026-09-30T09:00:00Z", start: days[0].date, tz: "Europe/Zurich", days };
}
