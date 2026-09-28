"use client";

/**
 * Insights → "Usage by week" (ROADMAP E0 baseline, in MC). Last six ISO
 * weeks: list-price equivalent and local share of output tokens, then this
 * week's top sources and the most visited pages (route beacon).
 *
 *   USAGE BY WEEK
 *   List-price equivalent · local share of output tokens
 *   Week 40 · so far  ▇▇             $68      21 %
 *   Week 39           ▇▇▇▇▇▇▇        $1,339   4.9 %
 *   …
 *   THIS WEEK BY SOURCE              PAGE VIEWS THIS WEEK
 *   You, interactive       $1,281    /sessions        42
 *   Lead agent               $44     /                18
 *
 * Mono only for the numbers (K7); the words sit once in the hint and headings.
 * The bar is plain CSS (share of the busiest week), no chart library needed.
 */

import { useLocale, useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import {
  formatShare,
  formatUsd,
  sourceLabel,
  topRoutes,
  topSources,
  weekNumber,
  type PageViews,
  type UsageByWeek as UsageByWeekData,
} from "@/lib/usage";

export const USAGE_BY_WEEK_KEY = ["intelligence", "byWeek", 6] as const;
export const PAGE_VIEWS_KEY = ["usage", "pages", 1] as const;

export function UsageByWeek() {
  const weeks = useQuery<UsageByWeekData>({
    queryKey: USAGE_BY_WEEK_KEY,
    queryFn: () => api.intelligence.byWeek(6),
    refetchInterval: 5 * 60_000,
  });
  const pages = useQuery<PageViews>({
    queryKey: PAGE_VIEWS_KEY,
    queryFn: () => api.usage.pages(1),
    refetchInterval: 5 * 60_000,
  });
  if (!weeks.data) return null;
  return <UsageByWeekView data={weeks.data} pages={pages.data} />;
}

export function UsageByWeekView({ data, pages }: { data: UsageByWeekData; pages?: PageViews }) {
  const t = useTranslations("insights.usage");
  const locale = useLocale();
  const weeks = [...data.weeks].reverse(); // newest first
  const maxCost = Math.max(0, ...weeks.map((w) => w.totals.cost_usd));
  const current = data.weeks[data.weeks.length - 1];
  const sources = topSources(current, 3);
  const routes = topRoutes(pages?.weeks[pages.weeks.length - 1], 5);
  const hasUsage = weeks.some((w) => w.totals.events > 0);

  return (
    <section className="mb-6" aria-labelledby="usage-by-week-heading" data-testid="usage-by-week">
      <h2 id="usage-by-week-heading" className="label-sys">{t("title")}</h2>
      <p className="mt-1 text-xs" style={{ color: C.textMuted }}>{t("hint")}</p>

      {!hasUsage ? (
        <p className="mt-3 text-sm" style={{ color: C.textSecondary }}>{t("empty")}</p>
      ) : (
        <div className="mt-3 grid grid-cols-1 lg:grid-cols-2 gap-x-12 gap-y-6">
          <ul className="divide-y" style={{ borderColor: C.borderSubtle }}>
            {weeks.map((w) => {
              const share = formatShare(w.totals.local_output_share, locale);
              const pct = maxCost > 0 ? (w.totals.cost_usd / maxCost) * 100 : 0;
              return (
                <li
                  key={w.week}
                  data-testid={`usage-week-${w.week}`}
                  className="grid grid-cols-[6.5rem_1fr_auto] sm:grid-cols-[8rem_1fr_5.5rem_6.5rem] items-center gap-x-3 py-2"
                  style={{ borderColor: C.borderSubtle }}
                >
                  <span className="text-sm truncate" style={{ color: w.partial ? C.textPrimary : C.textSecondary }}>
                    {t(w.partial ? "weekNow" : "week", { week: weekNumber(w.week) })}
                  </span>
                  <span aria-hidden className="h-2 rounded-full" style={{ background: C.borderSubtle }}>
                    <span className="block h-full rounded-full" style={{ width: `${pct}%`, background: C.textMuted }} />
                  </span>
                  <span className="font-mono tabular-nums text-sm text-right" style={{ color: C.textPrimary }}>
                    {formatUsd(w.totals.cost_usd, locale)}
                  </span>
                  {share && (
                    <span
                      className="col-start-3 sm:col-start-auto font-mono tabular-nums text-xs text-right"
                      style={{ color: C.textMuted }}
                    >
                      {share}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-1 gap-6 content-start">
            {sources.length > 0 && (
              <div>
                <h3 className="label-sys">{t("sourcesTitle")}</h3>
                <ul className="mt-1">
                  {sources.map((s) => {
                    const l = sourceLabel(s.source);
                    return (
                      <li key={s.source} className="flex items-baseline justify-between gap-3 py-2 text-sm">
                        <span className="truncate" style={{ color: C.textSecondary }}>
                          {t(`source.${l.key}`, { harness: l.harness ?? "" })}
                        </span>
                        <span className="font-mono tabular-nums" style={{ color: C.textPrimary }}>
                          {formatUsd(s.cost_usd, locale)}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}
            <div>
              <h3 className="label-sys">{t("pagesTitle")}</h3>
              {routes.length === 0 ? (
                <p className="mt-1 py-2 text-sm" style={{ color: C.textMuted }}>{t("pagesEmpty")}</p>
              ) : (
                <ul className="mt-1" data-testid="usage-pages">
                  {routes.map(([route, count]) => (
                    <li key={route} className="flex items-baseline justify-between gap-3 py-2 text-sm">
                      <span className="font-mono truncate" style={{ color: C.textSecondary }}>{route}</span>
                      <span className="font-mono tabular-nums" style={{ color: C.textMuted }}>{count}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
