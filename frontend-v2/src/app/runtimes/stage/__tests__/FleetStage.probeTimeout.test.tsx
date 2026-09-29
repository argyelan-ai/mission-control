/**
 * FleetStage — a box whose state probe timed out (GET /runtimes reports its
 * recipe runtimes as unknown / container_status "probe_timeout"). Nothing is
 * known about it, so it must not be painted as a free box ("no model ·
 * ready") nor fold into the "nothing is running" empty state.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Host } from "@/lib/types";
import type { HostGroup } from "../../grouping";
import { FleetStage } from "../FleetStage";

window.matchMedia = vi.fn().mockImplementation((query: string) => ({
  matches: true,
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

const alpha = makeHost({ slug: "alpha", display_name: "Box Alpha", ui_order: 0 });
const recipe = {
  id: "rt-1", slug: "rt-1", display_name: "Recipe One",
  runtime_type: "ssh_process", provider: "vllm",
  endpoint: "http://192.0.2.10:8000/v1", healthcheck_path: "/health",
  container_name: null, role_tags: [], supports_tools: true,
  supports_reasoning: false, supports_streaming: true,
  preferred_context_len: 8192, max_context_len: 32768,
  gpu_profile: "default", memory_notes: "", startup_notes: "",
  ui_order: 1, enabled: true,
  host: { id: "alpha", slug: "alpha", display_name: "Box Alpha" },
} as HostGroup["runtimes"][number];

function renderStage(groups: HostGroup[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <FleetStage stageGroups={groups} sleepingGroups={[]} onOpen={() => {}} />
    </QueryClientProvider>
  );
}

describe("FleetStage — probe timed out", () => {
  beforeEach(() => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({
      reachable: true, gpu_util_pct: 10, vram_used_mb: 1024, vram_total_mb: 8192, gpu_temp_c: 40,
    });
    vi.spyOn(api.hosts, "pulse").mockResolvedValue({ points: [], now_tps: null, idle_seconds: null, available: false });
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "rt-1", count: 0, agents: [] });
  });

  it("shows the box with the reason instead of 'free' or the empty state", () => {
    renderStage([{ host: alpha, runtimes: [{ ...recipe, state: "unknown", container_status: "probe_timeout" }] }]);
    expect(screen.queryByTestId("empty-stage")).not.toBeInTheDocument();
    const card = screen.getByTestId("free-box");
    expect(card).toHaveTextContent("Unknown — probe timed out");
    expect(card).not.toHaveTextContent("no model · ready");
  });

  it("a truly free box keeps its free wording", () => {
    renderStage([
      { host: alpha, runtimes: [{ ...recipe, state: "stopped", container_status: "no_process" }] },
    ]);
    // Only one box and nothing running → the empty state, no timeout wording.
    expect(screen.getByTestId("empty-stage")).toBeInTheDocument();
    expect(screen.queryByText(/probe timed out/)).not.toBeInTheDocument();
  });
});
