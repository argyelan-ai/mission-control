"use client";

/**
 * FleetStage — die neue Fleet-Ansicht v2 „Die Bühne" (Spec, Welle 4).
 *
 * Gruppiert wie die heutige Seite (`grouping.ts`), aber auf Modell-Ebene
 * statt Box-Ebene: ein laufendes Modell = eine Karte (`Stage`), mit Head +
 * Worker als Mitgliedern. Freie erreichbare Boxen werden `FreeBox`,
 * schlafende/aus `AsleepBox`. Läuft nichts und sind Boxen frei, zeigt die
 * Seite den Leer-Zustand (Spec §3).
 */

import { useMemo } from "react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import type { Host, Runtime, RuntimeLiveStatus } from "@/lib/types";
import type { HostGroup } from "../grouping";
import { pickServing, pickSlot } from "../grouping";
import { useDevices } from "../DeviceControl";
import { Stage, type StageMember } from "./Stage";
import { FreeBox } from "./FreeBox";
import { AsleepBox } from "./AsleepBox";

export interface BuiltStage {
  runtime: Runtime;
  hostIds: string[];
}

/** Pure grouping — exported for the Vitest suite (no querying, no hooks). */
export function buildStages(
  stageGroups: HostGroup[],
  live?: Record<string, RuntimeLiveStatus>
): { stages: BuiltStage[]; freeHostIds: Set<string> } {
  const seen = new Set<string>();
  const stages: BuiltStage[] = [];

  for (const group of stageGroups) {
    if (seen.has(group.host.id)) continue;
    const serving = pickServing(group, live);
    if (!serving) continue;
    const hostIds = [group.host.id, ...(serving.member_hosts ?? []).map((m) => m.host_id)];
    for (const id of hostIds) seen.add(id);
    stages.push({ runtime: serving, hostIds });
  }

  const freeHostIds = new Set<string>();
  for (const group of stageGroups) {
    if (!seen.has(group.host.id)) freeHostIds.add(group.host.id);
  }
  return { stages, freeHostIds };
}

function StageRow({
  runtime,
  hostIds,
  hostsById,
  live,
  onOpenCockpit,
}: {
  runtime: Runtime;
  hostIds: string[];
  hostsById: Map<string, Host>;
  live?: Record<string, RuntimeLiveStatus>;
  onOpenCockpit: (rt: Runtime) => void;
}) {
  const devices = useDevices();
  const memberOf = new Map((runtime.member_hosts ?? []).map((m) => [m.host_id, m]));

  const members: StageMember[] = hostIds
    .map((id) => hostsById.get(id))
    .filter((h): h is Host => !!h)
    .map((host) => ({
      host,
      role: host.id === hostIds[0] ? "head" : (memberOf.get(host.id)?.role ?? "worker"),
      device: devices.get(host.id),
    }));

  const rtLive = live?.[runtime.slug ?? runtime.id];
  return <Stage runtime={runtime} members={members} live={rtLive} onOpenCockpit={onOpenCockpit} />;
}

function EmptyStage({ groups, onOpenCockpit }: { groups: HostGroup[]; onOpenCockpit: (rt: Runtime) => void }) {
  const t = useTranslations("runtimes.stage");
  const target = groups[0] ? (pickSlot(groups[0]) ?? groups[0].runtimes[0] ?? null) : null;
  return (
    <div className="rounded-xl overflow-hidden" style={{ background: C.bgSurface, border: `1px solid ${C.border}` }} data-testid="empty-stage">
      <div className="flex items-center justify-between px-4 py-3" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
        <span className="flex items-center gap-2 text-xs" style={{ color: C.textMuted }}>
          <span className="w-2 h-2 rounded-full" style={{ background: C.textDim }} />
          {t("noModel")}
        </span>
        <span className="text-xs font-mono" style={{ color: C.textMuted }}>
          {t("boxesReady", { count: groups.length })}
        </span>
      </div>
      <div className="flex flex-col items-center gap-2 px-4 py-8 text-center">
        <span className="text-sm font-medium" style={{ color: C.textPrimary }}>{t("nothingRunning")}</span>
        <span className="text-xs max-w-xs" style={{ color: C.textMuted }}>{t("nothingRunningHint")}</span>
        {target && (
          <div className="mt-1">
            <button
              type="button"
              onClick={() => onOpenCockpit(target)}
              className="text-xs font-medium px-3.5 py-2.5 rounded-md cursor-pointer"
              style={{ background: C.accent, color: C.onAccent }}
            >
              {t("startModel")}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export function FleetStage({
  stageGroups,
  sleepingGroups,
  live,
  onOpen,
}: {
  stageGroups: HostGroup[];
  sleepingGroups: HostGroup[];
  live?: Record<string, RuntimeLiveStatus>;
  onOpen: (rt: Runtime) => void;
}) {
  const { stages, freeHostIds } = useMemo(() => buildStages(stageGroups, live), [stageGroups, live]);
  const hostsById = useMemo(() => new Map(stageGroups.map((g) => [g.host.id, g.host])), [stageGroups]);
  const devices = useDevices();

  const freeGroups = stageGroups.filter((g) => freeHostIds.has(g.host.id));
  const isFullyEmpty = stages.length === 0;

  return (
    <div className="flex flex-col gap-6" data-testid="fleet-stage">
      {isFullyEmpty && freeGroups.length > 0 && sleepingGroups.length === 0 ? (
        <EmptyStage groups={freeGroups} onOpenCockpit={onOpen} />
      ) : (
        <>
          {stages.map(({ runtime, hostIds }) => (
            <StageRow
              key={runtime.id}
              runtime={runtime}
              hostIds={hostIds}
              hostsById={hostsById}
              live={live}
              onOpenCockpit={onOpen}
            />
          ))}
          {freeGroups.map((g) => {
            const slot = pickSlot(g);
            const target = slot ?? g.runtimes[0] ?? null;
            return (
              <FreeBox
                key={g.host.id}
                host={g.host}
                slot={slot}
                device={devices.get(g.host.id)}
                onOpenCockpit={() => target && onOpen(target)}
              />
            );
          })}
        </>
      )}
      {sleepingGroups.map((g) => {
        const rt = g.runtimes.find((r) => r.power_managed === true);
        if (!rt) return null;
        return <AsleepBox key={g.host.id} host={g.host} runtime={rt} onOpenCockpit={() => onOpen(rt)} />;
      })}
    </div>
  );
}
