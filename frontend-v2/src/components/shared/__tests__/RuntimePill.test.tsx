/**
 * RuntimePill — Phase 15 T3.6 vitest.
 *
 * Coverage:
 *   1. compact variant renders display_name once runtime data resolves
 *   2. host agent without runtime_id shows the muted scope chip
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RuntimePill } from "../RuntimePill";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const mkAgent = (overrides: Partial<Agent> = {}): Agent =>
  ({
    id: "a",
    name: "Davinci",
    agent_runtime: "cli-bridge",
    runtime_id: "rt-1",
    status: "online",
    is_board_lead: false,
    context_tokens: 0,
    context_max: 200_000,
    ...overrides,
  }) as Agent;

describe("RuntimePill", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the display_name once runtime data resolves (compact)", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({
      runtimes: [
        {
          id: "rt-1",
          slug: "qwen-general",
          display_name: "Qwen 3.6",
          runtime_type: "vllm_docker",
          endpoint: "http://x/v1",
          enabled: true,
        } as never,
      ],
    } as never);
    renderWithQuery(<RuntimePill agent={mkAgent()} variant="compact" />);
    await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
  });

  it("renders host scope chip for host agents without runtime_id", () => {
    renderWithQuery(
      <RuntimePill
        agent={mkAgent({ agent_runtime: "host", runtime_id: null, model: "claude-opus-4-7" })}
        variant="compact"
      />,
    );
    expect(screen.getByText("host")).toBeInTheDocument();
    expect(screen.getByText("claude-opus-4-7")).toBeInTheDocument();
  });

  it("renders hermes runtime with the distinct teal pill color", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({
      runtimes: [
        {
          id: "rt-hermes",
          slug: "hermes-host",
          display_name: "Hermes",
          runtime_type: "hermes",
          endpoint: "host://hermes",
          enabled: true,
          single_instance: true,
        } as never,
      ],
    } as never);
    const { container } = renderWithQuery(
      <RuntimePill agent={mkAgent({ runtime_id: "rt-hermes" })} variant="default" />,
    );
    await waitFor(() => expect(screen.getByText("Hermes")).toBeInTheDocument());
    // jsdom normalises hex → rgb(20, 196, 196) — C.accentHover
    const dot = Array.from(container.querySelectorAll("span")).find(
      (el) =>
        el.getAttribute("style")?.includes("20, 196, 196") ||
        el.getAttribute("style")?.toLowerCase().includes("#14c4c4"),
    );
    expect(dot, "expected a span with the hermes teal color #14C4C4").toBeDefined();
    expect(dot).toBeTruthy();
  });

  it("renders the lock icon when runtime.single_instance === true", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({
      runtimes: [
        {
          id: "rt-hermes",
          slug: "hermes-host",
          display_name: "Hermes",
          runtime_type: "hermes",
          endpoint: "host://hermes",
          enabled: true,
          single_instance: true,
        } as never,
      ],
    } as never);
    renderWithQuery(<RuntimePill agent={mkAgent({ runtime_id: "rt-hermes" })} variant="compact" />);
    await waitFor(() => expect(screen.getByTestId("runtime-lock-icon")).toBeInTheDocument());
  });

  it("does not render the lock icon when single_instance is false/undefined", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({
      runtimes: [
        {
          id: "rt-1",
          slug: "qwen-general",
          display_name: "Qwen 3.6",
          runtime_type: "vllm_docker",
          endpoint: "http://x/v1",
          enabled: true,
        } as never,
      ],
    } as never);
    renderWithQuery(<RuntimePill agent={mkAgent()} variant="compact" />);
    await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
    expect(screen.queryByTestId("runtime-lock-icon")).toBeNull();
  });
});
