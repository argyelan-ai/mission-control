import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CloudUsage } from "../CloudUsage";
import { api } from "@/lib/api";
import type { Runtime, RuntimeAgentsResponse } from "@/lib/types";

// Fixture copied from grouping.test.ts (this repo's established pattern — see
// slot-stage.test.tsx doing the same rather than importing another test
// file's locals).
function makeRuntime(over: Partial<Runtime>): Runtime {
  return {
    id: over.slug ?? "rt", slug: "rt", display_name: "RT",
    runtime_type: "cloud", provider: "anthropic",
    endpoint: "https://api.anthropic.com/v1", healthcheck_path: "/health",
    container_name: null, role_tags: [], supports_tools: true,
    supports_reasoning: false, supports_streaming: true,
    preferred_context_len: 8192, max_context_len: 32768,
    gpu_profile: "default", memory_notes: "", startup_notes: "",
    ui_order: 0, enabled: true, state: "unknown",
    ...over,
  };
}

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function agentsResponse(slug: string, agents: RuntimeAgentsResponse["agents"]): RuntimeAgentsResponse {
  return { runtime_slug: slug, count: agents.length, agents };
}

describe("CloudUsage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a bound runtime as agent chips left + mono model right", async () => {
    const rt = makeRuntime({ slug: "opus", display_name: "Claude Opus", model_identifier: "claude-opus-5" });
    vi.spyOn(api.runtimes.db, "agents").mockImplementation((slug) =>
      Promise.resolve(
        slug === "opus"
          ? agentsResponse("opus", [
              { id: "a1", name: "Boss", agent_runtime: "host" },
              { id: "a2", name: "Rex", agent_runtime: "cli-bridge" },
            ])
          : agentsResponse(slug, [])
      )
    );

    renderWithQuery(<CloudUsage runtimes={[rt]} onOpen={() => {}} />);

    const row = await screen.findByTestId("cloud-usage-row-opus");
    expect(within(row).getByText("Boss")).toBeInTheDocument();
    expect(within(row).getByText("Rex")).toBeInTheDocument();
    expect(within(row).getByText("claude-opus-5")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "Boss" })).toHaveAttribute("href", "/agents/a1");
  });

  it("caps chips at 3 and shows a +N overflow chip", async () => {
    const rt = makeRuntime({ slug: "sonnet", display_name: "Claude Sonnet", model_identifier: "claude-sonnet-5" });
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue(
      agentsResponse("sonnet", [
        { id: "a1", name: "Tester", agent_runtime: "cli-bridge" },
        { id: "a2", name: "Davinci", agent_runtime: "cli-bridge" },
        { id: "a3", name: "Shakespeare", agent_runtime: "cli-bridge" },
        { id: "a4", name: "FreeCode", agent_runtime: "cli-bridge" },
        { id: "a5", name: "Deployer", agent_runtime: "cli-bridge" },
        { id: "a6", name: "Installer", agent_runtime: "cli-bridge" },
      ])
    );

    renderWithQuery(<CloudUsage runtimes={[rt]} onOpen={() => {}} />);

    const row = await screen.findByTestId("cloud-usage-row-sonnet");
    expect(within(row).getAllByRole("link")).toHaveLength(3);
    expect(within(row).getByText("+3")).toBeInTheDocument();
  });

  it("hides zero-agent runtimes behind a dashed collapse row until expanded", async () => {
    const bound = makeRuntime({ slug: "grok", display_name: "Grok", model_identifier: "grok-4.5" });
    const unbound1 = makeRuntime({ slug: "kimi", display_name: "Kimi", model_identifier: "kimi-k3" });
    const unbound2 = makeRuntime({ slug: "gemini", display_name: "Gemini", model_identifier: "gemini-3-pro" });
    vi.spyOn(api.runtimes.db, "agents").mockImplementation((slug) =>
      Promise.resolve(
        slug === "grok"
          ? agentsResponse("grok", [{ id: "a1", name: "Grok", agent_runtime: "cli-bridge" }])
          : agentsResponse(slug, [])
      )
    );

    renderWithQuery(<CloudUsage runtimes={[bound, unbound1, unbound2]} onOpen={() => {}} />);

    await screen.findByTestId("cloud-usage-row-grok");
    expect(screen.queryByTestId("cloud-usage-row-kimi")).not.toBeInTheDocument();
    expect(screen.queryByTestId("cloud-usage-row-gemini")).not.toBeInTheDocument();

    const toggle = await screen.findByTestId("cloud-usage-collapse-toggle");
    expect(toggle).toHaveTextContent("2");

    toggle.click();

    expect(await screen.findByTestId("cloud-usage-row-kimi")).toBeInTheDocument();
    expect(screen.getByTestId("cloud-usage-row-gemini")).toBeInTheDocument();
  });

  it("shows a pending-sync chip when any bound agent has pending_runtime_sync", async () => {
    const rt = makeRuntime({ slug: "sparky", display_name: "Sparky Cloud", model_identifier: "qwen3-coder-next" });
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue(
      agentsResponse("sparky", [
        { id: "a1", name: "Sparky", agent_runtime: "cli-bridge", pending_runtime_sync: true },
      ])
    );

    renderWithQuery(<CloudUsage runtimes={[rt]} onOpen={() => {}} />);

    const row = await screen.findByTestId("cloud-usage-row-sparky");
    expect(within(row).getByText("pending sync")).toBeInTheDocument();
  });

  it("clicking a row calls onOpen with that runtime, and never renders a status dot", async () => {
    const rt = makeRuntime({ slug: "opus", display_name: "Claude Opus", model_identifier: "claude-opus-5" });
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue(
      agentsResponse("opus", [{ id: "a1", name: "Boss", agent_runtime: "host" }])
    );
    const onOpen = vi.fn();

    renderWithQuery(<CloudUsage runtimes={[rt]} onOpen={onOpen} />);

    const row = await screen.findByTestId("cloud-usage-row-opus");
    row.click();
    expect(onOpen).toHaveBeenCalledWith(rt);

    expect(screen.queryByTestId("state-dot")).not.toBeInTheDocument();
    expect(document.querySelectorAll('[class*="rounded-full"]')).toHaveLength(0);
  });

  it("renders nothing when there are no cloud runtimes", () => {
    const { container } = renderWithQuery(<CloudUsage runtimes={[]} onOpen={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("suppresses the collapse row while an agent query is still pending, instead of flashing an inflated count", async () => {
    const bound = makeRuntime({ slug: "grok", display_name: "Grok", model_identifier: "grok-4.5" });
    const pendingUnbound = makeRuntime({ slug: "kimi", display_name: "Kimi", model_identifier: "kimi-k3" });
    let resolveKimi: (v: RuntimeAgentsResponse) => void = () => {};
    vi.spyOn(api.runtimes.db, "agents").mockImplementation((slug) => {
      if (slug === "grok") {
        return Promise.resolve(
          agentsResponse("grok", [{ id: "a1", name: "Grok", agent_runtime: "cli-bridge" }])
        );
      }
      return new Promise((resolve) => {
        resolveKimi = resolve;
      });
    });

    renderWithQuery(<CloudUsage runtimes={[bound, pendingUnbound]} onOpen={() => {}} />);

    await screen.findByTestId("cloud-usage-row-grok");
    // kimi's query is still pending — the collapse row must not appear yet
    // (not even with a wrong/inflated count), or it flickers 0 → 1 → shrinks.
    expect(screen.queryByTestId("cloud-usage-collapse-toggle")).not.toBeInTheDocument();

    resolveKimi(agentsResponse("kimi", []));

    const toggle = await screen.findByTestId("cloud-usage-collapse-toggle");
    expect(toggle).toHaveTextContent("1");
  });
});
