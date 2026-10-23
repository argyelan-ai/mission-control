/**
 * One agent state — the dot, the label and what "paused" means.
 *
 * Round-2 user test (t4 S5/S-neu-2): the agent page showed "LIVE · PAUSED ·
 * Working" at once, with a grey dot next to "Working". Three sources, each
 * on its own: the provisioning chip, the operational mode and the raw
 * heartbeat status ("working", which no dot mapping knew). This folds them
 * into one state everybody reads the same way.
 *
 * "Paused" in MC means: the agent gets no new tasks (dispatch gate
 * R-paused-no-dispatch). A task that is already running goes on — so a
 * paused agent can be working, and the label says exactly that.
 */

export type AgentDot = "online" | "warning" | "error" | "busy" | "idle" | "offline";

export interface AgentStatusView {
  dot: AgentDot;
  /** Key in the `agents` namespace (`status.*`). */
  labelKey: string;
  paused: boolean;
  working: boolean;
}

/** The heartbeat reports "working"; older paths and tests say "busy". */
export function isAgentWorking(status: string | null | undefined): boolean {
  return status === "working" || status === "busy";
}

const STATUS_VIEW: Record<string, { dot: AgentDot; labelKey: string }> = {
  online: { dot: "online", labelKey: "status.online" },
  idle: { dot: "idle", labelKey: "status.idle" },
  error: { dot: "error", labelKey: "status.error" },
  restarting: { dot: "warning", labelKey: "status.restarting" },
  provisioning: { dot: "warning", labelKey: "status.provisioning" },
  offline: { dot: "offline", labelKey: "status.offline" },
};

export function agentStatusView(agent: {
  status?: string | null;
  operational_mode?: string | null;
}): AgentStatusView {
  const working = isAgentWorking(agent.status);
  const paused = agent.operational_mode === "paused";
  if (working) {
    return { dot: "busy", labelKey: paused ? "status.pausedWorking" : "status.working", paused, working };
  }
  // Paused only replaces a healthy idle state; offline/error/restarting say
  // more about the agent right now than "no new tasks" does.
  if (paused && (agent.status === "idle" || agent.status === "online")) {
    return { dot: "warning", labelKey: "status.paused", paused, working };
  }
  const view = STATUS_VIEW[agent.status ?? ""] ?? STATUS_VIEW.offline;
  return { ...view, paused, working };
}
