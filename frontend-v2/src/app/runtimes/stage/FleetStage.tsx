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

import { useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import type { Runtime, RuntimeLiveStatus } from "@/lib/types";
import type { HostGroup } from "../grouping";
import { pickServing, pickSlot } from "../grouping";
import { useDevices } from "../DeviceControl";
import { Stage, type StageMember } from "./Stage";
import { FreeBox } from "./FreeBox";
import { AsleepBox } from "./AsleepBox";
import { BoxCockpit, type BoxCockpitMember } from "./cockpit/BoxCockpit";

export interface BuiltStage {
  runtime: Runtime;
  hostIds: string[];
}

/**
 * Pure grouping — exported for the Vitest suite (no querying, no hooks).
 *
 * Two passes (Review #438 Fund 3, 06.09.2026): a worker box that is BOTH a
 * member of some other host's active duo runtime AND carries its own bound
 * (and currently serving) solo runtime would, in a single left-to-right
 * pass, get processed on its own turn before the head's turn ever ran —
 * `pickServing()` on the worker's OWN group finds its own runtime "active"
 * and spins up a second, standalone stage for it, tearing the duo card in
 * two. Pass 1 claims every duo/multi-host stage's host ids FIRST, using
 * `serving.member_hosts` from whichever group discovers it (order among
 * groups doesn't matter here, only "duo before solo" does) — pass 2 then
 * only considers hosts pass 1 left untouched.
 */
export function buildStages(
  stageGroups: HostGroup[],
  live?: Record<string, RuntimeLiveStatus>
): { stages: BuiltStage[]; freeHostIds: Set<string> } {
  const seen = new Set<string>();
  const stages: BuiltStage[] = [];

  // Pass 1 — duo/multi-host stages claim their boxes first.
  for (const group of stageGroups) {
    if (seen.has(group.host.id)) continue;
    const serving = pickServing(group, live);
    if (!serving) continue;
    const memberHosts = serving.member_hosts ?? [];
    if (memberHosts.length === 0) continue; // solo runtime — pass 2 handles it
    const hostIds = [group.host.id, ...memberHosts.map((m) => m.host_id)];
    for (const id of hostIds) seen.add(id);
    stages.push({ runtime: serving, hostIds });
  }

  // Pass 2 — solo stages, only for hosts pass 1 didn't already claim as a
  // duo member (a worker whose own runtime lost this race stays hidden —
  // it belongs to the duo card above, not a card of its own).
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
  groupsByHostId,
  live,
  onOpenBoxCockpit,
}: {
  runtime: Runtime;
  hostIds: string[];
  groupsByHostId: Map<string, HostGroup>;
  live?: Record<string, RuntimeLiveStatus>;
  onOpenBoxCockpit: (members: BoxCockpitMember[], runtime: Runtime, activeHostId: string) => void;
}) {
  const devices = useDevices();
  const memberOf = new Map((runtime.member_hosts ?? []).map((m) => [m.host_id, m]));

  // Cockpit-Fund 06.09.2026 (Team-Lead-Sichtprüfung): die Connection-URL muss
  // die Slot-Runtime dieser Box zeigen (ADR-078, `is_slot=true`), NICHT den
  // Endpoint der laufenden Engine (`runtime` hier ist `pickServing()` — kann
  // an der LAN-Adresse hängen, während die Slot-Zeile die stabile Adresse
  // trägt, an der die Agenten wirklich hängen). Jedes Mitglied trägt darum
  // seine EIGENE Slot-Runtime, nicht die der Bühne.
  const members: BoxCockpitMember[] = hostIds
    .map((id) => groupsByHostId.get(id))
    .filter((g): g is HostGroup => !!g)
    .map((group) => ({
      host: group.host,
      role: group.host.id === hostIds[0] ? "head" : (memberOf.get(group.host.id)?.role ?? "worker"),
      device: devices.get(group.host.id),
      slot: pickSlot(group),
    }));

  const stageMembers: StageMember[] = members.map(({ host, role, device }) => ({ host, role, device }));
  const rtLive = live?.[runtime.slug ?? runtime.id];
  return (
    <Stage
      runtime={runtime}
      members={stageMembers}
      live={rtLive}
      onOpenCockpit={(headHostId) => onOpenBoxCockpit(members, runtime, headHostId)}
    />
  );
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

interface CockpitState {
  members: BoxCockpitMember[];
  runtime: Runtime | null;
  activeHostId: string;
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
  const groupsByHostId = useMemo(() => new Map(stageGroups.map((g) => [g.host.id, g])), [stageGroups]);
  const devices = useDevices();
  // Das Cockpit (Zahnrad, Spec §4) — ersetzt seit PR 5 das alte
  // RuntimeDetailPanel für Stage/FreeBox/AsleepBox. `onOpen` bleibt nur noch
  // für EmptyStage's "Start model" (öffnet den Rezept-Umschalter-Kontext,
  // kein Box-Cockpit).
  const [cockpit, setCockpit] = useState<CockpitState | null>(null);

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
              groupsByHostId={groupsByHostId}
              live={live}
              onOpenBoxCockpit={(members, rt, activeHostId) => setCockpit({ members, runtime: rt, activeHostId })}
            />
          ))}
          {freeGroups.map((g) => {
            const slot = pickSlot(g);
            return (
              <FreeBox
                key={g.host.id}
                host={g.host}
                slot={slot}
                device={devices.get(g.host.id)}
                onOpenCockpit={() =>
                  setCockpit({
                    members: [{ host: g.host, role: g.host.role, device: devices.get(g.host.id), slot }],
                    runtime: slot,
                    activeHostId: g.host.id,
                  })
                }
              />
            );
          })}
        </>
      )}
      {sleepingGroups.map((g) => {
        const rt = g.runtimes.find((r) => r.power_managed === true);
        if (!rt) return null;
        const slot = pickSlot(g);
        return (
          <AsleepBox
            key={g.host.id}
            host={g.host}
            runtime={rt}
            onOpenCockpit={() =>
              setCockpit({
                members: [{ host: g.host, role: g.host.role, device: devices.get(g.host.id), slot }],
                runtime: rt,
                activeHostId: g.host.id,
              })
            }
          />
        );
      })}

      <BoxCockpit
        open={cockpit != null}
        onClose={() => setCockpit(null)}
        members={cockpit?.members ?? []}
        activeHostId={cockpit?.activeHostId ?? null}
        onSwitchActive={(hostId) => setCockpit((cur) => (cur ? { ...cur, activeHostId: hostId } : cur))}
        runtime={cockpit?.runtime ?? null}
      />
    </div>
  );
}
