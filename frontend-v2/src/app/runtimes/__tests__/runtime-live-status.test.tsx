import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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

const RUNTIME: Runtime = {
  id: "runtime-1",
  slug: "engine-x-runtime",
  display_name: "Engine X",
  runtime_type: "vllm_docker",
  provider: "vllm",
  endpoint: "http://192.0.2.10:8001/v1",
  healthcheck_path: "/health",
  container_name: "mc-engine-x-vllm",
  model_identifier: "engine-x",
  role_tags: [],
  supports_tools: true,
  supports_reasoning: false,
  supports_streaming: true,
  preferred_context_len: 8192,
  max_context_len: 32768,
  gpu_profile: "default",
  memory_notes: "",
  startup_notes: "",
  ui_order: 0,
  enabled: true,
  state: "ready",
};

describe("RuntimeCard live status", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
      runtime_slug: "engine-x-runtime",
      count: 0,
      agents: [],
    });
  });

  it("renders served model + Drift badge when reachable and drifted", async () => {
    const live: RuntimeLiveStatus = {
      reachable: true,
      served_model: "engine-x",
      latency_ms: 12,
      last_probe_at: "2026-07-04T00:00:00Z",
      consecutive_failures: 0,
      drift: true,
    };
    renderWithQuery(<RuntimeCard runtime={RUNTIME} live={live} />);
    expect(await screen.findByText(/engine-x/)).toBeInTheDocument();
    expect(screen.getByText("Drift")).toBeInTheDocument();
  });

  it("renders unreachable message when live status reports unreachable", async () => {
    const live: RuntimeLiveStatus = {
      reachable: false,
      served_model: null,
      latency_ms: null,
      last_probe_at: "2026-07-04T00:00:00Z",
      consecutive_failures: 3,
      drift: false,
    };
    renderWithQuery(<RuntimeCard runtime={RUNTIME} live={live} />);
    expect(await screen.findByText(/unreachable/i)).toBeInTheDocument();
  });

  it("renders neither served-model nor drift/unreachable text without a live prop", async () => {
    renderWithQuery(<RuntimeCard runtime={RUNTIME} />);
    await waitFor(() => expect(api.runtimes.db.agents).toHaveBeenCalled());
    expect(screen.queryByText(/Engine serves:/)).toBeNull();
    expect(screen.queryByText("Drift")).toBeNull();
    expect(screen.queryByText(/unreachable/i)).toBeNull();
  });
});
