"use client";

/**
 * Insights → "Local boxes": what the local inference boxes themselves counted
 * this week (their Prometheus counters, booked hourly by the backend's token
 * ledger), split into the part MC's own transcripts account for and the rest —
 * requests sent straight to a box's API from another machine. Cached prompt
 * tokens are reused, not recomputed: they stand apart, never in the headline.
 * Same bar + list pattern as SourceSplit.
 *
 *   1.2M  fresh tokens on the boxes
 *   ██████████████▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
 *   ■ Through MC                     480K   40 %
 *   ■ Direct API (not through MC)    720K   60 %
 *   + 14.8M cached prompt tokens
 */

import { useLocale, useTranslations } from "next-intl";
import { C, alpha } from "@/lib/colors";
import { formatShare, type UsageWeek } from "@/lib/usage";
import { boxSplit, formatTokens } from "@/lib/insights";

const STEPS = [C.accent, alpha(C.accent, 0.42)];

export function BoxTokens({ week, since }: { week: UsageWeek | undefined; since: string | null | undefined }) {
  const t = useTranslations("insights.box");
  const locale = useLocale();
  const split = since ? boxSplit(week) : null;

  const header = (
    <div className="flex items-baseline justify-between gap-3">
      <h2 id="box-heading" className="text-base font-semibold" style={{ color: C.textPrimary }}>
        {t("title")}
      </h2>
      <span className="text-sm shrink-0" style={{ color: C.textMuted }}>{t("period")}</span>
    </div>
  );

  if (!split || !since) {
    return (
      <section data-region="box" aria-labelledby="box-heading" className="min-w-0">
        {header}
        <p className="mt-3 text-sm" style={{ color: C.textSecondary }}>{t("notYet")}</p>
      </section>
    );
  }

  const rows = [
    { key: "via-mc", label: t("viaMc"), value: split.viaMc, share: split.viaMcShare },
    { key: "external", label: t("external"), value: split.external, share: split.externalShare },
  ];
  const sinceDate = new Intl.DateTimeFormat(locale, { day: "numeric", month: "long" }).format(new Date(since));

  return (
    <section data-region="box" aria-labelledby="box-heading" className="min-w-0">
      {header}

      <p className="mt-3 flex items-baseline gap-2" data-testid="box-total">
        <span className="display text-xl font-medium tabular-nums" style={{ color: C.textPrimary }}>
          {formatTokens(split.total, locale)}
        </span>
        <span className="text-xs" style={{ color: C.textSecondary }}>{t("totalLabel")}</span>
      </p>

      {split.total > 0 && (
        <div className="mt-4 flex h-3 gap-1 rounded-full overflow-hidden" aria-hidden data-testid="box-bar">
          {rows.map((r, i) => (
            <span key={r.key} className="block h-full" style={{ flexGrow: r.share, flexBasis: 0, background: STEPS[i] }} />
          ))}
        </div>
      )}
      <ul className="mt-3">
        {rows.map((r, i) => (
          <li
            key={r.key}
            className="grid grid-cols-[0.75rem_1fr_auto_3.5rem] items-center gap-3 min-h-11"
            style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
            data-testid={`box-${r.key}`}
          >
            <span aria-hidden className="size-3 rounded-dense" style={{ background: STEPS[i] }} />
            <span className="text-sm truncate" style={{ color: C.textSecondary }}>{r.label}</span>
            <span className="font-mono text-sm tabular-nums text-right" style={{ color: C.textPrimary }}>
              {formatTokens(r.value, locale)}
            </span>
            <span className="font-mono text-xs tabular-nums text-right" style={{ color: C.textMuted }}>
              {split.total > 0 ? formatShare(r.share, locale) : ""}
            </span>
          </li>
        ))}
      </ul>

      <p className="mt-3 text-sm" style={{ color: C.textSecondary }} data-testid="box-cached">
        {t("cached", { tokens: formatTokens(split.cached, locale) })}
      </p>
      {split.clamped && (
        <p className="mt-2 text-xs" style={{ color: C.textMuted }} data-testid="box-clamped">
          {t("clamped")}
        </p>
      )}
      <p className="mt-2 text-xs" style={{ color: C.textMuted }} data-testid="box-since">
        {t("since", { date: sinceDate })}
      </p>
    </section>
  );
}
