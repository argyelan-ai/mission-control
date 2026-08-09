import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RuntimeDetailPanel } from "../RuntimeDetailPanel";
import { api } from "@/lib/api";
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

describe("RuntimeDetailPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
      runtime_slug: "rt", count: 0, agents: [],
    });
  });

  it("cloud runtime: no Start/Stop, but model editor present", async () => {
    renderWithQuery(<RuntimeDetailPanel open runtime={makeRuntime({ runtime_type: "cloud", model_identifier: "claude-opus-5" })} onClose={() => {}} />);
    expect(screen.queryByTitle("Start")).not.toBeInTheDocument();
    expect(screen.getByText("claude-opus-5")).toBeInTheDocument();
    expect(screen.getByTitle("Edit model")).toBeInTheDocument();
  });

  it("vllm runtime: Start enabled when stopped, calls api.runtimes.start", async () => {
    const start = vi.spyOn(api.runtimes, "start").mockResolvedValue({ ok: true, message: "ok" });
    renderWithQuery(<RuntimeDetailPanel open runtime={makeRuntime({ runtime_type: "vllm_docker", state: "stopped" })} onClose={() => {}} />);
    const btn = screen.getByTitle("Start");
    expect(btn).toBeEnabled();
    btn.click();
    await waitFor(() => expect(start).toHaveBeenCalledWith("rt", undefined));
  });

  it("renders nothing when runtime is null", () => {
    const { container } = renderWithQuery(<RuntimeDetailPanel open runtime={null} onClose={() => {}} />);
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  });
});
