/**
 * FleetStage — Duo→Solo-Übergang unter `prefers-reduced-motion: reduce`
 * (PR 6 "Schliff"): `cardMotionProps` in `FleetStage.tsx`/`Stage.tsx` passes
 * `{}` when `useReducedMotion()` is true, so the `motion.div` card wrapper
 * gets no `initial`/`animate`/`exit`/`layout` at all — no animation runs.
 *
 * Split into its OWN file rather than sharing one with the non-reduced case
 * (`FleetStage.motion.test.tsx`): Framer Motion's `useReducedMotion()` reads
 * `window.matchMedia(...)` exactly ONCE per process into a module-level
 * singleton (`framer-motion/.../reduced-motion/state.mjs`) and never
 * re-checks it — `vi.resetModules()` does not reliably evict Vite's
 * optimized-dep cache for `node_modules` packages, so two states in one
 * file/worker bleed into each other (confirmed empirically: the second case
 * saw the first case's cached value). A fresh test FILE gets a fresh worker
 * and module graph, which sidesteps the singleton cleanly.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Host } from "@/lib/types";
import type { HostGroup } from "../../grouping";
import { FleetStage } from "../FleetStage";

window.matchMedia = vi.fn().mockImplementation((query: string) => ({
  matches: query.includes("prefers-reduced-motion"),
  media: query,
  // Framer Motion's reduced-motion singleton subscribes via the OLD
  // MediaQueryList API — addListener/removeListener, not
  // addEventListener/removeEventListener.
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

function makeGroups(): { stageGroups: HostGroup[]; sleepingGroups: HostGroup[] } {
  const servingHost = makeHost({ slug: "spark", display_name: "DGX Spark", ui_order: 0 });
  const freeHost = makeHost({ slug: "free-a", display_name: "Free A", ui_order: 1 });
  const serving = {
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
  return {
    stageGroups: [
      { host: servingHost, runtimes: [serving] },
      { host: freeHost, runtimes: [] },
    ],
    sleepingGroups: [],
  };
}

function renderFleetStage() {
  const { stageGroups, sleepingGroups } = makeGroups();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <FleetStage stageGroups={stageGroups} sleepingGroups={sleepingGroups} onOpen={() => {}} />
    </QueryClientProvider>
  );
}

describe("FleetStage — prefers-reduced-motion: reduce", () => {
  beforeEach(() => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({
      reachable: true, gpu_util_pct: 10, vram_used_mb: 1024, vram_total_mb: 8192, gpu_temp_c: 40,
    });
    vi.spyOn(api.hosts, "pulse").mockResolvedValue({ points: [], now_tps: null, idle_seconds: null, available: false });
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "rt-1", count: 0, agents: [] });
  });

  it("cards render with no animation inline style on their motion wrapper", async () => {
    renderFleetStage();

    // The card's OWN inline styles (background/border on `stage-card`
    // itself) are unaffected by this — this checks the `motion.div` WRAPPER
    // one level up, which gets no `style` attribute at all when
    // `cardMotionProps` is `{}` (no initial/animate/exit/layout passed).
    const stageCard = await screen.findByTestId("stage-card");
    expect(stageCard.parentElement?.getAttribute("style")).toBeNull();

    const freeBox = await screen.findByTestId("free-box");
    expect(freeBox.parentElement?.getAttribute("style")).toBeNull();
  });
});
