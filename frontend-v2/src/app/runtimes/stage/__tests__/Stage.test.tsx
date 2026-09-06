/**
 * Stage — Kurztitel (Live-Sichtprüfung 06.09.2026) + Ecke rechts oben zeigt
 * den Zustand statt einer erfundenen Laufzeit (HONESTY RULE).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Stage } from "../Stage";
import { api } from "@/lib/api";
import type { Host, Runtime } from "@/lib/types";

const mockStore = vi.hoisted(() => ({
  state: { currentUser: { id: "u1", email: "a@b.c", name: "Admin", role: "admin" } as { id: string; email: string; name: string; role: string } | null },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector: (s: typeof mockStore.state) => unknown) => selector(mockStore.state),
}));

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function makeHost(over: Partial<Host>): Host {
  return {
    id: over.slug ?? "h", slug: "h", display_name: "H", kind: "ssh",
    ssh_host: null, ssh_user: null, ssh_key_path: null, ssh_credential_id: null, role: null, fabric_ip: null, control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true,
    ui_order: 0, created_at: "", updated_at: "",
    ...over,
  };
}

function makeRuntime(over: Partial<Runtime>): Runtime {
  return {
    id: over.slug ?? "rt", slug: "rt", display_name: "RT",
    runtime_type: "vllm_docker", provider: "vllm",
    endpoint: "http://192.0.2.10:8001/v1", healthcheck_path: "/health",
    container_name: null, role_tags: [], supports_tools: true,
    supports_reasoning: false, supports_streaming: true,
    preferred_context_len: 8192, max_context_len: 32768,
    gpu_profile: "default", memory_notes: "", startup_notes: "",
    ui_order: 0, enabled: true, state: "ready",
    ...over,
  };
}

describe("Stage — title + corner", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.hosts, "pulse").mockResolvedValue({ points: [], now_tps: null, idle_seconds: null, available: false });
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({ reachable: true, gpu_util_pct: 10, vram_used_mb: 1024, vram_total_mb: 8192, gpu_temp_c: 40 });
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "rt", count: 0, agents: [] });
  });

  it("shows the short title, keeps the full name as a tooltip", async () => {
    const rt = makeRuntime({
      display_name: "Qwen3.8 Flash Next NVFP4 MTP3 — vLLM (2× Spark)",
      host: { id: "spark", slug: "spark", display_name: "DGX Spark" },
    });
    renderWithQuery(<Stage runtime={rt} members={[{ host: makeHost({ slug: "spark", display_name: "DGX Spark" }), role: "head" }]} onOpenCockpit={() => {}} />);

    const title = await screen.findByTitle("Qwen3.8 Flash Next NVFP4 MTP3 — vLLM (2× Spark)");
    expect(title).toHaveTextContent("Qwen3.8 Flash Next");
    expect(title).not.toHaveTextContent("NVFP4");
    expect(title).not.toHaveTextContent("Spark");
  });

  it("corner shows 'serving' for a healthy runtime, not a fabricated uptime", async () => {
    const rt = makeRuntime({ display_name: "Qwen3.8 27B", host: { id: "spark", slug: "spark", display_name: "DGX Spark" } });
    renderWithQuery(<Stage runtime={rt} members={[{ host: makeHost({ slug: "spark", display_name: "DGX Spark" }), role: "head" }]} onOpenCockpit={() => {}} />);
    expect(await screen.findByText("serving")).toBeInTheDocument();
    expect(screen.queryByText(/up \d/)).not.toBeInTheDocument();
  });

  it("corner shows 'unreachable' for a failed runtime", async () => {
    const rt = makeRuntime({
      display_name: "Qwen3.8 27B", state: "failed",
      host: { id: "spark", slug: "spark", display_name: "DGX Spark" },
    });
    renderWithQuery(<Stage runtime={rt} members={[{ host: makeHost({ slug: "spark", display_name: "DGX Spark" }), role: "head" }]} onOpenCockpit={() => {}} />);
    expect(await screen.findByText("unreachable")).toBeInTheDocument();
  });

  it("corner shows 'switching' while a recipe switch is in progress", async () => {
    const rt = makeRuntime({ display_name: "Qwen3.8 27B", host: { id: "spark", slug: "spark", display_name: "DGX Spark" } });
    renderWithQuery(
      <Stage
        runtime={rt}
        members={[{ host: makeHost({ slug: "spark", display_name: "DGX Spark" }), role: "head" }]}
        live={{ reachable: false, served_model: null, latency_ms: null, last_probe_at: "", consecutive_failures: 0, drift: false, status: "switching", phase: "launching" }}
        onOpenCockpit={() => {}}
      />
    );
    expect(await screen.findByText("switching")).toBeInTheDocument();
  });
});

describe("Stage — Agents KPI reads the box's slot runtime (Team-Lead-Fund 06.09.2026)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.hosts, "pulse").mockResolvedValue({ points: [], now_tps: null, idle_seconds: null, available: false });
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({ reachable: true, gpu_util_pct: 10, vram_used_mb: 1024, vram_total_mb: 8192, gpu_temp_c: 40 });
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
  });

  it("counts agents bound to the head box's SLOT runtime (ADR-078), not the recipe runtime", async () => {
    const recipeRuntime = makeRuntime({ slug: "qwen38-flash-next", host: { id: "spark", slug: "spark", display_name: "DGX Spark" } });
    const slotRuntime = makeRuntime({ slug: "dgx-spark-slot", is_slot: true });

    const agentsSpy = vi.spyOn(api.runtimes.db, "agents").mockImplementation((slug: string) =>
      Promise.resolve(
        slug === "dgx-spark-slot"
          ? { runtime_slug: slug, count: 3, agents: [
              { id: "a1", name: "Alpha", agent_runtime: "cli-bridge" },
              { id: "a2", name: "Beta", agent_runtime: "cli-bridge" },
              { id: "a3", name: "Gamma", agent_runtime: "cli-bridge" },
            ] }
          : { runtime_slug: slug, count: 0, agents: [] }
      )
    );

    renderWithQuery(
      <Stage
        runtime={recipeRuntime}
        members={[{ host: makeHost({ slug: "spark", display_name: "DGX Spark" }), role: "head", slot: slotRuntime }]}
        onOpenCockpit={() => {}}
      />
    );

    expect(await screen.findByText("3")).toBeInTheDocument();
    expect(agentsSpy).toHaveBeenCalledWith("dgx-spark-slot");
    expect(agentsSpy).not.toHaveBeenCalledWith("qwen38-flash-next");
  });

  it("falls back to the recipe runtime when the head box has no slot runtime yet", async () => {
    const recipeRuntime = makeRuntime({ slug: "qwen38-flash-next", host: { id: "spark", slug: "spark", display_name: "DGX Spark" } });
    const agentsSpy = vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "qwen38-flash-next", count: 1, agents: [] });

    renderWithQuery(
      <Stage
        runtime={recipeRuntime}
        members={[{ host: makeHost({ slug: "spark", display_name: "DGX Spark" }), role: "head" }]}
        onOpenCockpit={() => {}}
      />
    );

    await screen.findByText("1");
    expect(agentsSpy).toHaveBeenCalledWith("qwen38-flash-next");
  });
});
