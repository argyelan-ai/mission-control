"use client";

/**
 * Insights → the one switch in the title row: All · Cloud · Local. It moves
 * the cost at the top (hero) and the colour of the heatmap together, so the
 * number and the picture always answer the same question.
 */

import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { METRICS, type HeatMetric } from "@/lib/insights";

export function MetricSwitch({ value, onChange }: { value: HeatMetric; onChange: (m: HeatMetric) => void }) {
  const t = useTranslations("insights.metric");
  return (
    <div
      role="radiogroup"
      aria-label={t("aria")}
      className="inline-flex shrink-0 rounded-full p-1"
      style={{ border: `1px solid ${C.border}` }}
    >
      {METRICS.map((m) => (
        <button
          key={m}
          type="button"
          role="radio"
          aria-checked={value === m}
          onClick={() => onChange(m)}
          className="min-h-11 px-3 sm:px-4 rounded-full text-sm cursor-pointer transition-colors"
          style={{
            background: value === m ? C.bgElevated : "transparent",
            color: value === m ? C.textPrimary : C.textSecondary,
          }}
        >
          {t(m)}
        </button>
      ))}
    </div>
  );
}
