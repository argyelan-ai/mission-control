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
import { WakeBoxButton } from "./WakeBoxButton";
import { NoModelTestButton } from "./BenchTest";

export function FreeBox({
  host,
  slot,
  device,
  probeTimedOut = false,
  probePending = false,
  onOpenCockpit,
}: {
  host: Host;
  /** Slot-Runtime (ADR-078) dieser Box, falls vorhanden — liefert Endpoint +
   *  gebundene Agenten für eine sonst leere Box. */
  slot: Runtime | null;
  device?: Device;
  /** GET /runtimes cut this box's state probe off: nothing is known about
   *  it, so the card must not claim "free · no model · ready". */
  probeTimedOut?: boolean;
  /** The page painted from the unprobed rows (round-2 Q6) and the live
   *  probe has not answered yet — "being checked", not "free". */
  probePending?: boolean;
  onOpenCockpit: () => void;
}) {
  const t = useTranslations("runtimes.stage");
  const tRuntimes = useTranslations("runtimes");
  const currentUser = useAppStore((s) => s.currentUser);

  const { data: metrics, isError: metricsFailed } = useQuery({
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

  // "Recipes fit" is about THIS box: a recipe for two or more boxes needs
  // other boxes and is no capacity of this one (round-5 re-check L1, t2 NICE-3).
  const recipesFit = useMemo(() => {
    const list = recipes ?? [];
    if (list.length === 0) return null;
    const fitCount = list.filter((r) => (r.topology?.nodes ?? 1) <= 1 && r.capacity?.ok === true).length;
    const anyCapacity = list.some((r) => r.capacity != null);
    return anyCapacity ? fitCount : null;
  }, [recipes]);

  const hasVram = metrics?.vram_total_mb != null;
  const memTotal = hasVram ? metrics?.vram_total_mb : metrics?.ram_total_mb;
  const memUsed = hasVram ? metrics?.vram_used_mb : metrics?.ram_used_mb;
  const memFreeGb =
    memTotal != null && memUsed != null ? Math.max(0, Math.round((memTotal - memUsed) / 1024)) : null;

  const endpointPort = slot?.endpoint?.match(/:(\d+)/)?.[1] ?? recipes?.[0]?.port ?? null;

  // What the card may claim (round-2 user test t4 S2: a sleeping box read
  // "free · no model · ready" while its own metrics said reachable:false).
  // "ready" needs the box to answer; until its metrics are in, or while the
  // page still paints from the unprobed rows, it is being checked.
  // Only the box's own check says it does not answer. The runtime watcher's
  // "dark" is about the MODEL port — an awake box without a model is dark
  // there too, and read SCHLÄFT → PRÜFT → FREI in 2 s, once "asleep" next to
  // "Start model" (round-4 user test t3 S1, r2 S1). Until the check is in,
  // the card is being checked.
  const unreachable = metrics?.reachable === false;
  const asleep = unreachable && (host.power_managed || metrics?.status === "asleep");
  // A failed metrics call says nothing about the box — the runtime probe
  // already called it free (no model), so the corner keeps "free" instead of
  // "checking" forever; the line says the readings are missing rather than
  // claiming "ready" without a proof (PR #960 review, NICE).
  const checking = probePending || (metrics == null && !metricsFailed);
  const noReadings = !checking && metrics == null && metricsFailed;
  // "Start model" waits for the box's own check only where "no answer yet"
  // can mean "asleep": a power-managed box. Any other box keeps its Start
  // while a slow metrics call is out (PR #976 review, SHOULD 2) — only the
  // unprobed first paint (probePending) holds it back there.
  const startHeld = probePending || (host.power_managed && checking);
  const corner = probeTimedOut
    ? t("cornerUnknown")
    : unreachable
      ? asleep ? t("cornerAsleep") : t("cornerUnreachable")
      : checking
        ? t("cornerChecking")
        : t("cornerFree");
  // The corner names the state; the line never repeats it (round-3 r2 N1:
  // "ASLEEP" over "asleep · not reachable"). It says what that leaves.
  const line = unreachable
    ? t("freeLineNoReadings")
    : checking
      ? t("checkingLine")
      : noReadings
        ? t("freeLineNoReadings")
        : t("freeLine");

  const cells: KpiCell[] = [
    { value: memFreeGb != null ? String(memFreeGb) : "–", unit: memFreeGb != null ? "GB" : undefined, label: t("kpiMemoryFree") },
    { value: recipesFit != null ? String(recipesFit) : "–", label: t("kpiRecipesFit") },
    { value: String(agentsData?.count ?? 0), label: t("kpiAgents") },
    {
      value: endpointPort != null ? `:${endpointPort}` : "–",
      // "· idle" is a claim about the engine — not made while unchecked
      // (rows or the box's own check still out), nor for a box that does
      // not answer (round-3: "asleep" over "Endpoint · idle").
      label: checking || unreachable ? t("kpiEndpoint") : t("kpiEndpointIdle"),
      copyValue: endpointPort != null ? slot?.endpoint ?? `:${endpointPort}` : undefined,
    },
  ];

  return (
    <div
      className="rounded-xl overflow-hidden stage-card"
      style={{ background: C.bgSurface, border: `1px solid ${C.border}`, containerType: "inline-size" }}
      data-testid="free-box"
      // Disambiguates one FreeBox from another in tests when several boxes
      // are free at once — a text-content assertion risks matching a
      // transient render of the WRONG box's card during data loading (Fund
      // #442 CI-Flake, PR 6 Nachlese).
      data-host-slug={host.slug}
    >
      <div className="px-4 pt-4">
        <div className="flex items-center gap-2.5">
          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: C.textDim }} />
          <span className="display font-semibold text-[20px] flex-1 min-w-0 truncate" style={{ color: C.textSecondary }}>
            {host.display_name}
          </span>
          <span className="font-mono uppercase shrink-0" style={{ fontSize: "10px", letterSpacing: "0.1em", color: C.textMuted }}>
            {corner}
          </span>
        </div>
        <HeatStrip empty />
        {probeTimedOut ? (
          <div className="text-xs mt-2 pb-4" style={{ color: C.textMuted }} title={tRuntimes("probeTimedOutHint")}>
            {tRuntimes("unknownProbeTimedOut")}
          </div>
        ) : (
          <div className="text-xs mt-2 pb-4" style={{ color: C.textMuted }} data-testid="free-box-line">
            {line}
          </div>
        )}
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
        {/* No armed "Start model" while the box is still being checked: on
            the first paint a running duo would offer to start a second
            model onto its own boxes (PR #960 review, SHOULD 1). Nor while
            a power-managed box's own check is out or says it sleeps: a
            sleeping box starts nothing — it is woken first (round-5 L1). */}
        {asleep ? (
          <WakeBoxButton host={host} />
        ) : (
          !startHeld && (
            <HostRecipeSwitcher hostId={host.id} hostName={host.slug} servingName={null} compact primary label={t("startModel")} />
          )
        )}
        {/* Bench W1 P1: nothing to test until a model runs (mockup 01). */}
        <span className="ml-auto">
          <NoModelTestButton />
        </span>
        <button
          type="button"
          onClick={onOpenCockpit}
          aria-label={t("cockpitAria")}
          className="stage-actions-gear w-11 h-11 flex items-center justify-center rounded-md cursor-pointer shrink-0"
          style={{ border: `1px solid ${C.borderActive}` }}
        >
          <Settings size={14} style={{ color: C.textSecondary }} />
        </button>
      </div>
    </div>
  );
}
