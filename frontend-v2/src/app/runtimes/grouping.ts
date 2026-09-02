import type { Host, Runtime, RuntimeLiveStatus } from "@/lib/types";

// openai_compatible is hostless-cloud too (spec §3) — an OpenAI-compatible
// endpoint with no bound host is a hosted API like Claude/Grok/Kimi, not an
// orphaned box runtime. A host-bound openai_compatible runtime still routes
// into its host group as normal (groupRuntimes checks rt.host first).
export const CLOUD_TYPES = new Set<string>(["cloud", "grok", "kimi", "openai_compatible"]);

const ACTIVE_STATES = new Set(["ready", "starting", "warming"]);

// Verbund-UI Phase 1b (30.08.2026) — a host that is a MEMBER (not the head)
// of some other runtime's multi-node verbund (Runtime.member_hosts). The
// runtime's own host_id (the head) is never the subject of this — that host
// gets a normal HostGroup with its own bound runtimes, same as always.
export interface WorkerMembership {
  runtimeId: string;
  runtimeDisplayName: string;
  headSlug: string | null;
  role: "head" | "worker";
  nodeRank: number;
}

export interface HostGroup {
  host: Host;
  runtimes: Runtime[];
  /** Set whenever this host appears in SOME runtime's member_hosts, whether
   *  or not it also has bound runtimes of its own. The stage decides what
   *  to do with it (Phase 1a's WorkerTile only renders when there is
   *  nothing else to show — see SlotStage.tsx). */
  workerOf?: WorkerMembership;
}

export interface RuntimeGroups {
  hosts: HostGroup[];
  cloud: Runtime[];
  unassigned: Runtime[];
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

  // Verbund-UI Phase 1b (30.08.2026) — member_hosts lookup by host UUID
  // (member_hosts.host_id is always a real UUID from runtime_hosts, unlike
  // the legacy-string-fallback matching above for the head). Only the FIRST
  // membership per host wins if a host were ever listed twice — shouldn't
  // happen (runtime_hosts.host_id has no cross-runtime uniqueness, but a
  // host is realistically a worker of one verbund at a time).
  const workerOfByHostId = new Map<string, WorkerMembership>();
  for (const rt of runtimes) {
    for (const member of rt.member_hosts ?? []) {
      if (workerOfByHostId.has(member.host_id)) continue;
      workerOfByHostId.set(member.host_id, {
        runtimeId: rt.id,
        runtimeDisplayName: rt.display_name,
        headSlug: rt.host?.slug ?? null,
        role: member.role,
        nodeRank: member.node_rank,
      });
    }
  }

  const claimedKeys = new Set<string>();
  const hostGroups = orderedHosts.map((host) => {
    // Runtime.host carries the host UUID in `id`; legacy rows may only match by slug.
    const byId = byHost.get(host.id);
    const bySlug = byHost.get(host.slug);
    if (byId) claimedKeys.add(host.id);
    if (bySlug) claimedKeys.add(host.slug);
    const workerOf = workerOfByHostId.get(host.id);
    return { runtimes: sortGroup(byId ?? bySlug ?? []), host, ...(workerOf ? { workerOf } : {}) };
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

// Lifecycle = types the backend start/stop/restart endpoints manage.
// omp + llamacpp_docker are intentionally excluded: runtime_manager's
// start/stop/restart paths don't recognize them yet and return
// "Unbekannter runtime_type" (HTTP 400) — no backend lifecycle support exists.
const LIFECYCLE_TYPES = new Set<string>([
  "vllm_docker", "lmstudio", "unsloth", "unsloth_porsche",
  // ssh_process (PR #285, DwarfStar 4): start/stop go through the same
  // runtime_manager paths incl. exclusive-memory eviction + grace phases —
  // verified against backend start_runtime (SSH_PROCESS_TYPE branches).
  "ssh_process",
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
    // Rezept-Umschalter (Vertrag 02.09.2026): jede host-gebundene Runtime
    // kann Rezepte haben — welche, sagt GET /hosts/{id}/recipes. Ob die
    // Box tatsächlich Rezepte hat, entscheidet der Umschalter selbst an der
    // Liste (hideWhenEmpty); hier gibt es nur die Vorbedingung „hat Box".
    // Früher hing das an EINEM runtime_type — das war die Hardcodierung.
    recipeSwitcher: rt.host != null,
    contextSettings: rt.runtime_type === "lmstudio",
    autostart: rt.autostart_supported === true,
  };
}

// Slot-serving states, in preference order (lower index wins on a tie).
const SERVING_STATES = ["ready", "warming", "starting"];

function servingStateWeight(rt: Runtime): number {
  const idx = SERVING_STATES.indexOf(rt.state ?? "unknown");
  return idx === -1 ? SERVING_STATES.length : idx;
}

function pickBest(runtimes: Runtime[]): Runtime {
  return [...runtimes].sort(
    (a, b) => servingStateWeight(a) - servingStateWeight(b) || a.ui_order - b.ui_order
  )[0];
}

// Picks the runtime that currently owns the host's GPU slot, for the stage's
// single-runtime display. Preference order:
//   1. A runtime mid-switch (`live[...].status === "switching"`) owns the slot
//      even if its DB `state` hasn't caught up yet — that's the whole point of
//      the switching signal (recipe switch / cold start / eviction).
//   2. A runtime in an active DB state (ready/warming/starting).
//   3. A runtime the watcher reports as `reachable === true` even without an
//      active state (e.g. hosted APIs that don't track lifecycle state).
// Ties within a tier are broken by state weight (ready < warming < starting),
// then `ui_order`. Returns null when nothing is active — the stage renders an
// OFF placeholder.
export function pickServing(
  group: HostGroup,
  live?: Record<string, RuntimeLiveStatus>
): Runtime | null {
  const liveFor = (rt: Runtime) => live?.[rt.slug ?? rt.id];

  const switching = group.runtimes.filter((rt) => liveFor(rt)?.status === "switching");
  if (switching.length > 0) return pickBest(switching);

  const activeState = group.runtimes.filter((rt) => SERVING_STATES.includes(rt.state ?? "unknown"));
  if (activeState.length > 0) return pickBest(activeState);

  const reachable = group.runtimes.filter((rt) => liveFor(rt)?.reachable === true);
  if (reachable.length > 0) return pickBest(reachable);

  return null;
}
