import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Runtime, Host, RuntimeAgentsResponse, RuntimesLiveResponse } from "@/lib/types";

// AppShell (auth guard, Sidebar, TopBar, CommandPalette, VoiceProvider, …) is
// unrelated to the page's own stage/placeholder/panel logic under test —
// mocking it out keeps this focused instead of pulling in the whole shell's
// auth/localStorage/SSE dependency chain (same pattern as
// src/app/sessions/__tests__/SessionsPage.test.tsx).
vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

import RuntimesPage from "../page";

// Fixtures copied from grouping.test.ts / slot-stage.test.tsx (this repo's
// established pattern).
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
    ssh_host: null, ssh_user: null, ssh_key_path: null, ssh_credential_id: null, control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true,
    ui_order: 0, created_at: "", updated_at: "",
    ...over,
  };
}

function agentsResponse(slug: string, agents: RuntimeAgentsResponse["agents"]): RuntimeAgentsResponse {
  return { runtime_slug: slug, count: agents.length, agents };
}

const emptyLive: RuntimesLiveResponse = { live: {}, watcher_enabled: true, interval: 30 };

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={qc}>
      <RuntimesPage />
    </QueryClientProvider>
  );
  return { ...utils, qc };
}

describe("RuntimesPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({
      reachable: true, gpu_util_pct: 10, vram_used_mb: 1024, vram_total_mb: 8192, gpu_temp_c: 40,
    });
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue(agentsResponse("rt", []));
    vi.spyOn(api.lmstudio, "list").mockResolvedValue({ models: [], reachable: true });
    vi.spyOn(api.lmstudio, "downloads").mockResolvedValue({ downloads: [] });
    vi.spyOn(api.runtimes, "liveStatus").mockResolvedValue(emptyLive);
    vi.spyOn(api.runtimes.sparkrun, "listRecipes").mockResolvedValue({ recipes: [] });
    vi.spyOn(api.runtimes.sparkrun, "currentRecipe").mockResolvedValue({
      slug: "rt", current_recipe: null, sparkrun_managed: false,
    });
  });

  it("(a) an omp runtime on an enabled host is visible as a ready row", async () => {
    const host = makeHost({ slug: "spark", display_name: "DGX Spark", enabled: true });
    const omp = makeRuntime({
      slug: "omp1", display_name: "OMP Runtime", runtime_type: "omp", state: "stopped",
      host: { id: "spark", slug: "spark", display_name: "DGX Spark" },
    });
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [omp] });
    vi.spyOn(api.hosts, "list").mockResolvedValue([host]);

    renderPage();

    // Visible twice by design: as a disabled engine in the stage's unified
    // switch dropdown, and as a register row on the Infrastructure tab.
    await act(async () => { (await screen.findByTestId("recipe-dropdown-trigger")).click(); });
    expect(await screen.findByTestId("switch-engine-omp1")).toBeInTheDocument();
    await act(async () => { screen.getByTestId("page-tab-infra").click(); });
    expect(await screen.findByTestId("runtime-register-row-omp1")).toBeInTheDocument();
  });

  it("(b) a host with zero runtimes still renders a placeholder stage", async () => {
    // Page-level empty state (isEmpty) fires only when there are zero
    // runtimes anywhere — give the fixture one elsewhere so this exercises
    // the per-host placeholder, not the whole-page empty state.
    const emptyHost = makeHost({ slug: "empty-box", display_name: "Empty Box", enabled: true, ui_order: 0 });
    const otherHost = makeHost({ slug: "spark", display_name: "DGX Spark", enabled: true, ui_order: 1 });
    const elsewhere = makeRuntime({
      slug: "rt-elsewhere", display_name: "Elsewhere", runtime_type: "vllm_docker", state: "ready",
      host: { id: "spark", slug: "spark", display_name: "DGX Spark" },
    });
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [elsewhere] });
    vi.spyOn(api.hosts, "list").mockResolvedValue([emptyHost, otherHost]);

    renderPage();

    expect(await screen.findByText("Empty Box")).toBeInTheDocument();
    expect(await screen.findByText("No model set up")).toBeInTheDocument();
  });

  it("(c) a power-managed host whose runtimes lack the power_managed flag still renders (never vanishes)", async () => {
    const host = makeHost({ slug: "porsche", display_name: "Porsche Box", enabled: true, power_managed: true });
    // Bound runtime exists, but none of them carry power_managed: true — the
    // old SleepingHostLine silently returned null for this exact case.
    const bound = makeRuntime({
      slug: "unsloth1", display_name: "Unsloth Porsche", runtime_type: "unsloth_porsche", state: "stopped",
      host: { id: "porsche", slug: "porsche", display_name: "Porsche Box" },
    });
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [bound] });
    vi.spyOn(api.hosts, "list").mockResolvedValue([host]);

    renderPage();

    expect(await screen.findByText("Porsche Box")).toBeInTheDocument();
  });

  it("(d) page tabs switch the lower area; openModelsTab lands on the Models tab", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] });
    vi.spyOn(api.hosts, "list").mockResolvedValue([]);

    renderPage();

    // Tabs render even for an empty fleet — Fleet is the default.
    const fleetTab = await screen.findByTestId("page-tab-fleet");
    expect(fleetTab).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByTestId("models-tab-download")).not.toBeInTheDocument();

    // Clicking the Models tab reveals the models content (inner sub-tabs).
    await act(async () => {
      screen.getByTestId("page-tab-models").click();
    });
    expect(await screen.findByTestId("models-tab-download")).toBeInTheDocument();

    // Back to Fleet, then the openModelsTab event (SlotStage "+ Model") must
    // switch the page tab on its own.
    await act(async () => {
      screen.getByTestId("page-tab-fleet").click();
    });
    expect(screen.queryByTestId("models-tab-download")).not.toBeInTheDocument();
    const { openModelsTab } = await import("../modelsTab");
    await act(async () => {
      openModelsTab("download");
    });
    await waitFor(() => expect(screen.getByTestId("page-tab-models")).toHaveAttribute("aria-selected", "true"));
    expect(await screen.findByTestId("models-tab-download")).toBeInTheDocument();
  });

  it("(e) the detail panel derives fresh runtime data instead of a stale snapshot", async () => {
    let runtimes: Runtime[] = [
      makeRuntime({
        slug: "opus", display_name: "Claude Opus", runtime_type: "cloud", state: "unknown",
        model_identifier: "model-a", host: null,
      }),
    ];
    vi.spyOn(api.runtimes, "list").mockImplementation(() => Promise.resolve({ runtimes }));
    vi.spyOn(api.hosts, "list").mockResolvedValue([]);
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue(
      agentsResponse("opus", [{ id: "a1", name: "Boss", agent_runtime: "host" }])
    );

    const { qc } = renderPage();

    // Cloud usage now lives on its own tab.
    await act(async () => {
      (await screen.findByTestId("page-tab-cloud")).click();
    });
    const row = await screen.findByTestId("cloud-usage-row-opus");
    await act(async () => {
      row.click();
    });

    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("model-a")).toBeInTheDocument();

    runtimes = [{ ...runtimes[0], model_identifier: "model-b" }];
    await act(async () => {
      await qc.invalidateQueries({ queryKey: ["runtimes"] });
    });

    expect(await within(dialog).findByText("model-b")).toBeInTheDocument();
    expect(within(dialog).queryByText("model-a")).not.toBeInTheDocument();
  });
});
