import type { SystemMetrics } from "@/lib/types";
import { fleetCountLabel } from "@/app/agents/fleetCount";

type Translate = (key: string, values?: Record<string, string | number>) => string;

/** Sidebar badge for /agents: active/total, with the same "2 active · 12 paused"
 *  label the /agents header shows as tooltip. A paused agent still heartbeats
 *  ("idle"), so `online` overstated the running fleet. `t` is the "agents"
 *  namespace. */
export function agentsBadge(
  agents: SystemMetrics["agents"],
  t: Translate,
): { text: string; title?: string } {
  if (agents.active === undefined || agents.paused === undefined) {
    // Older backend without the split — keep the previous behaviour.
    return { text: `${agents.online}/${agents.total}` };
  }
  return {
    text: `${agents.active}/${agents.total}`,
    title: fleetCountLabel({ active: agents.active, paused: agents.paused }, t),
  };
}
