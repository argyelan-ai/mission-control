"use client";

/**
 * TelemetryChart — Cockpit-Gruppe „Telemetry" (Spec §4.1). SVG-Diagramm der
 * letzten Stunde aus `GET /hosts/{id}/metrics/history` (Spec §6, PR 3 —
 * Backend-PR läuft parallel; `api.hosts.metricsHistory` fängt jeden Fehler
 * inkl. 404 ab und liefert `{points:[]}`, siehe dessen Kommentar). Leer
 * (`points: []`) → ruhige Leerfläche mit „collecting…" statt eines Fehlers
 * (HONESTY RULE, wie Puls/HeatStrip).
 *
 * GPU-Linie + Fläche 8% (C.accent), RAM gestrichelt (C.textMuted), Temp
 * (C.warning). Alle drei auf einer gemeinsamen 0–100-Skala (GPU/RAM als
 * Prozent, Temp als roher °C-Wert) — exakt die Mockup-Vorgabe (`build-a.py
 * chart()`), keine eigene Temp-Achse.
 *
 * KEINE Fan-Prozentzahl in der Legende: `HostMetricsHistoryPoint.fan` kommt
 * vom Backend-Vertrag mit, aber `DeviceState` (die einzige heute wirklich
 * befüllte Quelle) trägt keinen Fan-Wert — eine erfundene Zahl wäre die
 * gleiche Lüge, die HeatStrip/ModeList schon vermeiden.
 */

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import type { HostMetricsHistoryPoint } from "@/lib/types";
import { api } from "@/lib/api";

const W = 360;
const H = 96;
const PAD_X = 6;
const PAD_TOP = 8;
const PAD_BOTTOM = 12;

function pathFor(points: HostMetricsHistoryPoint[], pick: (p: HostMetricsHistoryPoint) => number | null): string {
  const usable = points.map((p, i) => ({ i, v: pick(p) })).filter((p): p is { i: number; v: number } => p.v != null);
  if (usable.length === 0) return "";
  const n = points.length;
  const x = (i: number) => PAD_X + (i * (W - 2 * PAD_X)) / Math.max(1, n - 1);
  const y = (v: number) => H - PAD_BOTTOM - (Math.max(0, Math.min(100, v)) / 100) * (H - PAD_TOP - PAD_BOTTOM);
  return usable.map(({ i, v }, idx) => `${idx === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
}

function ramPct(p: HostMetricsHistoryPoint): number | null {
  if (p.ram_used == null || !p.ram_total) return null;
  return (p.ram_used / p.ram_total) * 100;
}

export function TelemetryChart({ hostId }: { hostId: string }) {
  const t = useTranslations("runtimes.cockpit");
  const { data } = useQuery({
    queryKey: ["hosts", hostId, "metrics-history"],
    queryFn: () => api.hosts.metricsHistory(hostId, 3600),
    refetchInterval: 30_000,
  });

  const points = data?.points ?? [];
  const hasData = points.length > 0;

  const last = hasData ? points[points.length - 1] : null;
  const gpuPath = hasData ? pathFor(points, (p) => p.gpu) : "";
  const ramPath = hasData ? pathFor(points, ramPct) : "";
  const tempPath = hasData ? pathFor(points, (p) => p.temp) : "";
  const gpuArea = gpuPath ? `${gpuPath} L${(W - PAD_X).toFixed(1)},${(H - PAD_BOTTOM).toFixed(1)} L${PAD_X},${(H - PAD_BOTTOM).toFixed(1)} Z` : "";

  if (!hasData) {
    return (
      <div data-testid="telemetry-chart-empty" className="flex items-center justify-center rounded-md" style={{ height: H, background: C.bgDeep, border: `1px solid ${C.borderSubtle}` }}>
        <span className="font-mono text-[11px]" style={{ color: C.textMuted }}>{t("telemetryCollecting")}</span>
      </div>
    );
  }

  const lastRamPct = ramPct(last!);

  return (
    <div data-testid="telemetry-chart">
      <svg
        className="block w-full"
        style={{ height: H }}
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={t("telemetryChartAria")}
      >
        <line x1={PAD_X} y1={H - PAD_BOTTOM} x2={W - PAD_X} y2={H - PAD_BOTTOM} stroke={C.borderActive} strokeWidth={1} />
        <line
          x1={PAD_X}
          y1={PAD_TOP + (H - PAD_TOP - PAD_BOTTOM) / 2}
          x2={W - PAD_X}
          y2={PAD_TOP + (H - PAD_TOP - PAD_BOTTOM) / 2}
          stroke={C.borderActive}
          strokeWidth={1}
        />
        <line x1={PAD_X} y1={PAD_TOP} x2={W - PAD_X} y2={PAD_TOP} stroke={C.borderActive} strokeWidth={1} />
        {gpuArea && <path d={gpuArea} fill={C.accent} opacity={0.08} />}
        {gpuPath && <path d={gpuPath} fill="none" stroke={C.accent} strokeWidth={1.5} vectorEffect="non-scaling-stroke" data-testid="telemetry-gpu-path" />}
        {ramPath && (
          <path d={ramPath} fill="none" stroke={C.textMuted} strokeWidth={1.2} strokeDasharray="3 3" vectorEffect="non-scaling-stroke" data-testid="telemetry-ram-path" />
        )}
        {tempPath && <path d={tempPath} fill="none" stroke={C.warning} strokeWidth={1.2} vectorEffect="non-scaling-stroke" data-testid="telemetry-temp-path" />}
        <text x={W - PAD_X} y={PAD_TOP - 2} textAnchor="end" fontSize={9} fill={C.textMuted}>100</text>
        <text x={W - PAD_X} y={H - PAD_BOTTOM + 8} textAnchor="end" fontSize={9} fill={C.textMuted}>0</text>
        <text x={PAD_X} y={H - 2} fontSize={9} fill={C.textMuted}>{t("telemetryAxisStart")}</text>
        <text x={W - PAD_X} y={H - 2} textAnchor="end" fontSize={9} fill={C.textMuted}>{t("telemetryAxisNow")}</text>
      </svg>
      <div className="flex flex-wrap gap-3.5 font-mono mt-1.5" style={{ fontSize: "10px", color: C.textMuted }} data-testid="telemetry-legend">
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden style={{ display: "inline-block", width: 10, height: 2, background: C.accent }} />
          {t("legendGpu", { value: last?.gpu != null ? `${Math.round(last.gpu)}` : "–" })}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden style={{ display: "inline-block", width: 10, height: 2, background: C.textMuted }} />
          {t("legendRam", { value: lastRamPct != null ? `${Math.round(lastRamPct)}` : "–" })}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden style={{ display: "inline-block", width: 10, height: 2, background: C.warning }} />
          {t("legendTemp", { value: last?.temp != null ? `${Math.round(last.temp)}` : "–" })}
        </span>
      </div>
    </div>
  );
}
