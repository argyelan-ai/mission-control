import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SlotStage } from "../SlotStage";
import { api } from "@/lib/api";
import type { Device, Runtime, Host, RuntimeLiveStatus } from "@/lib/types";
import type { HostGroup } from "../grouping";

// Der echte zustand-Store schreibt über die persist-Middleware in
// localStorage — im jsdom reicht ein Selektor-Mock (SlotStage liest nur
// currentUser.role, um den Geräte-Schalter zu sperren).
const mockStore = vi.hoisted(() => ({
  state: { currentUser: { id: "u1", email: "a@b.c", name: "Admin", role: "admin" } as { id: string; email: string; name: string; role: string } | null },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector: (s: typeof mockStore.state) => unknown) => selector(mockStore.state),
}));

/** Eine Zeile aus /nodes/devices — nur je gekoppelte Boxen stehen dort. */
function makeDevice(over: Partial<Device> = {}): Device {
  return {
    host_id: "spark",
    slug: "spark",
    display_name: "GPU-Box",
    has_agent: true,
    desired_state: { gpu_mode: "eco" },
    device_state: {
      gpu_mode: "eco", gpu_clock_mhz: 1989, gpu_power_w: 33, gpu_temp_c: 63,
      min_free_kbytes: 5242880, oom_guard: "active", latency_tune: true,
      mtu: { iface: "enP7s7", value: 9000 }, applied_at: "2026-09-01T00:12:00Z", last_error: null,
    },
    device_state_updated_at: "2026-09-01T00:12:00Z",
    agent_last_seen_at: "2026-09-01T00:12:00Z",
    status: "green",
    reason: "in_sync",
    diff: [],
    last_error: null,
    age_s: 4,
    ...over,
  };
}

// Fixtures copied from grouping.test.ts (this repo's established pattern —
// RuntimeDetailPanel.test.tsx does the same rather than importing another
// test file's locals).
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

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const noopSizeGb = () => undefined;

/**
 * Wartet, bis die Geräte-Abfrage wirklich durch ist.
 *
 * Ohne das prüft ein "kein Schalter da"-Test nichts: die Abfrage ist zum
 * Zeitpunkt der Behauptung noch offen, der Schalter fehlt also aus dem
 * falschen Grund. (Genau so ist eine Sabotage-Probe hier durchgerutscht.)
 */
