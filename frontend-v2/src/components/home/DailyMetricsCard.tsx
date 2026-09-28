"use client";

/**
 * Home → "Measured today" (ROADMAP E0). The daily metrics digest (M1–M5)
 * used to reach the operator only through Slack / Telegram; MC is the main
 * channel, so the same numbers — computed live by the same code — sit here.
 *
 *   ◔ METRICS · 24 H                                                      ›
 *   3              2/5               0/1              4          $1,339
 *   cards idle     reviews at the    double dispatch  manual     week 39 · list price
 *   over 4 h       lead              / healer         changes    4.9 % local
 *
 * One status colour only: idle cards turn warning when there are any.
 * Renders nothing while the numbers are loading or unavailable.
 */

import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { Activity, ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import { formatShare, formatUsd, usageWeekOf, weekNumber, type DailyMetrics } from "@/lib/usage";

export const DAILY_METRICS_KEY = ["system", "dailyMetrics"] as const;

export function DailyMetricsCard() {
  const q = useQuery<DailyMetrics>({
    queryKey: DAILY_METRICS_KEY,
    queryFn: () => api.system.dailyMetrics(),
    retry: false,
    refetchInterval: 5 * 60_000,
  });
  if (!q.data) return null;
  return <DailyMetricsCardView data={q.data} />;
}

interface Stat {
  key: string;
  value: string;
  label: string;
  sub?: string | null;
  tone?: string;
}

export function DailyMetricsCardView({ data }: { data: DailyMetrics }) {
  const t = useTranslations("home.metrics");
  const locale = useLocale();
  const usage = usageWeekOf(data);
  const stale = data.stale_cards.length;

  const stats: Stat[] = [
    { key: "stale", value: String(stale), label: t("stale"), tone: stale > 0 ? STATUS_TEXT.warning : undefined },
    { key: "reviews", value: `${data.reviews_to_lead_24h}/${data.reviews_total_24h}`, label: t("reviews") },
    { key: "dispatch", value: `${data.double_dispatch_24h}/${data.healer_repeats_24h}`, label: t("dispatch") },
    { key: "hand", value: String(data.hand_status_changes_24h), label: t("hand") },
  ];
  if (usage) {
    const share = formatShare(usage.local_output_share, locale);
    stats.push({
      key: "week",
      value: formatUsd(usage.cost_usd, locale),
      label: t("week", { week: weekNumber(usage.week) }),
      sub: share ? t("local", { share }) : null,
    });
  }

  return (
    <section
      className="rounded-xl corner-ticks p-3 sm:p-4"
      style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
      aria-labelledby="daily-metrics-heading"
      data-testid="daily-metrics-card"
    >
      <div className="flex items-center gap-2">
        <Activity size={13} aria-hidden style={{ color: C.textMuted }} className="shrink-0" />
        <h2 id="daily-metrics-heading" className="label-sys flex-1 min-w-0 truncate">
          {t("title")}
        </h2>
        <Link
          href="/insights"
          aria-label={t("more")}
          title={t("more")}
          className="shrink-0 -my-2 -mr-2 inline-flex items-center justify-center min-w-[44px] min-h-[44px] rounded-md transition-colors hover:bg-[var(--color-bg-hover)]"
          style={{ color: C.textMuted }}
        >
          <ChevronRight size={16} aria-hidden />
        </Link>
      </div>

      <dl className="mt-2 grid grid-cols-2 sm:grid-cols-5 gap-x-4 gap-y-3">
        {stats.map((s) => (
          <div
            key={s.key}
            data-testid={`daily-metric-${s.key}`}
            // the week value closes the grid: full width on the phone instead of an orphan cell
            className={`min-w-0 flex flex-col-reverse${s.key === "week" ? " col-span-2 sm:col-span-1" : ""}`}
          >
            <dt className="text-xs leading-snug" style={{ color: C.textMuted }}>
              {s.label}
              {s.sub && <span className="block">{s.sub}</span>}
            </dt>
            <dd className="font-mono tabular-nums text-base" style={{ color: s.tone ?? C.textPrimary }}>
              {s.value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
