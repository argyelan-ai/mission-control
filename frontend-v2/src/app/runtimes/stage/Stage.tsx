"use client";

/**
 * Stage — die laufende Bühne (Spec §2): ein Modell, eine Karte. Zonen in
 * fester Reihenfolge: 0 Lauflicht · 1 Lebenszeichen · 2 Instrumente ·
 * 3 Mitglieder · 4 Aktionen.
 *
 * HONESTY RULE (wie SlotStage, siehe dessen Kopfkommentar): nur echte Felder.
 * Seit #443 trägt `RuntimeLiveStatus.serving_since` (vom Wächter gesetzt/
 * gelöscht, nie clientseitig abgeleitet) die Laufzeit der Ecke rechts oben
 * ("up 2 h 41" / "up 34 min", Minuten-Auflösung) — fehlt der Wert (Modell
 * gerade erst geladen, Wächter noch ohne Probe), bleibt die Ecke beim
 * Zustandswort ("serving"). Wechsel-/Störungs-Ecke ("switching"/
 * "unreachable") bleibt Wort-only: weder `RuntimeLiveStatus` noch `Runtime`
 * tragen einen Phasenbeginn- oder Unreachable-Zeitstempel, ein erfundener
 * Wert wäre genau die Lüge, die diese Regel verbietet.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { api } from "@/lib/api";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import type { Device, Host, Runtime, RuntimeLiveStatus } from "@/lib/types";
import { typeLabel } from "../runtimeTypeLabel";
import { formatUptimeParts, pad2 } from "./uptimeFormat";
import { fmtCtx } from "@/lib/utils";
import { FlowEdge, type FlowKind } from "./FlowEdge";
import { HeatStrip } from "./HeatStrip";
import { KpiRow, type KpiCell } from "./KpiRow";
import { MemberRowContainer } from "./MemberRow";
import { ActionBar } from "./ActionBar";
import { PhaseBar } from "./PhaseBar";
import { shortModelTitle } from "./modelTitle";
import { useAppStore } from "@/lib/store";

export interface StageMember {
  host: Host;
  role: "head" | "worker" | null;
  device?: Device;
  /**
   * Die Slot-Runtime dieser Box (ADR-078, `is_slot=true`) — Team-Lead-Fund
   * 06.09.2026: die Agenten hängen an DIESER Zeile, nicht an der laufenden
   * Rezept-Runtime. Die Instrumente-Zelle "Agents" muss darum die Head-Box-
   * Slot-Runtime abfragen, nicht `runtime` (das Rezept).
   */
  slot?: Runtime | null;
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
  /** Cockpit öffnet immer für die Head-Box (Spec §4: "Head-Box bei Duo" —
   *  der Umschalter zu Worker sitzt im Cockpit-Kopf, nicht auf der Karte). */
  onOpenCockpit: (headHostId: string) => void;
}) {
  const t = useTranslations("runtimes.stage");
  const currentUser = useAppStore((s) => s.currentUser);
  const reduceMotion = useReducedMotion();
  const headHost = members.find((m) => m.role === "head" || m.role == null) ?? members[0];

  const { data: pulse } = useQuery({
    queryKey: ["hosts", headHost?.host.id, "pulse"],
    queryFn: () => api.hosts.pulse(headHost!.host.id),
    enabled: !!headHost,
    refetchInterval: 5_000,
  });

  // Team-Lead-Fund 06.09.2026: Agenten hängen an der Slot-Runtime der
  // Head-Box (ADR-078), nicht am laufenden Rezept — `runtime` bleibt nur
  // Fallback für Boxen ohne eigene Slot-Zeile (ältere Stände).
  const agentsSlug = (headHost?.slot?.slug ?? headHost?.slot?.id ?? runtime.slug ?? runtime.id) as string;
  const { data: agentsData } = useQuery({
    queryKey: ["runtime-agents", agentsSlug],
    queryFn: () => api.runtimes.db.agents(agentsSlug),
    enabled: !!agentsSlug,
    staleTime: 15_000,
    retry: false,
  });

  const status = useMemo(() => deriveStageStatus(runtime, live), [runtime, live]);
  const idleSeconds = pulse?.available ? pulse?.idle_seconds ?? null : null;
  const flowKind = flowKindFor(status, idleSeconds);
  const isDuo = members.length > 1;

  // Laufzeit-Ecke (#443/W3-Nachlese): nur solange serving — "switching"/
  // "unreachable" bleiben Wort-only (kein Phasenbeginn-/Unreachable-
  // Zeitstempel im Vertrag, s. Datei-Kopfkommentar). `live.serving_since`
  // ist der gespiegelte Wert extra für die Karte; `runtime.serving_since`
  // deckt den Rand ab, in dem der 30s-Live-Poll noch nicht nachgezogen hat,
  // aber die 15s-Runtime-Liste schon. Neu bei jedem Render gelesen (kein
  // eigener Ticker nötig) — Stage re-rendert ohnehin alle 5s über die
  // Puls-Abfrage oben, oft genug für eine Minuten-Auflösung.
  const uptime = status === "serving" ? formatUptimeParts(live?.serving_since ?? runtime.serving_since) : null;

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
          <span
            className="display font-semibold text-[22px] sm:text-[28px] flex-1 min-w-0 truncate"
            style={{ color: C.textPrimary }}
            title={runtime.display_name}
          >
            {shortModelTitle(runtime.display_name)}
          </span>
          {/* Ecke rechts oben (#443/W3-Nachlese): "up 2 h 41" / "up 34 min"
              sobald `serving_since` da ist, sonst das Zustandswort — nie
              erfunden (HONESTY RULE, s.o.). "switching"/"unreachable" bleiben
              Wort-only: ein Phasenbeginn-/Unreachable-Zeitstempel existiert
              im Vertrag (noch) nicht, s. Datei-Kopfkommentar. */}
          <span
            className="font-mono uppercase shrink-0"
            style={{ fontSize: "10px", letterSpacing: "0.08em", color: C.textMuted }}
          >
            {status === "failed"
              ? t("cornerUnreachable")
              : status === "switching"
                ? t("cornerSwitching")
                : uptime
                  ? uptime.hours >= 1
                    ? t("cornerUptimeHours", { h: uptime.hours, m: pad2(uptime.minutes) })
                    : t("cornerUptimeMinutes", { m: uptime.minutes })
                  : t("cornerServing")}
          </span>
        </div>
        <HeatStrip pulse={pulse} dead={status === "failed"} />
        <div className="text-xs mt-2 pb-4 font-mono truncate" style={{ color: status === "failed" ? STATUS_TEXT.error : C.textMuted }}>
          {status === "switching"
            ? t("switchingTo", { model: shortModelTitle(runtime.display_name) })
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
        {/* Duo→Solo-Übergang (Spec, PR 6 "Schliff"): verliert die Bühne ein
            Mitglied (Verbund endet), blendet dessen Zeile statt abrupt zu
            verschwinden — opacity + translateY, 200ms ease-out. `layout` lässt
            die verbleibende(n) Zeile(n) sanft nachrücken statt zu springen.
            `prefers-reduced-motion` → kein Übergang (initial=false, exit
            entfällt effektiv da AnimatePresence ohne Animation sofort entfernt). */}
        <AnimatePresence initial={false}>
          {members.map((m) => (
            <motion.div
              key={m.host.id}
              layout={!reduceMotion}
              initial={reduceMotion ? false : { opacity: 0, y: -8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduceMotion ? undefined : { opacity: 0, y: 8 }}
              transition={{ duration: 0.2, ease: "easeOut" }}
            >
              <MemberRowContainer
                host={m.host}
                role={m.role}
                device={m.device}
                canControlDevice={currentUser?.role === "admin"}
              />
            </motion.div>
          ))}
        </AnimatePresence>
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
            onOpenCockpit={() => onOpenCockpit(headHost?.host.id ?? "")}
          />
        )}
      </div>
    </div>
  );
}
