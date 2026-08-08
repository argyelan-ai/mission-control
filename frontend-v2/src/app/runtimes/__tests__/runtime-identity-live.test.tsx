/**
 * The runtime cockpit must show what the ENGINE serves, not what the row says
 * (PR9).
 *
 * On 08.08. the Spark was switched to deepseek-v4-flash-0731-spark with a 262k
 * window. /runtimes kept reading "Spark vLLM (Laguna/Qwen — switchable)" with a
 * 98k chip, because the card showed the box name and the stored column and had
 * no way to say "the engine disagrees". These tests pin the three fixes:
 *
 *   1. the context chip shows the LIVE window and warns when it differs,
 *   2. it is no longer gated to vllm_docker (the column feeds every agent's
 *      env, not just that one engine type),
 *   3. a display_name that claims a version the served model does not back
 *      says so on the card.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RuntimeCard } from "../page";
import { api } from "@/lib/api";
import type { Runtime, RuntimeLiveStatus } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const mkRuntime = (overrides: Partial<Runtime> = {}): Runtime =>
  ({
    id: "runtime-spark",
    slug: "qwen-general",
    display_name: "Spark vLLM (switchbar)",
    runtime_type: "vllm_docker",
    provider: "vllm",
    endpoint: "http://192.0.2.10:8000/v1",
    healthcheck_path: "/health",
    container_name: "sparkrun_solo",
    model_identifier: "deepseek-v4-flash-0731-spark",
    role_tags: [],
    supports_tools: true,
    supports_reasoning: false,
    supports_streaming: true,
    preferred_context_len: 98304,
    max_context_len: 98304,
    gpu_profile: "default",
    memory_notes: "",
    startup_notes: "",
    ui_order: 0,
    enabled: true,
    state: "ready",
    ...overrides,
  }) as Runtime;

const mkLive = (overrides: Partial<RuntimeLiveStatus> = {}): RuntimeLiveStatus => ({
  reachable: true,
  served_model: "deepseek-v4-flash-0731-spark",
  latency_ms: 12,
  last_probe_at: "2026-08-08T00:00:00Z",
  consecutive_failures: 0,
  drift: false,
  ...overrides,
});

describe("RuntimeCard — live model identity", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
      runtime_slug: "qwen-general",
      count: 0,
      agents: [],
    });
  });

  it("shows the ENGINE's window, not the stored one, when they disagree", async () => {
    renderWithQuery(
      <RuntimeCard
        runtime={mkRuntime()}
        live={mkLive({ served_context_len: 262144, context_drift: true })}
      />,
    );
    expect(await screen.findByText("262k ctx")).toBeInTheDocument();
    expect(screen.queryByText("98k ctx")).not.toBeInTheDocument();
  });

  it("falls back to the stored window when the probe reports none", async () => {
    renderWithQuery(<RuntimeCard runtime={mkRuntime()} live={mkLive()} />);
    expect(await screen.findByText("98k ctx")).toBeInTheDocument();
  });

  it("shows the window for a non-vllm_docker runtime too", async () => {
    // The old chip was gated on runtime_type === "vllm_docker", so the omp and
    // cloud rows showed no window at all — while max_context_len is exactly
    // what gets rendered into an omp agent's OMP_CONTEXT_WINDOW.
    renderWithQuery(
      <RuntimeCard
        runtime={mkRuntime({ runtime_type: "omp", max_context_len: 262144 })}
        live={mkLive()}
      />,
    );
    expect(await screen.findByText("262k ctx")).toBeInTheDocument();
  });

  it("does not shrink a million-token window to 262k", async () => {
    // fmtCtx used to be a ladder capped at the largest CTX_PRESET.
    renderWithQuery(
      <RuntimeCard
        runtime={mkRuntime({
          runtime_type: "cloud",
          model_identifier: "claude-opus-5",
          max_context_len: 1_000_000,
        })}
        live={undefined}
      />,
    );
    expect(await screen.findByText("1M ctx")).toBeInTheDocument();
  });

  it("flags a display_name that claims a version the model does not back", async () => {
    renderWithQuery(
      <RuntimeCard
        runtime={mkRuntime({
          display_name: "Spark vLLM (Laguna 2.1)",
          display_name_drift: ["2.1"],
        })}
        live={mkLive()}
      />,
    );
    expect(await screen.findByText("Name")).toBeInTheDocument();
  });

  it("stays quiet for an honest name", async () => {
    renderWithQuery(
      <RuntimeCard runtime={mkRuntime({ display_name_drift: [] })} live={mkLive()} />,
    );
    await screen.findByText("98k ctx");
    expect(screen.queryByText("Name")).not.toBeInTheDocument();
  });
});
