/**
 * FleetStage — Duo→Solo-Übergang OHNE `prefers-reduced-motion` (PR 6
 * "Schliff"): jede Karte (Stage/FreeBox) trägt einen `motion.div`-Wrapper
 * mit `layout` + fade/translate. Split from the reduced-motion case into
 * its own file — see `FleetStage.reducedMotion.test.tsx`'s header comment
 * for why (Framer Motion's reduced-motion module-level singleton bleeds
 * across cases sharing one worker/module graph).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Host } from "@/lib/types";
import type { HostGroup } from "../../grouping";
import { FleetStage } from "../FleetStage";

window.matchMedia = vi.fn().mockImplementation((query: string) => ({
  matches: false,
  media: query,
  addListener: vi.fn(),
  removeListener: vi.fn(),
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
})) as unknown as typeof window.matchMedia;

function makeHost(over: Partial<Host>): Host {
  return {
    id: over.slug ?? "h", slug: "h", display_name: "H", kind: "ssh",
    ssh_host: null, ssh_user: null, ssh_key_path: null, ssh_credential_id: null, role: null, fabric_ip: null, control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true,
    ui_order: 0, created_at: "", updated_at: "",
    ...over,
  } as Host;
}

const servingHost = makeHost({ slug: "spark", display_name: "DGX Spark", ui_order: 0 });
const freeHostB = makeHost({ slug: "free-b", display_name: "Free B", ui_order: 1 });
const baseRuntime = {
  id: "rt-1", slug: "rt-1", display_name: "Serving Model",
  runtime_type: "vllm_docker", provider: "vllm",
  endpoint: "http://192.0.2.10:8001/v1", healthcheck_path: "/health",
  container_name: null, role_tags: [], supports_tools: true,
  supports_reasoning: false, supports_streaming: true,
  preferred_context_len: 8192, max_context_len: 32768,
  gpu_profile: "default", memory_notes: "", startup_notes: "",
  ui_order: 0, enabled: true, state: "ready",
  host: { id: "spark", slug: "spark", display_name: "DGX Spark" },
} as HostGroup["runtimes"][number];

// Real duo→solo, same runtime.id ("rt-1") throughout — the head-box Stage
// card's key (Spec, PR 6 "Kartenkeys stabil"). Duo: `member_hosts` claims
// free-b into the SAME stage card (buildStages pass 1) — no separate
// FreeBox for it. Solo: `member_hosts` drops (the verbund ended), so
// free-b — same host, same key — reappears as its OWN FreeBox card, exactly
// the "Mitglied-Zeile blendet aus, FreeBox erscheint darunter" transition.
const duoGroups: { stageGroups: HostGroup[]; sleepingGroups: HostGroup[] } = {
  stageGroups: [
    { host: servingHost, runtimes: [{ ...baseRuntime, member_hosts: [{ host_id: "free-b", slug: "free-b", display_name: "Free B", role: "worker", node_rank: 1 }] }] },
    { host: freeHostB, runtimes: [] },
  ],
  sleepingGroups: [],
};
const soloGroups: { stageGroups: HostGroup[]; sleepingGroups: HostGroup[] } = {
  stageGroups: [
    { host: servingHost, runtimes: [{ ...baseRuntime, member_hosts: [] }] },
    { host: freeHostB, runtimes: [] },
  ],
  sleepingGroups: [],
};

function renderFleetStage(groups: { stageGroups: HostGroup[]; sleepingGroups: HostGroup[] }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <FleetStage stageGroups={groups.stageGroups} sleepingGroups={groups.sleepingGroups} onOpen={() => {}} />
    </QueryClientProvider>
  );
}

describe("FleetStage — motion (no reduced-motion)", () => {
  beforeEach(() => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({
      reachable: true, gpu_util_pct: 10, vram_used_mb: 1024, vram_total_mb: 8192, gpu_temp_c: 40,
    });
    vi.spyOn(api.hosts, "pulse").mockResolvedValue({ points: [], now_tps: null, idle_seconds: null, available: false });
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "rt-1", count: 0, agents: [] });
  });

  it("motion wrappers carry Framer Motion's computed animation style", async () => {
    renderFleetStage(soloGroups);

    // Framer Motion applies its computed style (opacity/transform) onto the
    // motion.div wrapper once `initial`/`animate` are non-`false` — the
    // positive-case mirror of the reduced-motion test's `toBeNull()`.
    const stageCard = await screen.findByTestId("stage-card");
    expect(stageCard.parentElement?.getAttribute("style")).toContain("opacity");

    const freeBox = await screen.findByTestId("free-box");
    expect(freeBox.parentElement?.getAttribute("style")).toContain("opacity");
  });

  it("a duo→solo transition keeps the stage card's key stable and mounts a new free box for the released host", async () => {
    const { rerender } = renderFleetStage(duoGroups);
    // Duo: free-b is claimed as a member — one 2-member stage card, no
    // FreeBox anywhere yet.
    const stageCardBefore = await screen.findByTestId("stage-card");
    expect(stageCardBefore.getAttribute("data-status")).toBe("serving");
    expect((await screen.findByTestId("stage-members")).getAttribute("data-duo")).toBe("true");
    expect(screen.queryByTestId("free-box")).not.toBeInTheDocument();
    const stageWrapperBefore = stageCardBefore.parentElement;

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    rerender(
      <QueryClientProvider client={qc}>
        <FleetStage stageGroups={soloGroups.stageGroups} sleepingGroups={soloGroups.sleepingGroups} onOpen={() => {}} />
      </QueryClientProvider>
    );

    // Solo: the stage card survives under the SAME key (runtime.id, keyed in
    // FleetStage.tsx) → React (and Framer Motion's `layout` prop) reuse its
    // DOM node rather than tearing it down and remounting a new one. free-b,
    // no longer a member, appears as its own NEW FreeBox card.
    const stageCardAfter = await screen.findByTestId("stage-card");
    expect(stageCardAfter.parentElement).toBe(stageWrapperBefore);
    expect((await screen.findByTestId("stage-members")).getAttribute("data-duo")).toBe("false");
    expect(await screen.findByTestId("free-box")).toBeInTheDocument();
  });
});
