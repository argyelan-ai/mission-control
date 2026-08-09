import type { Host, Runtime, RuntimeLiveStatus } from "@/lib/types";

export const CLOUD_TYPES = new Set<string>(["cloud", "grok", "kimi"]);

const ACTIVE_STATES = new Set(["ready", "starting", "warming"]);

export interface HostGroup {
  host: Host;
  runtimes: Runtime[];
}

export interface RuntimeGroups {
  hosts: HostGroup[];
  cloud: Runtime[];
  unassigned: Runtime[];
}

export interface StateSummary {
  active: number;
  stopped: number;
  failed: number;
}

function stateWeight(rt: Runtime): number {
  const s = rt.state ?? "unknown";
  if (ACTIVE_STATES.has(s)) return 0;
  if (s === "failed") return 1;
  return 2;
}

function sortGroup(runtimes: Runtime[]): Runtime[] {
  return [...runtimes].sort(
    (a, b) => stateWeight(a) - stateWeight(b) || a.ui_order - b.ui_order || a.display_name.localeCompare(b.display_name)
  );
}

export function groupRuntimes(runtimes: Runtime[], hosts: Host[]): RuntimeGroups {
  const byHost = new Map<string, Runtime[]>();
  const cloud: Runtime[] = [];
  const unassigned: Runtime[] = [];

  for (const rt of runtimes) {
    const ref = rt.host;
    if (ref) {
      const key = ref.id;
      byHost.set(key, [...(byHost.get(key) ?? []), rt]);
    } else if (CLOUD_TYPES.has(rt.runtime_type)) {
      cloud.push(rt);
    } else {
      unassigned.push(rt);
    }
  }

  const orderedHosts = [...hosts].sort(
    (a, b) => a.ui_order - b.ui_order || a.slug.localeCompare(b.slug)
  );

  const claimedKeys = new Set<string>();
  const hostGroups = orderedHosts.map((host) => {
    // Runtime.host carries the host UUID in `id`; legacy rows may only match by slug.
    const byId = byHost.get(host.id);
    const bySlug = byHost.get(host.slug);
    if (byId) claimedKeys.add(host.id);
    if (bySlug) claimedKeys.add(host.slug);
    return { runtimes: sortGroup(byId ?? bySlug ?? []), host };
  });

  // Host-bound runtimes whose host_id doesn't resolve against `hosts` (still
  // loading, hosts fetch failed, or an orphaned host_id) must not silently
  // vanish — bucket the leftovers into unassigned instead of dropping them.
  for (const [key, rts] of byHost) {
    if (!claimedKeys.has(key)) unassigned.push(...rts);
  }

  return {
    hosts: hostGroups,
    cloud: sortGroup(cloud),
    unassigned: sortGroup(unassigned),
  };
}

export function summarizeStates(
  runtimes: Runtime[],
  live?: Record<string, RuntimeLiveStatus>
): StateSummary {
  let active = 0, stopped = 0, failed = 0;
  for (const rt of runtimes) {
    const s = rt.state ?? "unknown";
    // Mirrors RuntimeListCard: an active-state runtime whose watcher reports
    // reachable===false counts as failed, not active — otherwise the summary
    // line and the cards disagree about what "failed" means.
    const liveStatus = live?.[rt.slug ?? rt.id];
    const unreachable = liveStatus != null && liveStatus.reachable === false;
    if (s === "failed" || (unreachable && ACTIVE_STATES.has(s))) failed += 1;
    else if (ACTIVE_STATES.has(s)) active += 1;
    // "Stopped" only makes sense for runtimes the operator can start/stop.
    // Hosted APIs (cloud/grok/kimi) idle in state stopped/unknown by design —
    // counting them would inflate the number into a phantom fleet problem.
    else if (LIFECYCLE_TYPES.has(rt.runtime_type)) stopped += 1;
  }
  return { active, stopped, failed };
}

// Lifecycle = types the backend start/stop/restart endpoints manage.
// omp + llamacpp_docker are intentionally excluded: runtime_manager's
// start/stop/restart paths don't recognize them yet and return
// "Unbekannter runtime_type" (HTTP 400) — no backend lifecycle support exists.
const LIFECYCLE_TYPES = new Set<string>([
  "vllm_docker", "lmstudio", "unsloth", "unsloth_porsche",
]);

// Copied from the old page.tsx `isProbeable` list — do not widen without backend support.
const PROBEABLE_TYPES = new Set<string>([
  "vllm_docker", "lmstudio", "openai_compatible", "unsloth", "unsloth_porsche",
]);

export interface PanelCapabilities {
  lifecycle: boolean;
  wake: boolean;
  probe: boolean;
  modelEditor: boolean;
  recipeSwitcher: boolean;
  contextSettings: boolean;
  autostart: boolean;
}

export function panelCapabilities(rt: Runtime): PanelCapabilities {
  const probe = PROBEABLE_TYPES.has(rt.runtime_type);
  return {
    lifecycle: LIFECYCLE_TYPES.has(rt.runtime_type),
    wake: rt.power_managed === true,
    probe,
    // Non-probeable runtimes have no watcher-driven live model; their static
    // DB value is the only source of truth and needs the manual editor.
    modelEditor: !probe,
    recipeSwitcher: rt.runtime_type === "vllm_docker",
    contextSettings: rt.runtime_type === "lmstudio",
    autostart: rt.autostart_supported === true,
  };
}
