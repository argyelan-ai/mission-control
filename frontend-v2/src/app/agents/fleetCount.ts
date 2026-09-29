import type { Agent } from "@/lib/types";

export interface FleetCount {
  active: number;
  paused: number;
}

/** Split the roster by operational mode. A paused agent may still heartbeat
 *  (status "idle"), so status alone overstated the running fleet. */
export function fleetCount(agents: Agent[] | undefined): FleetCount {
  const list = agents ?? [];
  const paused = list.filter((a) => a.operational_mode === "paused").length;
  return { active: list.length - paused, paused };
}

type Translate = (key: string, values?: Record<string, string | number>) => string;

/** "2 active · 12 paused" — the paused part only when something is paused. */
export function fleetCountLabel(count: FleetCount, t: Translate): string {
  return count.paused > 0
    ? t("fleetCountWithPaused", { active: count.active, paused: count.paused })
    : t("fleetCountActive", { active: count.active });
}
