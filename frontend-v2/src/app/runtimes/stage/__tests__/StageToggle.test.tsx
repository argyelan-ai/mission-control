/**
 * Runtimes-Bühne v2 — Schalter `NEXT_PUBLIC_RUNTIMES_STAGE` (Spec §7).
 * Default (unset/"v1") renders today's SlotStage tree; "v2" renders the new
 * FleetStage. `NEXT_PUBLIC_*` is read at module-import time, so each case
 * resets the module registry and re-imports both the API client and the
 * page fresh — a plain re-render would still see the FIRST import's baked-in
 * flag value.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Host, Runtime, RuntimeAgentsResponse, RuntimesLiveResponse } from "@/lib/types";

vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

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

function makeHost(over: Partial<Host>): Host {
  return {
    id: over.slug ?? "h", slug: "h", display_name: "H", kind: "ssh",
    ssh_host: null, ssh_user: null, ssh_key_path: null, ssh_credential_id: null, role: null, fabric_ip: null, control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true,
    ui_order: 0, created_at: "", updated_at: "",
    ...over,
  };
}

const emptyLive: RuntimesLiveResponse = { live: {}, watcher_enabled: true, interval: 30 };

async function renderPageWithFlag(flag: string | undefined) {
  vi.resetModules();
  if (flag === undefined) vi.unstubAllEnvs();
  else vi.stubEnv("NEXT_PUBLIC_RUNTIMES_STAGE", flag);

  const { api } = await import("@/lib/api");
  const host = makeHost({ slug: "spark", display_name: "DGX Spark", enabled: true });
  const rt = makeRuntime({
    slug: "qwen", display_name: "Qwen3.8", runtime_type: "vllm_docker", state: "ready",
    host: { id: "spark", slug: "spark", display_name: "DGX Spark" },
  });
  vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [rt] });
  vi.spyOn(api.hosts, "list").mockResolvedValue([host]);
  vi.spyOn(api.hosts, "metrics").mockResolvedValue({ reachable: true, gpu_util_pct: 10, vram_used_mb: 1024, vram_total_mb: 8192, gpu_temp_c: 40 });
  vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
  vi.spyOn(api.hosts, "pulse").mockResolvedValue({ points: [], now_tps: null, idle_seconds: null, available: false });
  vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "qwen", count: 0, agents: [] } as RuntimeAgentsResponse);
  vi.spyOn(api.lmstudio, "list").mockResolvedValue({ models: [], reachable: true });
  vi.spyOn(api.lmstudio, "downloads").mockResolvedValue({ downloads: [] });
  vi.spyOn(api.runtimes, "liveStatus").mockResolvedValue(emptyLive);

  const { default: RuntimesPage } = await import("../../page");
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <RuntimesPage />
    </QueryClientProvider>
  );
}

describe("Runtimes-Bühne — v1/v2 Schalter", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  // resetModules() + two fresh dynamic imports (api client + page) per case
  // is slow under CPU contention (the full runtimes/ suite running
  // alongside this file) — the default 5s test timeout flaked there even
  // though the assertions themselves are instant once rendered.
  const SLOW = 15_000;

  it("default (flag unset) renders the v1 SlotStage tree, not FleetStage", async () => {
    await renderPageWithFlag(undefined);
    expect(await screen.findByTestId("recipe-dropdown-trigger")).toBeInTheDocument();
    expect(screen.queryByTestId("fleet-stage")).not.toBeInTheDocument();
    expect(screen.queryByTestId("stage-card")).not.toBeInTheDocument();
  }, SLOW);

  it("v2 renders FleetStage's Stage card, not the old SlotStage layout", async () => {
    await renderPageWithFlag("v2");
    expect(await screen.findByTestId("fleet-stage")).toBeInTheDocument();
    expect(await screen.findByTestId("stage-card")).toBeInTheDocument();
    // SlotStage's own root wrapper (the standalone Fleet-tab-body div outside
    // FleetStage) never renders under v2 — only fleet-stage/stage-card do.
    expect(screen.queryByTestId("worker-now-block")).not.toBeInTheDocument();
  }, SLOW);

  it("v2 drops the page subtitle and shows an icon-only add button", async () => {
    await renderPageWithFlag("v2");
    await screen.findByTestId("fleet-stage");
    expect(screen.getByTestId("add-runtime-icon")).toBeInTheDocument();
    expect(screen.queryByText("What's occupying the GPU — and who's working with it")).not.toBeInTheDocument();
  }, SLOW);
});
