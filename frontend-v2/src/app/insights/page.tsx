"use client";

/**
 * Insights — one page that answers five questions in this order:
 *   1. What did my AI work cost this week, and how much ran locally?  (hero)
 *   2. When was there work?                                            (heatmap)
 *   3. Where does the usage come from?                                 (sources)
 *   4. Is the local share going the right way?                         (trend)
 *   5. What got done?                                                  (done)
 * Everything the old tabs showed lives folded away in "Details".
 * Days and weeks are counted in the browser's time zone.
 */

import { useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import AppShell from "@/components/layout/AppShell";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { browserTimeZone } from "@/lib/insights";
import { InsightsHero } from "@/components/insights/InsightsHero";
import { UsageHeatmap } from "@/components/insights/UsageHeatmap";
import { SourceSplit } from "@/components/insights/SourceSplit";
import { LocalShareTrend } from "@/components/insights/LocalShareTrend";
import { DoneSummary } from "@/components/insights/DoneSummary";
import { InsightsDetails } from "@/components/insights/InsightsDetails";

const DAYS = 371; // 53 weeks: the desktop grid shows 52 plus the running one
const WEEKS = 12;

export default function InsightsPage() {
  const t = useTranslations("insights");
  const tz = browserTimeZone();

  const byDay = useQuery({
    queryKey: ["intelligence", "byDay", DAYS, tz],
    queryFn: () => api.intelligence.byDay(DAYS, tz),
    refetchInterval: 5 * 60_000,
  });
  const byWeek = useQuery({
    queryKey: ["intelligence", "byWeek", WEEKS, tz],
    queryFn: () => api.intelligence.byWeek(WEEKS, tz),
    refetchInterval: 5 * 60_000,
  });
  const insights = useQuery({
    queryKey: ["intelligence-insights"],
    queryFn: () => api.intelligence.insights(),
    refetchInterval: 60_000,
  });
  // Analysis window is a backend config (analysis_window_days).
  const config = useQuery({
    queryKey: ["intelligence-config"],
    queryFn: () => api.intelligence.config(),
    staleTime: 5 * 60_000,
  });
  // 404 `heads_disabled` while the launcher is off → no heads row.
  const heads = useQuery({
    queryKey: ["heads", "list", "insights"],
    queryFn: () => api.heads.list(),
    retry: false,
    refetchInterval: 60_000,
  });

  const failed = byDay.isError || byWeek.isError;
  const ready = byDay.data && byWeek.data;
  const weeks = byWeek.data?.weeks ?? [];

  return (
    <AppShell>
      <div className="max-w-6xl mx-auto">
        <h1 className="display text-2xl font-semibold" style={{ color: C.textPrimary }}>
          {t("title")}
        </h1>

        {failed ? (
          <p className="mt-6 text-sm" style={{ color: C.textSecondary }}>{t("loadFailed")}</p>
        ) : !ready ? (
          <div className="mt-6 h-32" aria-busy="true" />
        ) : (
          <>
            <InsightsHero days={byDay.data} weeks={byWeek.data} />
            <div className="mt-12 grid grid-cols-1 lg:grid-cols-[3fr_2fr] gap-12">
              <UsageHeatmap data={byDay.data} />
              <SourceSplit week={weeks[weeks.length - 1]} />
            </div>
          </>
        )}

        <div className="mt-12 grid grid-cols-1 lg:grid-cols-2 gap-12">
          {ready && <LocalShareTrend weeks={weeks} />}
          <DoneSummary
            insights={insights.data}
            windowDays={config.data?.analysis_window_days ?? 7}
            heads={heads.data?.runs ?? null}
          />
        </div>

        <div className="mt-12">
          <InsightsDetails />
        </div>
      </div>
    </AppShell>
  );
}
