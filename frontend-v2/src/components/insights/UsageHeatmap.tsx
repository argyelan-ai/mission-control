"use client";

/**
 * Insights → "Tokens per day": one cell per day like the GitHub contribution
 * graph — 26 weeks on the phone, 52 from 1024 px. Colour = generated tokens
 * (input + output, no cache reads) in five brightness steps of the accent by
 * quartiles (DESIGN.md: colour means status, so no green). "All · Local"
 * switches to the tokens that ran locally.
 *
 *   Tokens per day                                  ( All | Local )
 *        Apr      May      Jun      Jul      Aug      Sep
 *   Mon  ▢▣▣▢▣■▣▢▣▣■■▢▣▣▣■■▣▢▣■■▣▣▢
 *   …
 *                                         less ▢▢▣▣■ more
 *   ‹  Wednesday, 23 September                                          ›
 *      1.2M tokens · $214 · 18 % local · Mostly You, interactive
 *
 * A cell is ~12 px — too small to aim at on a phone — so the day row below
 * has 44 px arrows; tapping a cell selects it too. The day text starts on the
 * grid's edge (K9: two left edges).
 */

import { useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { C, alpha } from "@/lib/colors";
import { formatShare, formatUsd, sourceLabel } from "@/lib/usage";
import {
  heatmapGrid,
  shiftDay,
  formatTokens,
  type HeatLevel,
  type HeatMetric,
  type UsageByDay,
} from "@/lib/insights";

const CELL = 12;
const GAP = 3;
const PITCH = CELL + GAP;

export const HEAT_FILL: Record<HeatLevel, string> = {
  0: C.borderActive,
  1: alpha(C.accent, 0.22),
  2: alpha(C.accent, 0.42),
  3: alpha(C.accent, 0.66),
  4: C.accent,
};

export function UsageHeatmap({ data }: { data: UsageByDay }) {
  const t = useTranslations("insights.heatmap");
  const ts = useTranslations("insights.source");
  const locale = useLocale();
  const [metric, setMetric] = useState<HeatMetric>("all");
  const today = data.days[data.days.length - 1]?.date ?? "";
  const [selected, setSelected] = useState(today);
  const day = data.days.find((d) => d.date === selected);

  const dayLabel = (iso: string) =>
    new Intl.DateTimeFormat(locale, { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" }).format(
      new Date(`${iso}T00:00:00Z`),
    );
  const localShare = day && day.generated_tokens > 0 ? day.local_generated_tokens / day.generated_tokens : null;

  return (
    <section data-region="heatmap" aria-labelledby="heatmap-heading" className="min-w-0">
      <div className="flex items-center justify-between gap-3">
        <h2 id="heatmap-heading" className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {t("title")}
        </h2>
        <div
          role="radiogroup"
          aria-label={t("metricAria")}
          className="inline-flex rounded-full p-1"
          style={{ border: `1px solid ${C.border}` }}
        >
          {(["all", "local"] as const).map((m) => (
            <button
              key={m}
              type="button"
              role="radio"
              aria-checked={metric === m}
              onClick={() => setMetric(m)}
              className="min-h-11 px-4 rounded-full text-sm cursor-pointer transition-colors"
              style={{
                background: metric === m ? C.bgElevated : "transparent",
                color: metric === m ? C.textPrimary : C.textSecondary,
              }}
            >
              {t(m)}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-4 lg:hidden">
        <Grid data={data} weeks={26} metric={metric} selected={selected} onSelect={setSelected} />
      </div>
      <div className="mt-4 hidden lg:block">
        <Grid data={data} weeks={52} metric={metric} selected={selected} onSelect={setSelected} />
      </div>

      <div className="mt-2 flex items-center justify-end gap-1 text-xs" style={{ color: C.textMuted }} aria-hidden>
        <span className="mr-1">{t("less")}</span>
        {([0, 1, 2, 3, 4] as const).map((l) => (
          <span key={l} className="inline-block size-3 rounded-dense" style={{ background: HEAT_FILL[l] }} />
        ))}
        <span className="ml-1">{t("more")}</span>
      </div>

      <div
        className="mt-4 grid grid-cols-[2.75rem_1fr_2.75rem] items-center"
        style={{ borderTop: `1px solid ${C.border}`, borderBottom: `1px solid ${C.border}` }}
        data-testid="heatmap-day"
      >
        <button
          type="button"
          onClick={() => setSelected(shiftDay(data.days, selected, -1))}
          disabled={selected === data.days[0]?.date}
          aria-label={t("prev")}
          className="size-11 flex items-center justify-center cursor-pointer disabled:cursor-default disabled:opacity-40"
          style={{ color: C.textSecondary }}
        >
          <ChevronLeft size={18} aria-hidden />
        </button>
        <div className="py-3 min-w-0" aria-live="polite">
          <p className="text-sm font-medium" style={{ color: C.textPrimary }}>{selected && dayLabel(selected)}</p>
          <p className="mt-1 text-xs" style={{ color: C.textSecondary }}>
            {!day || day.generated_tokens === 0 ? (
              t("dayEmpty")
            ) : (
              <>
                <span className="tabular-nums">{t("dayTokens", { tokens: formatTokens(day.generated_tokens, locale) })}</span>
                {" · "}
                <span className="tabular-nums">{formatUsd(day.cost_usd, locale)}</span>
                {localShare !== null && localShare > 0 && (
                  <>
                    {" · "}
                    <span className="tabular-nums">{t("dayLocal", { share: formatShare(localShare, locale) ?? "" })}</span>
                  </>
                )}
                {day.top_source && (
                  <>
                    {" · "}
                    {t("mostly", { source: sourceText(ts, day.top_source) })}
                  </>
                )}
              </>
            )}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setSelected(shiftDay(data.days, selected, 1))}
          disabled={selected === today}
          aria-label={t("next")}
          className="size-11 flex items-center justify-center cursor-pointer disabled:cursor-default disabled:opacity-40"
          style={{ color: C.textSecondary }}
        >
          <ChevronRight size={18} aria-hidden />
        </button>
      </div>

      <p className="mt-2 text-xs" style={{ color: C.textMuted }}>{t("note", { tz: data.tz })}</p>
    </section>
  );
}

function sourceText(ts: ReturnType<typeof useTranslations>, source: string): string {
  const l = sourceLabel(source);
  return ts(l.key, { harness: l.harness ?? "" });
}

function Grid({
  data,
  weeks,
  metric,
  selected,
  onSelect,
}: {
  data: UsageByDay;
  weeks: number;
  metric: HeatMetric;
  selected: string;
  onSelect: (date: string) => void;
}) {
  const t = useTranslations("insights.heatmap");
  const locale = useLocale();
  const grid = useMemo(() => heatmapGrid(data.days, weeks, metric), [data.days, weeks, metric]);
  const month = new Intl.DateTimeFormat(locale, { month: "short", timeZone: "UTC" });
  const weekday = new Intl.DateTimeFormat(locale, { weekday: "short", timeZone: "UTC" });
  // 2026-09-28 is a Monday: rows 0 (Mon), 2 (Wed), 4 (Fri) get a label.
  const rowLabel = (row: number) => weekday.format(new Date(Date.UTC(2026, 8, 28 + row)));
  const width = weeks * PITCH - GAP;
  const height = 7 * PITCH - GAP;
  const selectedCol = grid.columns.findIndex((cells) => cells.some((c) => c?.date === selected));
  const selectedAt =
    selectedCol < 0 ? null : { col: selectedCol, row: grid.columns[selectedCol].findIndex((c) => c?.date === selected) };

  return (
    <div className="grid grid-cols-[2.75rem_1fr]">
      <div />
      <div className="relative h-4 text-xs" style={{ color: C.textMuted }} aria-hidden>
        {grid.months.map((m) => (
          <span key={`${m.col}-${m.month}`} className="absolute top-0" style={{ left: `${(m.col / weeks) * 100}%` }}>
            {month.format(new Date(Date.UTC(2026, m.month, 1)))}
          </span>
        ))}
      </div>
      <div className="grid grid-rows-7 text-xs leading-none" style={{ color: C.textMuted }} aria-hidden>
        {[0, 1, 2, 3, 4, 5, 6].map((row) => (
          <span key={row} className="flex items-center">{row % 2 === 0 && row < 6 ? rowLabel(row) : ""}</span>
        ))}
      </div>
      <svg
        viewBox={`-2 -2 ${width + 4} ${height + 4}`}
        className="w-full h-auto block"
        role="img"
        aria-label={t("gridAria", { weeks })}
        data-testid={`heatmap-grid-${weeks}`}
      >
        {grid.columns.map((cells, col) =>
          cells.map((cell, row) =>
            cell ? (
              <rect
                key={cell.date}
                x={col * PITCH}
                y={row * PITCH}
                width={CELL}
                height={CELL}
                rx={2}
                fill={HEAT_FILL[cell.level]}
                data-date={cell.date}
                data-level={cell.level}
                onClick={cell.inWindow ? () => onSelect(cell.date) : undefined}
                className={cell.inWindow ? "cursor-pointer" : undefined}
              >
                <title>{`${cell.date} · ${formatTokens(cell.value, locale)}`}</title>
              </rect>
            ) : null,
          ),
        )}
        {selectedAt && (
          // A ring around the cell, not a stroke on it: a stroke vanishes on the brightest level.
          <rect
            x={selectedAt.col * PITCH - 2}
            y={selectedAt.row * PITCH - 2}
            width={CELL + 4}
            height={CELL + 4}
            rx={3}
            fill="none"
            stroke={C.textPrimary}
            strokeWidth={1.5}
            pointerEvents="none"
            data-testid="heatmap-selected"
          />
        )}
      </svg>
    </div>
  );
}
