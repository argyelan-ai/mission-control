"use client";

/**
 * AsleepBox — die Blaupause: Box aus/schläft (Spec §3). Gestrichelter Rahmen,
 * Instrumente „–", Balken leer, Modus gesperrt (Opacity .5), Zone 4 = "Wake"
 * + Zahnrad.
 */

import { useTranslations } from "next-intl";
import { Settings } from "lucide-react";
import { C } from "@/lib/colors";
import type { Host } from "@/lib/types";
import { HeatStrip } from "./HeatStrip";
import { KpiRow, type KpiCell } from "./KpiRow";
import { WakeBoxButton } from "./WakeBoxButton";

export function AsleepBox({
  host,
  lastSeen,
  checking = false,
  noReadings = false,
  onOpenCockpit,
}: {
  /** The box — its power_managed flag carries "Wake" (POST /hosts/{id}/wake),
   *  no runtime row needs the flag (round-5 re-check L1). */
  host: Host;
  lastSeen?: string | null;
  /** The box's own check has not answered yet: nothing proves it sleeps
   *  (round-4 t3 S1) — the card says it is being checked, not "asleep". */
  checking?: boolean;
  /** The box's own check could not be made (the metrics call failed): no
   *  proof either way — "unknown", never "asleep" (PR #971 review). */
  noReadings?: boolean;
  onOpenCockpit: () => void;
}) {
  const t = useTranslations("runtimes.stage");

  const cells: KpiCell[] = [
    { value: "–", label: t("kpiMemoryFree") },
    { value: "–", label: t("kpiRecipesFit") },
    { value: "–", label: t("kpiAgents") },
    { value: "–", label: checking || noReadings ? t("kpiEndpoint") : t("kpiEndpointOff") },
  ];

  return (
    <div
      className="rounded-xl overflow-hidden"
      style={{ background: "transparent", border: `1px dashed ${C.border}`, containerType: "inline-size" }}
      data-testid="asleep-box"
      data-host-slug={host.slug}
    >
      <div className="px-4 pt-4">
        <div className="flex items-center gap-2.5">
          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: C.textDim }} />
          <span className="display font-semibold text-[20px] flex-1 min-w-0 truncate" style={{ color: C.textSecondary, opacity: 0.6 }}>
            {host.display_name}
          </span>
          <span className="font-mono uppercase shrink-0" style={{ fontSize: "10px", letterSpacing: "0.1em", color: C.textMuted }}>
            {checking ? t("cornerChecking") : noReadings ? t("cornerUnknown") : t("cornerAsleep")}
          </span>
        </div>
        <div style={{ opacity: 0.6 }}>
          <HeatStrip empty />
        </div>
        <div className="text-xs mt-2 pb-4" style={{ color: C.textMuted }}>
          {checking
            ? t("checkingLine")
            : noReadings
              ? t("noReadingsLine")
              : lastSeen
              ? t("asleepLastSeen", { time: lastSeen })
              : t("asleepLine")}
        </div>
      </div>
      <div style={{ opacity: 0.6 }}>
        <KpiRow cells={cells} />
      </div>
      <div className="flex items-center gap-2 px-4 py-3" style={{ borderTop: `1px dashed ${C.borderSubtle}` }}>
        {/* Not while the check is out: an awake box would read "checking"
            next to "Wake" and then flip to "Start model" — the contradicting
            claim round 4 reported (PR #971 review). */}
        {!checking && <WakeBoxButton host={host} />}
        <button
          type="button"
          onClick={onOpenCockpit}
          aria-label={t("cockpitAria")}
          className="stage-actions-gear w-11 h-11 flex items-center justify-center rounded-md cursor-pointer shrink-0 ml-auto"
          style={{ border: `1px solid ${C.borderActive}` }}
        >
          <Settings size={14} style={{ color: C.textSecondary }} />
        </button>
      </div>
    </div>
  );
}
