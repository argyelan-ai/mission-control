"use client";

/**
 * Insights → "Voice": what the live voice in the chat (ADR-091) cost — today
 * and over the last 7 days, in call minutes and dollars, as MC booked it from
 * OpenAI's own token counts (voice PR 6). Same list pattern as SourceSplit.
 *
 *   Voice                                   live calls
 *   Today      12 min      $0.84
 *   7 days     47 min      $3.10
 *   Daily budget $5.00 · ≈ $0.07 per call minute (7 days)
 *
 * Hidden when the voice is off and nothing was spent (DESIGN K3: nothing
 * empty) or the numbers cannot be read (not an admin, older backend).
 */

import { useLocale, useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { api, type VoiceUsageBucket } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import { formatUsd } from "@/lib/usage";

function minutes(seconds: number): number {
  return seconds > 0 ? Math.max(1, Math.round(seconds / 60)) : 0;
}

export function VoiceUsage() {
  const t = useTranslations("insights.voice");
  const locale = useLocale();
  const q = useQuery({
    queryKey: ["voice-live", "usage-summary"],
    queryFn: () => api.voiceLive.usageSummary(),
    retry: false,
    refetchInterval: 60_000,
  });
  const data = q.data;
  if (!data || (!data.enabled && data.week.calls === 0)) return null;

  const rows: { key: string; label: string; b: VoiceUsageBucket }[] = [
    { key: "today", label: t("today"), b: data.today },
    { key: "week", label: t("week"), b: data.week },
  ];
  const budget = data.budget_usd > 0 ? formatUsd(data.budget_usd, locale) : null;

  return (
    <section data-region="voice" aria-labelledby="voice-heading" className="min-w-0">
      <div className="flex items-baseline justify-between gap-3">
        <h2 id="voice-heading" className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {t("title")}
        </h2>
        <span className="text-sm shrink-0" style={{ color: C.textMuted }}>{t("period")}</span>
      </div>

      {data.week.calls === 0 ? (
        <p className="mt-3 text-sm" style={{ color: C.textSecondary }} data-testid="voice-usage-empty">
          {t("empty")}
        </p>
      ) : (
        <ul className="mt-3">
          {rows.map((r) => (
            <li
              key={r.key}
              className="grid grid-cols-[1fr_auto_4.5rem] items-center gap-3 min-h-11"
              style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
              data-testid={`voice-usage-${r.key}`}
            >
              <span className="text-sm truncate" style={{ color: C.textSecondary }}>{r.label}</span>
              <span className="font-mono text-sm tabular-nums text-right" style={{ color: C.textMuted }}>
                {t("minutes", { minutes: String(minutes(r.b.seconds)) })}
              </span>
              <span className="font-mono text-sm tabular-nums text-right" style={{ color: C.textPrimary }}>
                {formatUsd(r.b.cost_usd, locale)}
              </span>
            </li>
          ))}
        </ul>
      )}

      {budget && (
        <p
          className="mt-3 text-sm"
          style={{ color: data.budget_reached ? STATUS_TEXT.warning : C.textSecondary }}
          data-testid="voice-usage-budget"
        >
          {data.budget_reached ? t("budget", { budget }) : t("budgetLeft", { budget })}
        </p>
      )}
      {data.per_minute_usd !== null && (
        <p className="mt-2 text-xs" style={{ color: C.textMuted }} data-testid="voice-usage-per-minute">
          {t("perMinute", { cost: formatUsd(data.per_minute_usd, locale) })}
        </p>
      )}
    </section>
  );
}
