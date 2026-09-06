"use client";

/**
 * Stage — die laufende Bühne (Spec §2): ein Modell, eine Karte. Zonen in
 * fester Reihenfolge: 0 Lauflicht · 1 Lebenszeichen · 2 Instrumente ·
 * 3 Mitglieder · 4 Aktionen.
 *
 * HONESTY RULE (wie SlotStage, siehe dessen Kopfkommentar): nur echte Felder.
 * `Runtime` trägt keine Startzeit — die Karte zeigt darum keine erfundene
 * Laufzeit ("up 2 h 41" aus dem Mockup bleibt Referenz, keine Vorgabe für
 * ein Feld, das es nicht gibt).
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import type { Device, Host, Runtime, RuntimeLiveStatus } from "@/lib/types";
import { typeLabel } from "../SlotStage";
import { fmtCtx } from "@/lib/utils";
import { FlowEdge, type FlowKind } from "./FlowEdge";
import { HeatStrip } from "./HeatStrip";
import { KpiRow, type KpiCell } from "./KpiRow";
import { MemberRowContainer } from "./MemberRow";
import { ActionBar } from "./ActionBar";
import { PhaseBar } from "./PhaseBar";
import { useAppStore } from "@/lib/store";

export interface StageMember {
  host: Host;
  role: "head" | "worker" | null;
  device?: Device;
}

export type StageStatus = "serving" | "quiet" | "switching" | "failed";

export function deriveStageStatus(rt: Runtime, live?: RuntimeLiveStatus): StageStatus {
  if (live?.status === "switching") return "switching";
  if ((rt.state ?? "unknown") === "failed") return "failed";
  if (live?.reachable === false && (live?.consecutive_failures ?? 0) >= 3) return "failed";
  return "serving";
}

function flowKindFor(status: StageStatus, idleSeconds: number | null | undefined): FlowKind {
  if (status === "failed") return "dead";
  if (status === "switching") return "dim";
  if (idleSeconds != null && idleSeconds >= 300) return "quiet";
  return "live";
}

export function Stage({
  runtime,
  members,
  live,
  onOpenCockpit,
}: {
  runtime: Runtime;
  members: StageMember[];
  live?: RuntimeLiveStatus;
  onOpenCockpit: (rt: Runtime) => void;
}) {
  const t = useTranslations("runtimes.stage");
  const currentUser = useAppStore((s) => s.currentUser);
  const headHost = members.find((m) => m.role === "head" || m.role == null) ?? members[0];

  const { data: pulse } = useQuery({
    queryKey: ["hosts", headHost?.host.id, "pulse"],
    queryFn: () => api.hosts.pulse(headHost!.host.id),
    enabled: !!headHost,
    refetchInterval: 5_000,
  });

  const { data: agentsData } = useQuery({
    queryKey: ["runtime-agents", runtime.slug ?? runtime.id],
    queryFn: () => api.runtimes.db.agents((runtime.slug ?? runtime.id) as string),
    staleTime: 15_000,
    retry: false,
  });

  const status = useMemo(() => deriveStageStatus(runtime, live), [runtime, live]);
  const idleSeconds = pulse?.available ? pulse?.idle_seconds ?? null : null;
  const flowKind = flowKindFor(status, idleSeconds);
  const isDuo = members.length > 1;

  const tps = pulse?.available ? pulse?.now_tps ?? null : null;
  const latencyMs = live?.latency_ms ?? null;
  const nowLineParts = [
    tps != null ? t("tpsValue", { tps: Math.round(tps) }) : null,
    isDuo ? t("topologyDuo") : t("topologySolo"),
    latencyMs != null ? t("latencyValue", { ms: latencyMs }) : null,
  ].filter(Boolean);

  const dotColor =
    status === "failed" ? STATUS_TEXT.error : status === "switching" ? STATUS_TEXT.warning : STATUS.online;

  const endpointPort = runtime.endpoint?.match(/:(\d+)/)?.[1] ?? null;
  const cells: KpiCell[] = [
    { value: fmtCtx(live?.served_context_len ?? runtime.max_context_len), label: t("kpiContext") },
    { value: "–", label: t("kpiSpeedSolo") },
    { value: String(agentsData?.count ?? 0), label: t("kpiAgents") },
    {
      value: endpointPort != null ? `:${endpointPort}` : "–",
      label: t("kpiEndpointSlot"),
      copyValue: endpointPort != null ? runtime.endpoint : undefined,
    },
  ];

  return (
    <div
      className="relative rounded-xl overflow-hidden"
      style={{ background: C.bgSurface, border: `1px solid ${C.border}`, containerType: "inline-size" }}
      data-testid="stage-card"
      data-status={status}
    >
      <FlowEdge kind={flowKind} />

      <div className="relative px-4 pt-4" style={{ zIndex: 2, opacity: status === "switching" ? 0.75 : 1 }}>
        <div className="flex items-center gap-2.5">
          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: dotColor }} />
          <span className="display font-semibold text-[22px] sm:text-[28px] flex-1 min-w-0 truncate" style={{ color: C.textPrimary }}>
            {runtime.display_name}
          </span>
        </div>
        <HeatStrip pulse={pulse} dead={status === "failed"} />
        <div className="text-xs mt-2 pb-4 font-mono truncate" style={{ color: status === "failed" ? STATUS_TEXT.error : C.textMuted }}>
          {status === "switching"
            ? t("switchingTo", { model: runtime.display_name })
            : status === "failed"
              ? t("engineUnreachable")
              : nowLineParts.length > 0
                ? nowLineParts.join(" · ")
                : `${typeLabel(runtime.runtime_type)}${runtime.model_identifier ? ` · ${runtime.model_identifier}` : ""}`}
        </div>
      </div>

      <div className="relative" style={{ zIndex: 2 }}>
        <KpiRow cells={cells} />
      </div>

      <div
        className={`relative stage-members grid grid-cols-1 ${isDuo ? "" : ""}`}
        style={{ zIndex: 2, borderTop: `1px solid ${C.borderSubtle}` }}
        data-testid="stage-members"
        data-duo={isDuo ? "true" : "false"}
      >
        {members.map((m) => (
          <MemberRowContainer
            key={m.host.id}
            host={m.host}
            role={m.role}
            device={m.device}
            canControlDevice={currentUser?.role === "admin"}
          />
        ))}
      </div>

      <div className="relative" style={{ zIndex: 2 }}>
        {status === "switching" ? (
          <PhaseBar phase={live?.phase} targetLabel={t("switchLog")} cancelLabel={t("cancel")} />
        ) : (
          <ActionBar
            hostId={headHost?.host.id ?? ""}
            hostName={headHost?.host.slug ?? null}
            servingName={runtime.display_name}
            runtimeId={runtime.id}
            variant={status === "failed" ? "trouble" : "normal"}
            onOpenCockpit={() => onOpenCockpit(runtime)}
          />
        )}
      </div>
    </div>
  );
}
