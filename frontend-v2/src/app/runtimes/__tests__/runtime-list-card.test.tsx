import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RuntimeListCard } from "../RuntimeListCard";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { Runtime } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
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

describe("RuntimeListCard", () => {
  beforeEach(() => {
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
      runtime_slug: "rt", count: 1,
      agents: [{ id: "a1", name: "Sparky", agent_runtime: "cli-bridge" }],
    });
  });

  it("active card shows model line, agent chip, and no action buttons", async () => {
    renderWithQuery(
      <RuntimeListCard
        runtime={makeRuntime({ state: "ready", model_identifier: "deepseek-v4" })}
        live={{ reachable: true, served_model: "deepseek-v4", latency_ms: 12, last_probe_at: "", consecutive_failures: 0, drift: false }}
        onOpen={() => {}}
      />
    );
    expect(screen.getByText(/deepseek-v4/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Sparky")).toBeInTheDocument());
    // Display-only: the card itself is the ONLY button.
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  it("shows drift badge only when live reports drift", () => {
    renderWithQuery(
      <RuntimeListCard
        runtime={makeRuntime({ state: "ready" })}
        live={{ reachable: true, served_model: "other", latency_ms: 5, last_probe_at: "", consecutive_failures: 0, drift: true }}
        onOpen={() => {}}
      />
    );
    expect(screen.getByText("Drift")).toBeInTheDocument();
  });

  it("failed card renders the unreachable reason", () => {
    renderWithQuery(
      <RuntimeListCard
        runtime={makeRuntime({ state: "failed" })}
        live={{ reachable: false, served_model: null, latency_ms: null, last_probe_at: "", consecutive_failures: 3, drift: false }}
        onOpen={() => {}}
      />
    );
    expect(screen.getByText(/unreachable \(3/i)).toBeInTheDocument();
  });

  it("failed card without a live prop still shows a probes suffix", () => {
    renderWithQuery(
      <RuntimeListCard runtime={makeRuntime({ state: "failed" })} onOpen={() => {}} />
    );
    expect(screen.getByText(/unreachable \(\? probes\)/i)).toBeInTheDocument();
  });

  it("shows a red dot when state says ready but live reports unreachable (state/live divergence)", () => {
    const { container } = renderWithQuery(
      <RuntimeListCard
        runtime={makeRuntime({ state: "ready" })}
        live={{ reachable: false, served_model: null, latency_ms: null, last_probe_at: "", consecutive_failures: 5, drift: false }}
        onOpen={() => {}}
      />
    );
    // The row must render as failed (red dot + reason), never a green "ready" dot.
    expect(screen.getByText(/unreachable \(5/i)).toBeInTheDocument();
    const dot = container.querySelector('[data-testid="state-dot"]') as HTMLElement;
    expect(dot).not.toBeNull();
    // jsdom normalizes hex → rgb() on read, so normalize the expected token the same way.
    const probe = document.createElement("div");
    probe.style.background = C.error;
    expect(dot.style.background).toBe(probe.style.background);
  });

  it("clicking the card calls onOpen", async () => {
    const onOpen = vi.fn();
    const rt = makeRuntime({ state: "stopped" });
    renderWithQuery(<RuntimeListCard runtime={rt} onOpen={onOpen} />);
    screen.getByRole("button").click();
    expect(onOpen).toHaveBeenCalledWith(rt);
  });
});
