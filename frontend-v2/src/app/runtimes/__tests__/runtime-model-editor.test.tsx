import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RuntimeCard } from "../page";
import { api } from "@/lib/api";
import type { Runtime } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const makeRuntime = (over: Partial<Runtime> = {}): Runtime => ({
  id: "runtime-cloud-1",
  slug: "anthropic-cloud",
  display_name: "Anthropic Cloud",
  runtime_type: "cloud",
  provider: "anthropic",
  endpoint: "https://api.anthropic.com",
  healthcheck_path: "/health",
  container_name: null,
  model_identifier: "claude-opus-4-7",
  role_tags: [],
  supports_tools: true,
  supports_reasoning: true,
  supports_streaming: true,
  preferred_context_len: 200000,
  max_context_len: 200000,
  gpu_profile: "default",
  memory_notes: "",
  startup_notes: "",
  ui_order: 0,
  enabled: true,
  state: "ready",
  ...over,
});

describe("RuntimeCard model editor", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
      runtime_slug: "anthropic-cloud",
      count: 0,
      agents: [],
    });
  });

  it("shows the pencil edit affordance for non-probeable (cloud) runtimes", async () => {
    renderWithQuery(<RuntimeCard runtime={makeRuntime()} />);
    expect(
      await screen.findByLabelText("Modell bearbeiten")
    ).toBeInTheDocument();
    expect(screen.getByText("claude-opus-4-7")).toBeInTheDocument();
  });

  it("does NOT show the edit affordance for probeable (vllm_docker) runtimes", async () => {
    renderWithQuery(
      <RuntimeCard runtime={makeRuntime({ runtime_type: "vllm_docker" })} />
    );
    await waitFor(() => expect(api.runtimes.db.agents).toHaveBeenCalled());
    expect(screen.queryByLabelText("Modell bearbeiten")).toBeNull();
  });

  it("saving calls PATCH with the new model_identifier", async () => {
    const updateSpy = vi
      .spyOn(api.runtimes.db, "update")
      .mockResolvedValue(makeRuntime({ model_identifier: "claude-opus-4-8" }));
    const user = userEvent.setup();

    renderWithQuery(<RuntimeCard runtime={makeRuntime()} />);
    await user.click(await screen.findByLabelText("Modell bearbeiten"));

    const input = screen.getByLabelText("Modell-Identifier");
    await user.clear(input);
    await user.type(input, "claude-opus-4-8");
    await user.click(screen.getByLabelText("Speichern"));

    await waitFor(() =>
      expect(updateSpy).toHaveBeenCalledWith("anthropic-cloud", {
        model_identifier: "claude-opus-4-8",
      })
    );
  });

  it("cancel discards the edit without calling PATCH", async () => {
    const updateSpy = vi.spyOn(api.runtimes.db, "update");
    const user = userEvent.setup();

    renderWithQuery(<RuntimeCard runtime={makeRuntime()} />);
    await user.click(await screen.findByLabelText("Modell bearbeiten"));

    const input = screen.getByLabelText("Modell-Identifier");
    await user.clear(input);
    await user.type(input, "something-else");
    await user.click(screen.getByLabelText("Abbrechen"));

    expect(updateSpy).not.toHaveBeenCalled();
    // Back to display mode with the original value.
    expect(screen.getByText("claude-opus-4-7")).toBeInTheDocument();
  });
});
