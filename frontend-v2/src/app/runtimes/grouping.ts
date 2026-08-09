import type { Host, Runtime } from "@/lib/types";

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

  return {
    hosts: orderedHosts.map((host) => ({
      // Runtime.host carries the host UUID in `id`; legacy rows may only match by slug.
      runtimes: sortGroup(byHost.get(host.id) ?? byHost.get(host.slug) ?? []),
      host,
    })),
    cloud: sortGroup(cloud),
    unassigned: sortGroup(unassigned),
  };
}

export function summarizeStates(runtimes: Runtime[]): StateSummary {
  let active = 0, stopped = 0, failed = 0;
  for (const rt of runtimes) {
    const s = rt.state ?? "unknown";
    if (ACTIVE_STATES.has(s)) active += 1;
    else if (s === "failed") failed += 1;
    else stopped += 1;
  }
  return { active, stopped, failed };
}

// Lifecycle = types the backend start/stop/restart endpoints manage.
// Mirrors the old page: it only ever rendered vllm_docker + lmstudio cards;
// unsloth/omp/llamacpp go through the same runtime_manager paths.
const LIFECYCLE_TYPES = new Set<string>([
  "vllm_docker", "lmstudio", "unsloth", "unsloth_porsche", "omp", "llamacpp_docker",
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