async function devicesSettled(spy: { mock: { results: unknown[] } }) {
  await waitFor(() => expect(spy.mock.results.length).toBeGreaterThan(0));
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("SlotStage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({
      reachable: true, gpu_util_pct: 42, vram_used_mb: 4096, vram_total_mb: 8192, gpu_temp_c: 55,
    });
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
      runtime_slug: "rt", count: 0, agents: [],
    });
    vi.spyOn(api.runtimes.sparkrun, "listRecipes").mockResolvedValue({
      recipes: [
        { name: "qwen-general", model: "qwen3.6", registry: "official", tp: 1, nodes: 1, solo_capable: true },
        { name: "laguna-s21", model: "laguna", registry: "official", tp: 1, nodes: 1, solo_capable: true },
      ],
    });
    vi.spyOn(api.runtimes.sparkrun, "currentRecipe").mockResolvedValue({
      slug: "rt", current_recipe: "qwen-general", sparkrun_managed: true,
    });
    // Standard: keine gekoppelte Box — wer den Schalter testet, überschreibt.
    vi.spyOn(api.nodes, "devices").mockResolvedValue([]);
    mockStore.state.currentUser = { id: "u1", email: "a@b.c", name: "Admin", role: "admin" };
  });

  it("serving stage renders model+ctx+latency and never renders tok/s", async () => {
    const serving = makeRuntime({
      slug: "rt", display_name: "DeepSeek V4 Flash", runtime_type: "vllm_docker",
      state: "ready", model_identifier: "deepseek-v4-flash-0731-spark", max_context_len: 262144,
    });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };
    const live: Record<string, RuntimeLiveStatus> = {
      rt: {
        reachable: true, served_model: "deepseek-v4-flash-0731-spark", served_context_len: 262144,
        latency_ms: 42, last_probe_at: "", consecutive_failures: 0, drift: false,
      },
    };

    renderWithQuery(
      <SlotStage group={group} live={live} sizeGb={() => 180} onOpen={() => {}} />
    );

    expect(await screen.findByText("DeepSeek V4 Flash")).toBeInTheDocument();
    expect(screen.getByText("262k")).toBeInTheDocument();
    expect(screen.getByText("42 ms")).toBeInTheDocument();
    expect(screen.queryByText(/tok\/s/i)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/tok\/s/i);
  });

  // ── P2: Rollen-Chip in der Kopfzeile ──────────────────────────────────────

  it("P2: serving stage shows a HEAD chip in the header when the host has a role", async () => {
    const serving = makeRuntime({ slug: "rt", display_name: "Model A", runtime_type: "vllm_docker", state: "ready" });
    const host = makeHost({ slug: "box-a", display_name: "Box A", role: "head" });
    renderWithQuery(<SlotStage group={{ host, runtimes: [serving] }} sizeGb={noopSizeGb} onOpen={() => {}} />);

    const chip = await screen.findByTestId("host-role-chip");
    expect(chip).toHaveAttribute("data-role", "head");
    expect(chip).toHaveTextContent("Head");
  });

  it("P2: without a role the header renders no chip at all (sabotage: role=null)", async () => {
    const serving = makeRuntime({ slug: "rt", display_name: "Model A", runtime_type: "vllm_docker", state: "ready" });
    const host = makeHost({ slug: "box-a", display_name: "Box A", role: null });
    renderWithQuery(<SlotStage group={{ host, runtimes: [serving] }} sizeGb={noopSizeGb} onOpen={() => {}} />);

    await screen.findByText("Model A");
    expect(screen.queryByTestId("host-role-chip")).toBeNull();
  });

  it("P2: the empty-slot placeholder and the worker tile carry the chip too", async () => {
    // Leerer Slot (ssh-Host ohne Runtime) mit Rolle worker
    const { unmount } = renderWithQuery(
      <SlotStage group={{ host: makeHost({ slug: "box-b", display_name: "Box B", role: "worker" }), runtimes: [] }} sizeGb={noopSizeGb} onOpen={() => {}} />,
    );
    expect(await screen.findByTestId("host-role-chip")).toHaveTextContent("Worker");
    unmount();

    // Verbund-Knoten (agent-Host ohne Runtime) mit Rolle worker
    renderWithQuery(
      <SlotStage group={{ host: makeHost({ slug: "box-c", display_name: "Box C", kind: "agent", role: "worker" }), runtimes: [] }} sizeGb={noopSizeGb} onOpen={() => {}} />,
    );
    expect(await screen.findByTestId("host-role-chip")).toHaveAttribute("data-role", "worker");
  });

  it("switching live status renders a phase indicator", async () => {
    const serving = makeRuntime({ slug: "rt", display_name: "DeepSeek V4 Flash", runtime_type: "vllm_docker", state: "ready" });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };
    const live: Record<string, RuntimeLiveStatus> = {
      rt: {
        reachable: false, served_model: null, latency_ms: null,
        last_probe_at: "", consecutive_failures: 0, drift: false,
        status: "switching", phase: "launching",
      },
    };

    renderWithQuery(
      <SlotStage group={group} live={live} sizeGb={noopSizeGb} onOpen={() => {}} />
    );

    const indicator = await screen.findByTestId("phase-indicator");
    expect(indicator).toHaveTextContent("evicting");
    expect(indicator).toHaveTextContent("launching");
    expect(indicator).toHaveTextContent("loading");
  });

  it("recipe click arms a confirm step; only the confirm click calls switchRecipe", async () => {
    const switchRecipe = vi.spyOn(api.runtimes.sparkrun, "switchRecipe").mockResolvedValue({
      ok: true, message: "Switching…", old_recipe: "qwen-general", new_recipe: "laguna-s21", launch_command: "sparkrun run laguna-s21",
    });
    const serving = makeRuntime({ slug: "rt", display_name: "DeepSeek V4 Flash", runtime_type: "vllm_docker", state: "ready" });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    // Recipes live behind a dropdown now (65-recipe catalog flooded the row).
    const trigger = await screen.findByTestId("recipe-dropdown-trigger");
    await act(async () => {
      trigger.click();
    });
    const option = await screen.findByText("laguna-s21");
    await act(async () => {
      option.click();
    });

    // Selecting only arms the confirm step — one click must never evict
    // whatever the GPU is currently serving.
    expect(switchRecipe).not.toHaveBeenCalled();
    const confirmBtn = await screen.findByText("Confirm switch");
    confirmBtn.click();

    await waitFor(() => expect(switchRecipe).toHaveBeenCalledWith("rt", "laguna-s21"));
  });

  // T1 (Verbund-UI, 30.08.2026): a verbund recipe's device requirement must
  // be visible in the row itself, not only discoverable by hovering.
  it("shows a verbund recipe's device requirement inline, disabled, with a plain-language tooltip", async () => {
    const switchRecipe = vi.spyOn(api.runtimes.sparkrun, "switchRecipe");
    vi.spyOn(api.runtimes.sparkrun, "listRecipes").mockResolvedValue({
      recipes: [
        { name: "qwen-general", model: "qwen3.6", registry: "official", tp: 1, nodes: 1, solo_capable: true },
        { name: "glm-verbund", model: "glm-5.3", registry: "official", tp: 2, nodes: 2, solo_capable: false },
      ],
    });
    const serving = makeRuntime({ slug: "rt", display_name: "DeepSeek V4 Flash", runtime_type: "vllm_docker", state: "ready" });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    const trigger = await screen.findByTestId("recipe-dropdown-trigger");
    await act(async () => { trigger.click(); });

    const verbundOption = await screen.findByText("glm-verbund");
    // Device requirement visible in the row itself (not just on hover).
    expect(await screen.findByText("Needs tp=2, nodes=2 — cannot run solo")).toBeInTheDocument();
    // Plain-language reason still available as a tooltip on top of that.
    const optionButton = verbundOption.closest("button");
    expect(optionButton).toHaveAttribute("title", "Needs tp=2, nodes=2 — cannot run solo on this host");
    expect(optionButton).toBeDisabled();

    // Genuinely not clickable through to a switch.
    optionButton?.click();
    expect(switchRecipe).not.toHaveBeenCalled();
  });

  it("a sibling engine in the unified dropdown arms a confirm; confirm calls start (slot takeover)", async () => {
    const start = vi.spyOn(api.runtimes, "start").mockResolvedValue({ ok: true, message: "started" });
    const serving = makeRuntime({ slug: "rt", display_name: "DeepSeek V4 Flash", runtime_type: "vllm_docker", state: "ready" });
    const stopped = makeRuntime({ slug: "other", display_name: "Qwen 3.6", runtime_type: "lmstudio", state: "stopped" });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving, stopped] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    await act(async () => { (await screen.findByTestId("recipe-dropdown-trigger")).click(); });
    await act(async () => { (await screen.findByTestId("switch-engine-other")).click(); });

    // Selecting only arms the confirm — one click must never evict the GPU.
    expect(start).not.toHaveBeenCalled();
    await act(async () => { (await screen.findByText("Confirm switch")).click(); });
    await waitFor(() => expect(start).toHaveBeenCalledWith(stopped.id));
  });

  it("renders a placeholder when the host has no runtimes at all", async () => {
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("No model set up")).toBeInTheDocument();
  });

  // Phase 1a (Verbund-UI, 30.08.2026): a kind="agent" host with zero bound
  // runtimes (a headless verbund worker, e.g. Beta as GLM rank1) must NOT
  // show "No model set up" — it's real fleet inventory, not an empty slot.
  it("renders a worker tile with real telemetry for a kind=agent host with no runtimes", async () => {
    const host = makeHost({ slug: "beta", display_name: "Beta", kind: "agent" });
    const group: HostGroup = { host, runtimes: [] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("Fleet worker")).toBeInTheDocument();
    expect(screen.queryByText("No model set up")).not.toBeInTheDocument();
    expect(screen.queryByText("+ Model")).not.toBeInTheDocument();
    // Real telemetry from the (mocked) beforeEach metrics response — the
    // honesty rule: only actually-fetched values, never a served_model
    // or a switch control (neither exists on this tile at all).
    expect(await screen.findByText("42 %")).toBeInTheDocument();
    expect(screen.getByText("55 °C")).toBeInTheDocument();
    expect(screen.queryByText(/served|switch/i)).not.toBeInTheDocument();
  });

  it("shows the honest unreachable text for a kind=agent worker instead of fabricated zero-values", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({
      reachable: false, gpu_util_pct: null, vram_used_mb: null, vram_total_mb: null, gpu_temp_c: null,
    });
    const host = makeHost({ slug: "beta", display_name: "Beta", kind: "agent" });
    const group: HostGroup = { host, runtimes: [] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("Host unreachable")).toBeInTheDocument();
    expect(screen.queryByText("0 %")).not.toBeInTheDocument();
    expect(screen.queryByText(/^0 /)).not.toBeInTheDocument();
  });

  // Verbund-UI Phase 1b (30.08.2026)
  it("shows which verbund a worker belongs to when grouping resolved a workerOf", async () => {
    const host = makeHost({ slug: "beta", display_name: "Beta", kind: "agent" });
    const group: HostGroup = {
      host, runtimes: [],
      workerOf: {
        runtimeId: "rt-1", runtimeDisplayName: "GLM Verbund",
        headSlug: "alpha", role: "worker", nodeRank: 1,
      },
    };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("Part of: GLM Verbund · head → alpha")).toBeInTheDocument();
    expect(screen.queryByText("No model of its own — telemetry for this box")).not.toBeInTheDocument();
  });

  it("falls back to the generic worker hint when workerOf is absent", async () => {
    const host = makeHost({ slug: "beta", display_name: "Beta", kind: "agent" });
    const group: HostGroup = { host, runtimes: [] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("No model of its own — telemetry for this box")).toBeInTheDocument();
  });

  // ── Geräte-Steuerung (GPU-Modus) ─────────────────────────────────────────
  // Der Schalter gehört in die Geräte-Ansicht — aber nur dort, wo er auch
  // etwas bewirken kann.

  it("puts the GPU mode strip into the serving tile of a paired box", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([makeDevice()]);
    const serving = makeRuntime({ slug: "rt", display_name: "DeepSeek V4", state: "ready" });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };

    renderWithQuery(
      <SlotStage
        group={group}
        live={{ rt: { reachable: true, latency_ms: 14 } as RuntimeLiveStatus }}
        sizeGb={noopSizeGb}
        onOpen={() => {}}
      />,
    );

    expect(await screen.findByTestId("device-control")).toBeInTheDocument();
    expect(screen.getByTestId("compact-mode-eco")).toHaveAttribute("aria-checked", "true");
    // Die Referenzwerte sind als solche markiert (HONESTY RULE) — die echten
    // Live-Werte stehen daneben in der Telemetrie-Spalte.
    expect(screen.getByTestId("compact-mode-boost")).toHaveTextContent("≈60 W");
    expect(screen.getByText(/^Measured: /)).toBeInTheDocument();
    expect(screen.getByText("55 °C")).toBeInTheDocument();
  });

  // Ein Schalter auf einer Box, die MC nicht stellen kann, ist eine Lüge.
  it("shows no GPU mode strip for a box without a node agent", async () => {
    const devices = vi.spyOn(api.nodes, "devices").mockResolvedValue([]);
    const serving = makeRuntime({ slug: "rt", state: "ready" });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    await screen.findByText("GPU-Box");
    await devicesSettled(devices);
    expect(screen.queryByTestId("device-control")).toBeNull();
  });

  it("shows no GPU mode strip while the host is unreachable", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({
      reachable: false, gpu_util_pct: null, vram_used_mb: null, vram_total_mb: null, gpu_temp_c: null,
    });
    const devices = vi
      .spyOn(api.nodes, "devices")
      .mockResolvedValue([makeDevice({ status: "yellow", reason: "stale" })]);
    const serving = makeRuntime({ slug: "rt", state: "ready" });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("Host unreachable")).toBeInTheDocument();
    await devicesSettled(devices);
    expect(screen.queryByTestId("device-control")).toBeNull();
  });

  // Graue Ampel = MC weiss nichts über den Zustand der Box. Einen Regler
  // anzubieten, dessen Ist-Stand niemand kennt, führt in die Irre.
  it("shows no GPU mode strip while the traffic light is grey", async () => {
    const devices = vi
      .spyOn(api.nodes, "devices")
      .mockResolvedValue([makeDevice({ status: "grey", reason: "no_agent", has_agent: false, device_state: null })]);
    const serving = makeRuntime({ slug: "rt", state: "ready" });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    await screen.findByText("GPU-Box");
    await devicesSettled(devices);
    expect(screen.queryByTestId("device-control")).toBeNull();
  });

  it("puts the strip on a worker tile too — a paired box with no runtime of its own", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([
      makeDevice({ host_id: "beta", slug: "beta", display_name: "Beta" }),
    ]);
    const host = makeHost({ slug: "beta", display_name: "Beta", kind: "agent" });
    const group: HostGroup = { host, runtimes: [] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("Fleet worker")).toBeInTheDocument();
    expect(await screen.findByTestId("device-control")).toBeInTheDocument();
  });

  it("is read-only for a non-admin", async () => {
    mockStore.state.currentUser = { id: "u2", email: "x@y.z", name: "Ops", role: "user" };
    vi.spyOn(api.nodes, "devices").mockResolvedValue([makeDevice()]);
    const serving = makeRuntime({ slug: "rt", state: "ready" });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByTestId("compact-mode-boost")).toBeDisabled();
  });

  it("a non-startable host runtime (e.g. omp) stays visible in the dropdown but disabled", async () => {
    const start = vi.spyOn(api.runtimes, "start").mockResolvedValue({ ok: true, message: "started" });
    const omp = makeRuntime({ slug: "omp1", display_name: "OMP Runtime", runtime_type: "omp", state: "stopped" });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [omp] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    await act(async () => { (await screen.findByTestId("recipe-dropdown-trigger")).click(); });
    const item = await screen.findByTestId("switch-engine-omp1");
    expect(item).toBeDisabled();
    item.click();
    expect(start).not.toHaveBeenCalled();
  });
});

