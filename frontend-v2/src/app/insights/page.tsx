"use client";

/**
 * Insights — one page that answers five questions in this order:
 *   1. What did my AI work cost this week, and how much ran locally?  (hero)
 *   2. When was there work?                                            (heatmap)
 *   3. Where does the usage come from?                                 (sources)
 *   4. Is the local share going the right way?                         (trend)
 *      How much ran on the local boxes, and how much of it bypassed MC?  (box)
 *      What did the live voice cost, today and this week?               (voice)
 *   5. What got done?                                                  (done)
 * The switch All · Cloud · Local in the title row moves the hero's cost, the
 * heatmap with its day row and the source split together. Everything the old tabs showed lives folded away in
 * "Details".
 * Days and weeks are counted in the browser's time zone.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import AppShell from "@/components/layout/AppShell";
import { PAGE_WIDE } from "@/components/layout/AppShellWidth";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { browserTimeZone, type HeatMetric } from "@/lib/insights";
import { MetricSwitch } from "@/components/insights/MetricSwitch";
import { InsightsHero } from "@/components/insights/InsightsHero";
import { UsageHeatmap } from "@/components/insights/UsageHeatmap";
import { SourceSplit } from "@/components/insights/SourceSplit";
import { LocalShareTrend } from "@/components/insights/LocalShareTrend";
import { BoxTokens } from "@/components/insights/BoxTokens";
import { VoiceUsage } from "@/components/insights/VoiceUsage";
import { DoneSummary } from "@/components/insights/DoneSummary";
import { InsightsDetails } from "@/components/insights/InsightsDetails";

const DAYS = 371; // 53 weeks: the desktop grid shows 52 plus the running one
const WEEKS = 12;

export default function InsightsPage() {
  const t = useTranslations("insights");
  const tz = browserTimeZone();
  const [metric, setMetric] = useState<HeatMetric>("all");

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
      <div className={PAGE_WIDE}>
        <div className="flex items-center justify-between gap-3">
          <h1 className="display text-2xl font-semibold" style={{ color: C.textPrimary }}>
            {t("title")}
          </h1>
          {/* Rendered at once, not after the data: the switch is part of the
              title row, and appearing late moved the row (CLS 0.51). */}
          {!failed && <MetricSwitch value={metric} onChange={setMetric} />}
        </div>

        {failed ? (
          <p className="mt-6 text-sm" style={{ color: C.textSecondary }}>{t("loadFailed")}</p>
        ) : !ready ? (
          <InsightsSkeleton />
        ) : (
          <>
            <InsightsHero days={byDay.data} weeks={byWeek.data} metric={metric} />
            <div className="mt-12">
              <UsageHeatmap data={byDay.data} metric={metric} />
            </div>
            <div className="mt-12 grid grid-cols-1 lg:grid-cols-2 gap-12">
              <SourceSplit week={weeks[weeks.length - 1]} metric={metric} />
              <LocalShareTrend weeks={weeks} />
              <BoxTokens week={weeks[weeks.length - 1]} since={byWeek.data.box_tracking_since} />
              <VoiceUsage />
            </div>
          </>
        )}

        <div className="mt-12 grid grid-cols-1 lg:grid-cols-2 gap-12 lg:items-start">
          <DoneSummary
            insights={insights.data}
            windowDays={config.data?.analysis_window_days ?? 7}
            heads={heads.data?.runs ?? null}
          />
          <InsightsDetails />
        </div>
      </div>
    </AppShell>
  );
}

/**
 * Placeholder at the final height of hero, heatmap and the four cards, so
 * "What got done" and the details below stay where they land once the data
 * is in (no jump on the phone). Heights measured at 390, 1024 and 1440 px.
 */
function InsightsSkeleton() {
  const block = "rounded-lg motion-safe:animate-pulse";
  const fill = { background: C.bgSurface };
  return (
    <div aria-busy="true" data-testid="insights-skeleton">
      <div className={`mt-6 h-52 md:h-24 ${block}`} style={fill} />
      <div className={`mt-12 h-76 md:h-72 lg:h-80 ${block}`} style={fill} />
      <div className="mt-12 grid grid-cols-1 lg:grid-cols-2 gap-12">
        <div className={`h-56 lg:h-52 ${block}`} style={fill} />
        <div className={`h-44 lg:h-52 ${block}`} style={fill} />
        <div className={`h-72 lg:h-64 ${block}`} style={fill} />
        <div className={`h-24 lg:h-64 ${block}`} style={fill} />
      </div>
    </div>
  );
}
