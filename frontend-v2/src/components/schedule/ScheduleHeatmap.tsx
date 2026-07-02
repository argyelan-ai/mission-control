"use client";

/**
 * ScheduleHeatmap — 7×24 grid showing run density per (weekday, hour).
 *
 * Plain div grid (lighter than Recharts for this 168-cell case). Cells
 * use opacity-scaled accent color — the more runs, the brighter.
 */

import type { ScheduleHeatmapCell } from "@/lib/types";
import { C } from "@/lib/colors";

interface ScheduleHeatmapProps {
  data: ScheduleHeatmapCell[];
  title?: string;
}

const WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const HOUR_LABELS = [0, 6, 12, 18];
// C.accent = "#0FA3A3" → RGB for use in rgba()
const ACCENT = "15,163,163";

export function ScheduleHeatmap({ data, title }: ScheduleHeatmapProps) {
  // Build lookup map (weekday, hour) → count
  const map = new Map<string, number>();
  let max = 0;
  for (const c of data) {
    map.set(`${c.weekday}-${c.hour}`, c.count);
    if (c.count > max) max = c.count;
  }

  const intensity = (count: number): number => {
    if (max === 0 || count === 0) return 0;
    // Smooth log-ish ramp 0.15 → 1
    return 0.15 + 0.85 * (Math.log(1 + count) / Math.log(1 + max));
  };

  return (
    <div
      className="flex flex-col gap-3 rounded-lg p-4"
      style={{ border: `1px solid ${C.borderSubtle}`, background: C.bgSurface }}
    >
      {title && (
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium" style={{ color: C.textPrimary }}>{title}</h3>
          <span className="text-[10px]" style={{ color: C.textDim }}>
            {max === 0 ? "Keine Daten" : `Max: ${max} Lauf${max === 1 ? "" : "e"}`}
          </span>
        </div>
      )}

      <div className="flex gap-2">
        {/* Weekday labels */}
        <div className="grid grid-rows-7 gap-1 pt-5 text-[10px]" style={{ color: C.textDim }}>
          {WEEKDAYS.map((d) => (
            <div
              key={d}
              className="flex h-4 items-center justify-end"
              style={{ minHeight: "16px" }}
            >
              {d}
            </div>
          ))}
        </div>

        {/* Grid */}
        <div className="flex flex-1 flex-col gap-1">
          {/* Hour labels */}
          <div
            className="grid gap-0.5 text-[9px]"
            style={{ gridTemplateColumns: "repeat(24, minmax(0, 1fr))", color: C.textDim }}
          >
            {Array.from({ length: 24 }, (_, h) => (
              <div key={h} className="h-4 text-center">
                {HOUR_LABELS.includes(h) ? h : ""}
              </div>
            ))}
          </div>

          {/* Cells */}
          <div
            className="grid gap-0.5"
            style={{
              gridTemplateRows: "repeat(7, 16px)",
              gridTemplateColumns: "repeat(24, minmax(0, 1fr))",
              gridAutoFlow: "row",
            }}
          >
            {Array.from({ length: 7 }, (_, w) =>
              Array.from({ length: 24 }, (_, h) => {
                const c = map.get(`${w}-${h}`) ?? 0;
                const a = intensity(c);
                return (
                  <div
                    key={`${w}-${h}`}
                    title={`${WEEKDAYS[w]} ${String(h).padStart(2, "0")}:00 — ${c} Lauf${c === 1 ? "" : "e"}`}
                    className="rounded-sm transition-colors"
                    style={{
                      gridColumn: h + 1,
                      gridRow: w + 1,
                      background:
                        a === 0
                          ? C.borderSubtle
                          : `rgba(${ACCENT}, ${a.toFixed(2)})`,
                      border:
                        a > 0
                          ? `1px solid rgba(${ACCENT}, ${Math.min(1, a + 0.1).toFixed(2)})`
                          : `1px solid ${C.borderSubtle}`,
                    }}
                  />
                );
              }),
            )}
          </div>
        </div>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-2 text-[10px]" style={{ color: C.textDim }}>
        <span>Wenig</span>
        <div className="flex gap-0.5">
          {[0.15, 0.4, 0.65, 0.9].map((a) => (
            <span
              key={a}
              className="block h-3 w-4 rounded-sm"
              style={{ background: `rgba(${ACCENT}, ${a})` }}
            />
          ))}
        </div>
        <span>Viel</span>
      </div>
    </div>
  );
}
