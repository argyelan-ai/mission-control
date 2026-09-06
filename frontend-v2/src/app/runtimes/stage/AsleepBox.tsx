"use client";

/**
 * AsleepBox — die Blaupause: Box aus/schläft (Spec §3). Gestrichelter Rahmen,
 * Instrumente „–", Balken leer, Modus gesperrt (Opacity .5), Zone 4 = "Wake"
 * + Zahnrad.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Loader2, Power, Settings } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { Host, Runtime } from "@/lib/types";
import { HeatStrip } from "./HeatStrip";
import { KpiRow, type KpiCell } from "./KpiRow";

export function AsleepBox({
  host,
  runtime,
  lastSeen,
  onOpenCockpit,
}: {
  host: Host;
  /** Die power_managed-Runtime dieser Box — trägt den Wake-Aufruf. */
  runtime: Runtime;
  lastSeen?: string | null;
  onOpenCockpit: () => void;
}) {
  const t = useTranslations("runtimes.stage");
  const queryClient = useQueryClient();

  const wakeMutation = useMutation({
    mutationFn: () => api.runtimes.wake(runtime.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["runtimes"] }),
  });

  const cells: KpiCell[] = [
    { value: "–", label: t("kpiMemoryFree") },
    { value: "–", label: t("kpiRecipesFit") },
    { value: "–", label: t("kpiAgents") },
    { value: "–", label: t("kpiEndpointOff") },
  ];

  return (
    <div
      className="rounded-xl overflow-hidden"
      style={{ background: "transparent", border: `1px dashed ${C.border}`, containerType: "inline-size" }}
      data-testid="asleep-box"
    >
      <div className="px-4 pt-4">
        <div className="flex items-center gap-2.5">
          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: C.textDim }} />
          <span className="display font-semibold text-[20px] flex-1 min-w-0 truncate" style={{ color: C.textSecondary, opacity: 0.6 }}>
            {host.display_name}
          </span>
          <span className="font-mono uppercase shrink-0" style={{ fontSize: "10px", letterSpacing: "0.1em", color: C.textMuted }}>
            {t("cornerAsleep")}
          </span>
        </div>
        <div style={{ opacity: 0.6 }}>
          <HeatStrip empty />
        </div>
        <div className="text-xs mt-2 pb-4" style={{ color: C.textMuted }}>
          {lastSeen ? t("poweredOffLastSeen", { time: lastSeen }) : t("poweredOff")}
        </div>
      </div>
      <div style={{ opacity: 0.6 }}>
        <KpiRow cells={cells} />
      </div>
      <div className="flex items-center gap-2 px-4 py-3" style={{ borderTop: `1px dashed ${C.borderSubtle}` }}>
        <button
          type="button"
          onClick={() => wakeMutation.mutate()}
          disabled={wakeMutation.isPending}
          data-testid="wake-box"
          className="flex items-center gap-1.5 text-xs px-3.5 py-2.5 rounded-md cursor-pointer disabled:opacity-50"
          style={{ border: `1px solid ${C.borderActive}`, color: C.textSecondary }}
        >
          {wakeMutation.isPending ? <Loader2 size={12} className="animate-spin" /> : <Power size={12} />}
          {t("wake")}
        </button>
        <button
          type="button"
          onClick={onOpenCockpit}
          aria-label={t("cockpitAria")}
          className="w-9 h-9 flex items-center justify-center rounded-md cursor-pointer shrink-0 ml-auto"
          style={{ border: `1px solid ${C.borderActive}` }}
        >
          <Settings size={14} style={{ color: C.textSecondary }} />
        </button>
      </div>
    </div>
  );
}
