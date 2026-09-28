"use client";

/**
 * Insights → "Local share per week": one bar per ISO week, the running week
 * in the accent. The direction the operator wants (ADR-086, local first) is
 * up; the bars share one scale from 0 to the best week so small shares stay
 * visible.
 *
 *   ▁ ▁ ▁ ▂ ▂ ▁ ▃ █ ▇ ▅ ▅ ▆
 *       W31       W34       W37      W40
 */

import { useLocale, useTranslations } from "next-intl";
import { C, alpha } from "@/lib/colors";
import { formatShare, weekNumber, type UsageWeek } from "@/lib/usage";

export function LocalShareTrend({ weeks }: { weeks: UsageWeek[] }) {
  const t = useTranslations("insights.trend");
  const locale = useLocale();
  const shares = weeks.map((w) => w.totals.local_output_share ?? 0);
  const max = Math.max(0.05, ...shares);
  const current = weeks[weeks.length - 1];

  return (
    <section data-region="trend" aria-labelledby="trend-heading" className="min-w-0">
      <div className="flex items-baseline justify-between gap-3">
        <h2 id="trend-heading" className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {t("title")}
        </h2>
        <span className="text-xs shrink-0" style={{ color: C.textMuted }}>{t("period", { count: weeks.length })}</span>
      </div>

      <ol
        className="mt-4 flex items-end gap-1 h-24"
        style={{ borderBottom: `1px solid ${C.border}` }}
        aria-label={t("aria")}
        data-testid="trend-bars"
      >
        {weeks.map((w, i) => {
          const share = shares[i];
          const isCurrent = w === current;
          return (
            <li
              key={w.week}
              className="flex-1 h-full flex flex-col justify-end"
              aria-label={`${t("week", { week: weekNumber(w.week) })}: ${formatShare(w.totals.local_output_share, locale) ?? "—"}`}
            >
              <span
                className="block rounded-t-sm"
                style={{
                  height: `${Math.max(2, (share / max) * 100)}%`,
                  background: isCurrent ? C.accent : alpha(C.accent, 0.42),
                }}
              />
            </li>
          );
        })}
      </ol>
      <div className="mt-1 flex gap-1 text-xs tabular-nums" style={{ color: C.textMuted }} aria-hidden>
        {weeks.map((w, i) => (
          <span key={w.week} className="flex-1 text-center">
            {(weeks.length - 1 - i) % 3 === 0 ? weekNumber(w.week) : ""}
          </span>
        ))}
      </div>
      <p className="mt-2 text-xs" style={{ color: C.textMuted }}>{t("hint")}</p>
    </section>
  );
}
