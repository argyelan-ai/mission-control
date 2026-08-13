import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SlotStage } from "../SlotStage";
import { api } from "@/lib/api";
import type { Runtime, Host, RuntimeLiveStatus } from "@/lib/types";
import type { HostGroup } from "../grouping";

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
    ssh_host: null, ssh_user: null, ssh_key_path: null, control_url: null,
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
  });

  it("serving stage renders model+ctx+latency and never renders tok/s", async () => {
    const serving = makeRuntime({
      slug: "rt", display_name: "DeepSeek V4 Flash", runtime_type: "vllm_docker",
      state: "ready", model_identifier: "deepseek-v4-flash-0731-spark", max_context_len: 262144,
    });
    const host = makeHost({ slug: "spark", display_name: "DGX Spark" });
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

  it("switching live status renders a phase indicator", async () => {
    const serving = makeRuntime({ slug: "rt", display_name: "DeepSeek V4 Flash", runtime_type: "vllm_docker", state: "ready" });
    const host = makeHost({ slug: "spark", display_name: "DGX Spark" });
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

  it("recipe click calls switchRecipe with the runtime id and recipe name", async () => {
    const switchRecipe = vi.spyOn(api.runtimes.sparkrun, "switchRecipe").mockResolvedValue({
      ok: true, message: "Switching…", old_recipe: "qwen-general", new_recipe: "laguna-s21", launch_command: "sparkrun run laguna-s21",
    });
    const serving = makeRuntime({ slug: "rt", display_name: "DeepSeek V4 Flash", runtime_type: "vllm_docker", state: "ready" });
    const host = makeHost({ slug: "spark", display_name: "DGX Spark" });
    const group: HostGroup = { host, runtimes: [serving] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    const btn = await screen.findByText("laguna-s21");
    btn.click();

    await waitFor(() => expect(switchRecipe).toHaveBeenCalledWith("rt", "laguna-s21"));
  });

  it("ready-row click calls onOpen with that runtime", async () => {
    const serving = makeRuntime({ slug: "rt", display_name: "DeepSeek V4 Flash", runtime_type: "vllm_docker", state: "ready" });
    const stopped = makeRuntime({ slug: "other", display_name: "Qwen 3.6", runtime_type: "lmstudio", state: "stopped" });
    const host = makeHost({ slug: "spark", display_name: "DGX Spark" });
    const group: HostGroup = { host, runtimes: [serving, stopped] };
    const onOpen = vi.fn();

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={onOpen} />);

    const row = await screen.findByTestId("ready-row-other");
    row.click();

    expect(onOpen).toHaveBeenCalledWith(stopped);
    // Switch row carries recipes only (M1 mockup) — the ready list is the
    // sole home for sibling runtimes, so its display name must appear once.
    expect(screen.getAllByText("Qwen 3.6")).toHaveLength(1);
  });

  it("renders a placeholder when nothing is serving and nothing is ready to start", async () => {
    const cloud = makeRuntime({ slug: "claude", display_name: "Claude", runtime_type: "cloud", state: "stopped" });
    const host = makeHost({ slug: "spark", display_name: "DGX Spark" });
    const group: HostGroup = { host, runtimes: [cloud] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("No model set up")).toBeInTheDocument();
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
// Not migrated (flagged, not silently dropped — see task-5-report.md):
//   - "flags a display_name that claims a version the model does not back"
//     / "stays quiet for an honest name" — `display_name_drift` (the "Name"
//     drift chip) is not rendered anywhere in SlotStage or
//     RuntimeDetailPanel in this redesign. Real feature gap, out of this
//     task's scope (SlotStage/RuntimeDetailPanel are Task 3/2 surfaces).
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
    const host = makeHost({ slug: "spark", display_name: "DGX Spark" });
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
    const host = makeHost({ slug: "spark", display_name: "DGX Spark" });
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
    const host = makeHost({ slug: "spark", display_name: "DGX Spark" });
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
    const host = makeHost({ slug: "spark", display_name: "DGX Spark" });
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
    const host = makeHost({ slug: "spark", display_name: "DGX Spark" });
    const group: HostGroup = { host, runtimes: [serving] };

    renderWithQuery(<SlotStage group={group} sizeGb={noopSizeGb} onOpen={() => {}} />);

    expect(await screen.findByText("1M")).toBeInTheDocument();
  });
});
