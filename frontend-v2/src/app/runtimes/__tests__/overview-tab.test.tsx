import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { OverviewTab } from "../OverviewTab";
import { api } from "@/lib/api";
import type { Runtime, Host } from "@/lib/types";

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

function makeHost(over: Partial<Host>): Host {
  return {
    id: over.slug ?? "h", slug: "h", display_name: "H", kind: "ssh",
    ssh_host: null, ssh_user: null, ssh_key_path: null, control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true,
    ui_order: 0, created_at: "", updated_at: "",
    ...over,
  };
}

describe("OverviewTab", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [
      makeRuntime({ slug: "anthropic-claude-opus", display_name: "Anthropic Opus", runtime_type: "cloud", host: null, state: "ready" }),
      makeRuntime({ slug: "qwen-general", display_name: "Qwen General", host: { id: "spark", slug: "spark", display_name: "Spark" }, state: "stopped" }),
    ]});
    vi.spyOn(api.runtimes, "liveStatus").mockResolvedValue({ live: {}, watcher_enabled: true, interval: 30 });
    vi.spyOn(api.hosts, "list").mockResolvedValue([makeHost({ slug: "spark", display_name: "Spark" })]);
    vi.spyOn(api.lmstudio, "list").mockResolvedValue({ models: [], reachable: true });
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "x", count: 0, agents: [] });
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({ reachable: false });
  });

  it("shows cloud runtimes that the old page hid", async () => {
    renderWithQuery(<OverviewTab />);
    await waitFor(() => expect(screen.getByText("Anthropic Opus")).toBeInTheDocument());
    expect(screen.getByText("Cloud")).toBeInTheDocument();
  });

  it("groups by host and shows the summary line", async () => {
    renderWithQuery(<OverviewTab />);
    await waitFor(() => expect(screen.getByText("Spark")).toBeInTheDocument());
    expect(screen.getByText(/1 active/)).toBeInTheDocument();
    expect(screen.getByText(/1 stopped/)).toBeInTheDocument();
  });

  it("opens the detail panel on card click", async () => {
    renderWithQuery(<OverviewTab />);
    // RuntimeListCard renders a stopped runtime's name + type as one combined
    // text node ("Qwen General · vLLM Docker") — match by regex, not exact string.
    await waitFor(() => expect(screen.getByText(/Qwen General/)).toBeInTheDocument());
    screen.getByRole("button", { name: /Qwen General/ }).click();
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
  });

  it("keeps the detail panel in sync after the runtimes list refetches (no frozen snapshot)", async () => {
    const stopped = makeRuntime({
      slug: "qwen-general", display_name: "Qwen General",
      host: { id: "spark", slug: "spark", display_name: "Spark" }, state: "stopped",
    });
    const nowReady: Runtime = { ...stopped, state: "ready" };

    vi.spyOn(api.runtimes, "list")
      .mockResolvedValueOnce({ runtimes: [stopped] })
      .mockResolvedValue({ runtimes: [nowReady] });

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={qc}>
        <OverviewTab />
      </QueryClientProvider>
    );

    await waitFor(() => expect(screen.getByText(/Qwen General/)).toBeInTheDocument());
    screen.getByRole("button", { name: /Qwen General/ }).click();
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
    expect(screen.getByText("Stopped")).toBeInTheDocument();

    // Simulate the 15s refetch (or a post-mutation invalidate) picking up the
    // new state — the panel must re-derive from the fresh list, not show the
    // stale snapshot captured at open time.
    await qc.refetchQueries({ queryKey: ["runtimes"] });

    await waitFor(() => expect(screen.getByText("Ready")).toBeInTheDocument());
    expect(screen.queryByText("Stopped")).not.toBeInTheDocument();
  });

  it("shows an empty state instead of the summary line when there are 0 runtimes", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] });
    vi.spyOn(api.hosts, "list").mockResolvedValue([]);

    renderWithQuery(<OverviewTab />);

    expect(await screen.findByText("No runtimes configured.")).toBeInTheDocument();
    expect(screen.queryByText(/active/)).not.toBeInTheDocument();
    expect(screen.queryByText("Cloud")).not.toBeInTheDocument();
  });

  it("skips the metrics bar for local hosts and skips the whole section for disabled hosts", async () => {
    // A single unrelated cloud runtime keeps the list non-empty (finding 7's
    // global empty state must not swallow this scenario) without binding to
    // either host under test.
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [
      makeRuntime({ slug: "anthropic-claude-opus", display_name: "Anthropic Opus", runtime_type: "cloud", host: null, state: "ready" }),
    ]});
    vi.spyOn(api.hosts, "list").mockResolvedValue([
      makeHost({ slug: "mc-local", display_name: "MC Local", kind: "local", ui_order: 1 }),
      makeHost({ slug: "off-box", display_name: "Off Box", enabled: false, ui_order: 2 }),
    ]);
    const metricsSpy = vi.spyOn(api.hosts, "metrics").mockResolvedValue({ reachable: true, gpu_util_pct: 10 });

    renderWithQuery(<OverviewTab />);

    expect(await screen.findByText("MC Local")).toBeInTheDocument();
    expect(screen.queryByText("Off Box")).not.toBeInTheDocument();
    expect(metricsSpy).not.toHaveBeenCalled();
  });

  it("does not dim an empty host section (only dims when the host has runtimes, all non-ready)", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [
      makeRuntime({ slug: "anthropic-claude-opus", display_name: "Anthropic Opus", runtime_type: "cloud", host: null, state: "ready" }),
    ]});
    vi.spyOn(api.hosts, "list").mockResolvedValue([
      makeHost({ slug: "porsche", display_name: "PORSCHE", power_managed: true }),
    ]);

    renderWithQuery(<OverviewTab />);

    const emptyLine = await screen.findByText("No runtimes on this host.");
    // Walk up to the HostSection's dimmable wrapper and confirm it's not dimmed.
    expect(emptyLine.parentElement?.className ?? "").not.toContain("opacity-60");
  });
});
