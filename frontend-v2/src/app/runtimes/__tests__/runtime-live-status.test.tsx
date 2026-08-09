import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RuntimeListCard } from "../RuntimeListCard";
import { api } from "@/lib/api";
import type { Runtime } from "@/lib/types";

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

// Drift-badge rendering and unreachable-reason rendering (reachable/drifted,
// reachable=false with consecutive_failures) are already covered by
// runtime-list-card.test.tsx ("shows drift badge only when live reports
// drift" / "shows a red dot when state says ready but live reports
// unreachable"). Only the no-live-prop case remains here.
describe("RuntimeListCard live status", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
      runtime_slug: "engine-x-runtime",
      count: 0,
      agents: [],
    });
  });

  it("renders neither drift nor unreachable text without a live prop", async () => {
    renderWithQuery(<RuntimeListCard runtime={RUNTIME} onOpen={() => {}} />);
    await waitFor(() => expect(api.runtimes.db.agents).toHaveBeenCalled());
    expect(screen.queryByText("Drift")).toBeNull();
    expect(screen.queryByText(/unreachable/i)).toBeNull();
  });
});