// Migrated from the deleted runtime-card's runtime-live-status.test.tsx and
// runtime-identity-live.test.tsx (page.tsx dissolved into the stage — Task
// 5). Assertion intent kept; wording adapted to SlotStage's actual UI text
// (e.g. a stale/unreachable serving runtime now shows the "FAILED" state
// chip, not a literal "unreachable" sentence — the old runtime card's own
// unreachable message has no direct equivalent here).
//
// Dropped as duplicates of the "SlotStage" describe block above:
//   - "shows a switching chip instead of the unreachable error during a
//     recipe switch" — covered by "switching live status renders a phase
//     indicator".
//   - "renders neither served-model nor drift/unreachable text without a
//     live prop" — SlotStage always renders a model line from
//     `serving.model_identifier`, live or not (unlike the old runtime
//     card's detail line, which was entirely gated on `live`), so there is nothing
//     live-shaped left to assert here.
// Not migrated into this file (flagged, not silently dropped — see
// task-5-report.md):
//   - "flags a display_name that claims a version the model does not back"
//     / "stays quiet for an honest name" — the "Name" drift chip
//     (`display_name_drift`) is NOT rendered in SlotStage (scoped to this
//     file's surface only) but IS rendered in RuntimeDetailPanel (see
//     runtime-detail-panel.test.tsx) — the panel is the fix-up surface for
//     this signal, the stage stays honesty-minimal per its own doc comment.
describe("SlotStage — migrated live-status/identity assertions", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({
      reachable: true, gpu_util_pct: 42, vram_used_mb: 4096, vram_total_mb: 8192, gpu_temp_c: 55,
    });
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
      runtime_slug: "rt", count: 0, agents: [],
    });
    vi.spyOn(api.runtimes.sparkrun, "listRecipes").mockResolvedValue({ recipes: [] });
    vi.spyOn(api.runtimes.sparkrun, "currentRecipe").mockResolvedValue({
      slug: "rt", current_recipe: null, sparkrun_managed: false,
    });
  });

  it("renders served model + Drift badge when reachable and drifted", async () => {
    const serving = makeRuntime({
      slug: "rt", display_name: "Engine X", runtime_type: "vllm_docker",
      state: "ready", model_identifier: "engine-x",
    });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };
    const live: Record<string, RuntimeLiveStatus> = {
      rt: {
        reachable: true, served_model: "engine-x", latency_ms: 12,
        last_probe_at: "2026-07-04T00:00:00Z", consecutive_failures: 0, drift: true,
      },
    };

    renderWithQuery(<SlotStage group={group} live={live} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText(/engine-x/)).toBeInTheDocument();
    expect(screen.getByText("Drift")).toBeInTheDocument();
  });

  it("shows the FAILED state when the DB state is active but the engine is unreachable", async () => {
    const serving = makeRuntime({ slug: "rt", display_name: "Engine X", runtime_type: "vllm_docker", state: "ready" });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };
    const live: Record<string, RuntimeLiveStatus> = {
      rt: {
        reachable: false, served_model: null, latency_ms: null,
        last_probe_at: "2026-07-04T00:00:00Z", consecutive_failures: 3, drift: false,
      },
    };

    renderWithQuery(<SlotStage group={group} live={live} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("FAILED")).toBeInTheDocument();
    expect(screen.queryByText(/\d+ ms/)).not.toBeInTheDocument();
  });

  it("shows the ENGINE's context window, not the stored one, when they disagree — with a Drift badge", async () => {
    const serving = makeRuntime({
      slug: "rt", display_name: "Spark vLLM", runtime_type: "vllm_docker",
      state: "ready", max_context_len: 98304,
    });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };
    const live: Record<string, RuntimeLiveStatus> = {
      rt: {
        reachable: true, served_model: "deepseek-v4-flash-0731-spark", served_context_len: 262144,
        context_drift: true, latency_ms: 12, last_probe_at: "2026-08-08T00:00:00Z",
        consecutive_failures: 0, drift: false,
      },
    };

    renderWithQuery(<SlotStage group={group} live={live} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("262k")).toBeInTheDocument();
    expect(screen.queryByText("98k")).not.toBeInTheDocument();
    expect(screen.getByText("Drift")).toBeInTheDocument();
  });

  it("falls back to the stored context window when the probe reports none", async () => {
    const serving = makeRuntime({
      slug: "rt", display_name: "Spark vLLM", runtime_type: "vllm_docker",
      state: "ready", max_context_len: 98304,
    });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };
    const live: Record<string, RuntimeLiveStatus> = {
      rt: {
        reachable: true, served_model: "deepseek-v4-flash-0731-spark", latency_ms: 12,
        last_probe_at: "2026-08-08T00:00:00Z", consecutive_failures: 0, drift: false,
      },
    };

    renderWithQuery(<SlotStage group={group} live={live} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("98k")).toBeInTheDocument();
  });

  it("shows the context window for a non-vllm_docker runtime too, without shrinking a million-token window", async () => {
    // The old chip was gated on runtime_type === "vllm_docker" and fmtCtx was
    // capped at the largest CTX_PRESET — this pins both fixes: any runtime
    // type shows its real window, at any size.
    const serving = makeRuntime({
      slug: "rt", display_name: "Claude Opus", runtime_type: "cloud",
      state: "ready", model_identifier: "claude-opus-5", max_context_len: 1_000_000,
    });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("1M")).toBeInTheDocument();
  });

  it("never renders the Name-drift chip — that signal lives in RuntimeDetailPanel, not the stage", async () => {
    const serving = makeRuntime({
      slug: "rt", display_name: "Laguna 2.1", runtime_type: "vllm_docker",
      state: "ready", model_identifier: "laguna-2.0", display_name_drift: ["2.1"],
    });
    const host = makeHost({ slug: "spark", display_name: "GPU-Box" });
    const group: HostGroup = { host, runtimes: [serving] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    await screen.findByText("Laguna 2.1");
    expect(screen.queryByText("Name")).not.toBeInTheDocument();
  });
});
