"use client";

/**
 * Insights → hero: the one answer at the top of the page — what this week's
 * AI work cost so far (list-price equivalent) and how much of it ran locally
 * (ADR-086: local first). Cost is compared with the same weekdays of last
 * week; the line shows the finished weeks only.
 *
 *   $1,339                                  ╱╲_╱‾╲__●
 *   This week so far · list-price equivalent — flat-rate plans …
 *   28 % less than the same days last week
 *
 *   12 %  ran locally
 *   ▓▓▓░░░░░░░░░░░░░░░░░░░
 *   Share of output tokens · last week 5 %
 */

import { useLocale, useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { formatShare, formatUsd, type UsageByWeek } from "@/lib/usage";
import { fullWeeks, weekSoFar, type UsageByDay } from "@/lib/insights";

export function InsightsHero({ days, weeks }: { days: UsageByDay; weeks: UsageByWeek }) {
  const t = useTranslations("insights.hero");
  const locale = useLocale();
  const now = weekSoFar(days.days);
  const current = weeks.weeks[weeks.weeks.length - 1];
  const previous = weeks.weeks[weeks.weeks.length - 2];
  const share = current?.totals.local_output_share ?? null;
  const previousShare = formatShare(previous?.totals.local_output_share, locale);
  const hasUsage = days.days.some((d) => d.events > 0) || weeks.weeks.some((w) => w.totals.events > 0);

  if (!hasUsage) {
    return (
      <section data-region="hero" className="mt-6">
        <p className="text-sm" style={{ color: C.textSecondary }}>{t("empty")}</p>
      </section>
    );
  }

  const change = now.change;
  let changeText: string | null = null;
  if (change !== null) {
    const pct = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 }).format(Math.abs(change));
    changeText = Math.abs(change) < 0.005 ? t("same") : change < 0 ? t("down", { pct }) : t("up", { pct });
  }

  return (
    <section
      data-region="hero"
      className="mt-6 grid grid-cols-1 lg:grid-cols-2 gap-8 lg:gap-16 lg:items-end"
    >
      <div>
        <div className="flex items-end justify-between gap-4">
          <p
            className="display text-2xl font-medium tabular-nums"
            style={{ color: C.textPrimary }}
            data-testid="hero-cost"
          >
            {formatUsd(now.cost, locale)}
          </p>
          <Sparkline values={fullWeeks(weeks.weeks).map((w) => w.totals.cost_usd)} label={t("trendAria", { count: fullWeeks(weeks.weeks).length })} />
        </div>
        <p className="mt-2 text-xs" style={{ color: C.textMuted }}>{t("costHint")}</p>
        {changeText && (
          <p className="mt-1 text-sm" style={{ color: C.textSecondary }} data-testid="hero-change">
            {changeText}
          </p>
        )}
      </div>

      {share !== null && (
        <div data-testid="hero-local">
          <p className="flex items-baseline gap-2">
            <span className="display text-xl font-medium tabular-nums" style={{ color: C.textPrimary }}>
              {formatShare(share, locale)}
            </span>
            <span className="text-sm" style={{ color: C.textSecondary }}>{t("local")}</span>
          </p>
          <div
            className="mt-2 h-2 rounded-full overflow-hidden"
            style={{ background: C.borderActive }}
            role="meter"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(share * 100)}
            aria-label={t("local")}
          >
            <div className="h-full rounded-full" style={{ width: `${Math.min(100, share * 100)}%`, background: C.accent }} />
          </div>
          <p className="mt-2 text-xs" style={{ color: C.textMuted }}>
            {previousShare ? t("localHint", { share: previousShare }) : t("localHintFirst")}
          </p>
        </div>
      )}
    </section>
  );
}

/** A quiet line over the finished weeks, the last one marked. Plain SVG. */
function Sparkline({ values, label }: { values: number[]; label: string }) {
  if (values.length < 2) return null;
  const w = 120;
  const h = 40;
  const max = Math.max(...values) || 1;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * (w - 4) + 2, h - 3 - (v / max) * (h - 6)] as const);
  const [lx, ly] = pts[pts.length - 1];
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      className="w-32 h-10 shrink-0"
      role="img"
      aria-label={label}
      data-testid="hero-sparkline"
    >
      <polyline
        fill="none"
        stroke={C.textMuted}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
        points={pts.map(([x, y]) => `${x},${y}`).join(" ")}
      />
      <circle cx={lx} cy={ly} r={2.5} fill={C.accent} />
    </svg>
  );
}
