"use client";

/**
 * Insights → "Where usage comes from": this week's list-price equivalent by
 * source (backend buckets from usage_baseline.py) as one stacked bar plus a
 * list. Achromatic: the biggest source is the brightest step.
 *
 *   ████████████████▓▓▓▓▓▓▒▒▒░░
 *   ■ You, interactive      $777   58 %
 *   ■ Lead agent            $281   21 %
 *   …
 */

import { useLocale, useTranslations } from "next-intl";
import { C, alpha } from "@/lib/colors";
import { formatShare, formatUsd, sourceLabel, type UsageWeek } from "@/lib/usage";
import { sourceSplit } from "@/lib/insights";

const STEPS = [C.accent, alpha(C.accent, 0.66), alpha(C.accent, 0.42), alpha(C.accent, 0.22), C.borderActive];

export function SourceSplit({ week }: { week: UsageWeek | undefined }) {
  const t = useTranslations("insights.sources");
  const ts = useTranslations("insights.source");
  const locale = useLocale();
  const { rows, rest } = sourceSplit(week, 4);
  const all = [
    ...rows.map((r) => {
      const l = sourceLabel(r.source);
      return { key: r.source, label: ts(l.key, { harness: l.harness ?? "" }), cost: r.cost_usd, share: r.share };
    }),
    ...(rest ? [{ key: "rest", label: t("rest", { count: rest.count }), cost: rest.cost_usd, share: rest.share }] : []),
  ];

  return (
    <section data-region="sources" aria-labelledby="sources-heading" className="min-w-0">
      <div className="flex items-baseline justify-between gap-3">
        <h2 id="sources-heading" className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {t("title")}
        </h2>
        <span className="text-sm shrink-0" style={{ color: C.textMuted }}>{t("period")}</span>
      </div>

      {all.length === 0 ? (
        <p className="mt-3 text-sm" style={{ color: C.textSecondary }}>{t("empty")}</p>
      ) : (
        <>
          <div className="mt-4 flex h-3 gap-1 rounded-full overflow-hidden" aria-hidden data-testid="sources-bar">
            {all.map((s, i) => (
              <span key={s.key} className="block h-full" style={{ flexGrow: s.share, flexBasis: 0, background: STEPS[i] }} />
            ))}
          </div>
          <ul className="mt-3">
            {all.map((s, i) => (
              <li
                key={s.key}
                className="grid grid-cols-[0.75rem_1fr_auto_3.5rem] items-center gap-3 min-h-11"
                style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
                data-testid={`source-${s.key}`}
              >
                <span aria-hidden className="size-3 rounded-dense" style={{ background: STEPS[i] }} />
                <span className="text-sm truncate" style={{ color: C.textSecondary }}>{s.label}</span>
                <span className="font-mono text-sm tabular-nums text-right" style={{ color: C.textPrimary }}>
                  {formatUsd(s.cost, locale)}
                </span>
                <span className="font-mono text-xs tabular-nums text-right" style={{ color: C.textMuted }}>
                  {formatShare(s.share, locale)}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
