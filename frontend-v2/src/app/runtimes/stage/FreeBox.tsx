"use client";

/**
 * FreeBox — die freie Karte: Box läuft, kein Modell (Spec §3
 * „Kapazitäts-Ausweis"). Gleiche Zonen wie die Bühne, aber ohne Lauflicht.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Settings } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { Device, Host, Runtime } from "@/lib/types";
import { HeatStrip } from "./HeatStrip";
import { KpiRow, type KpiCell } from "./KpiRow";
import { MemberRow } from "./MemberRow";
import { HostRecipeSwitcher } from "@/components/shared/HostRecipeSwitcher";
import { useAppStore } from "@/lib/store";

export function FreeBox({
  host,
  slot,
  device,
  onOpenCockpit,
}: {
  host: Host;
  /** Slot-Runtime (ADR-078) dieser Box, falls vorhanden — liefert Endpoint +
   *  gebundene Agenten für eine sonst leere Box. */
  slot: Runtime | null;
  device?: Device;
  onOpenCockpit: () => void;
}) {
  const t = useTranslations("runtimes.stage");
  const currentUser = useAppStore((s) => s.currentUser);

  const { data: metrics } = useQuery({
    queryKey: ["hosts", host.id, "metrics"],
    queryFn: () => api.hosts.metrics(host.id),
    refetchInterval: 5_000,
  });

  const { data: recipes } = useQuery({
    queryKey: ["hosts", host.id, "recipes"],
    queryFn: () => api.hosts.recipes(host.id),
    staleTime: 15_000,
  });

  const { data: agentsData } = useQuery({
    queryKey: ["runtime-agents", slot?.slug ?? slot?.id],
    queryFn: () => api.runtimes.db.agents((slot!.slug ?? slot!.id) as string),
    enabled: !!slot,
    staleTime: 15_000,
    retry: false,
  });

  const recipesFit = useMemo(() => {
    const list = recipes ?? [];
    if (list.length === 0) return null;
    const fitCount = list.filter((r) => r.capacity?.ok === true).length;
    const anyCapacity = list.some((r) => r.capacity != null);
    return anyCapacity ? fitCount : null;
  }, [recipes]);

  const hasVram = metrics?.vram_total_mb != null;
  const memTotal = hasVram ? metrics?.vram_total_mb : metrics?.ram_total_mb;
  const memUsed = hasVram ? metrics?.vram_used_mb : metrics?.ram_used_mb;
  const memFreeGb =
    memTotal != null && memUsed != null ? Math.max(0, Math.round((memTotal - memUsed) / 1024)) : null;

  const endpointPort = slot?.endpoint?.match(/:(\d+)/)?.[1] ?? recipes?.[0]?.port ?? null;

  const cells: KpiCell[] = [
    { value: memFreeGb != null ? String(memFreeGb) : "–", unit: memFreeGb != null ? "GB" : undefined, label: t("kpiMemoryFree") },
    { value: recipesFit != null ? String(recipesFit) : "–", label: t("kpiRecipesFit") },
    { value: String(agentsData?.count ?? 0), label: t("kpiAgents") },
    {
      value: endpointPort != null ? `:${endpointPort}` : "–",
      label: t("kpiEndpointIdle"),
      copyValue: endpointPort != null ? slot?.endpoint ?? `:${endpointPort}` : undefined,
    },
  ];

  return (
    <div
      className="rounded-xl overflow-hidden stage-card"
      style={{ background: C.bgSurface, border: `1px solid ${C.border}`, containerType: "inline-size" }}
      data-testid="free-box"
    >
      <div className="px-4 pt-4">
        <div className="flex items-center gap-2.5">
          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: C.textDim }} />
          <span className="display font-semibold text-[20px] flex-1 min-w-0 truncate" style={{ color: C.textSecondary }}>
            {host.display_name}
          </span>
          <span className="font-mono uppercase shrink-0" style={{ fontSize: "10px", letterSpacing: "0.1em", color: C.textMuted }}>
            {t("cornerFree")}
          </span>
        </div>
        <HeatStrip empty />
        <div className="text-xs mt-2 pb-4" style={{ color: C.textMuted }}>
          {t("freeLine")}
        </div>
      </div>
      <KpiRow cells={cells} />
      <div className="stage-members grid grid-cols-1" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
        <MemberRow
          name={host.display_name}
          role={host.role}
          online={metrics?.reachable === true}
          metrics={metrics}
          device={device}
          canControlDevice={currentUser?.role === "admin"}
        />
      </div>
      <div className="flex items-center gap-2 px-4 py-3" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
        <HostRecipeSwitcher hostId={host.id} hostName={host.slug} servingName={null} compact primary label={t("startModel")} />
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
