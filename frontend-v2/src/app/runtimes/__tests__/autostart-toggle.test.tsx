import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RuntimeCard } from "../page";
import { api } from "@/lib/api";
import type { Runtime, RuntimeAutostartStatus } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const BASE_RUNTIME: Runtime = {
  id: "runtime-1",
  slug: "spark-vllm",
  display_name: "Spark vLLM",
  runtime_type: "vllm_docker",
  provider: "vllm",
  endpoint: "http://192.0.2.10:8001/v1",
  healthcheck_path: "/health",
  container_name: "mc-spark-vllm",
  model_identifier: "spark-model",
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
  autostart_supported: true,
};

describe("AutostartToggle (via RuntimeCard)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
      runtime_slug: "spark-vllm",
      count: 0,
      agents: [],
    });
  });

  it("does not render the toggle when autostart_supported is false", () => {
    renderWithQuery(<RuntimeCard runtime={{ ...BASE_RUNTIME, autostart_supported: false }} />);
    expect(screen.queryByRole("switch", { name: /autostart/i })).not.toBeInTheDocument();
  });

  it("shows 'an' state when the host reports the flag file present", async () => {
    vi.spyOn(api.runtimes.db, "autostartStatus").mockResolvedValue({
      slug: "spark-vllm",
      flag_path: "/home/marknx/scripts/vllm-autostart.enabled",
      enabled: true,
      reachable: true,
    } satisfies RuntimeAutostartStatus);

    renderWithQuery(<RuntimeCard runtime={BASE_RUNTIME} />);

    const toggle = await screen.findByRole("switch", { name: /autostart/i });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
    expect(toggle).not.toBeDisabled();
  });

  it("shows disabled 'unbekannt' state when the host is unreachable", async () => {
    vi.spyOn(api.runtimes.db, "autostartStatus").mockResolvedValue({
      slug: "spark-vllm",
      flag_path: "/home/marknx/scripts/vllm-autostart.enabled",
      enabled: null,
      reachable: false,
    } satisfies RuntimeAutostartStatus);

    renderWithQuery(<RuntimeCard runtime={BASE_RUNTIME} />);

    const toggle = await screen.findByRole("switch", { name: /autostart/i });
    await waitFor(() => expect(toggle).toBeDisabled());
    expect(toggle).toHaveAttribute("title", expect.stringMatching(/nicht erreichbar/i));
  });

  it("clicking the toggle calls setAutostart with the flipped value and reflects the confirmed state", async () => {
    vi.spyOn(api.runtimes.db, "autostartStatus").mockResolvedValue({
      slug: "spark-vllm",
      flag_path: "/home/marknx/scripts/vllm-autostart.enabled",
      enabled: false,
      reachable: true,
    } satisfies RuntimeAutostartStatus);
    const setAutostart = vi.spyOn(api.runtimes.db, "setAutostart").mockResolvedValue({
      slug: "spark-vllm",
      flag_path: "/home/marknx/scripts/vllm-autostart.enabled",
      enabled: true,
      reachable: true,
    } satisfies RuntimeAutostartStatus);

    renderWithQuery(<RuntimeCard runtime={BASE_RUNTIME} />);

    const toggle = await screen.findByRole("switch", { name: /autostart/i });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "false"));

    const user = userEvent.setup();
    await user.click(toggle);

    expect(setAutostart).toHaveBeenCalledWith("spark-vllm", true);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
  });
});
