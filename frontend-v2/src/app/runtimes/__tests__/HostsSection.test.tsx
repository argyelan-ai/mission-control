import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HostMetricsBar, HostsSection } from "../HostsSection";
import { api } from "@/lib/api";
import type { Host, HostMetrics } from "@/lib/types";

// The real zustand store uses persist middleware (localStorage writes trip in
// jsdom) — a plain selector-mock is all HostsSection needs (currentUser.role).
const mockStore = vi.hoisted(() => ({
  state: { currentUser: null as { id: string; email: string; name: string; role: string } | null },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector: (s: typeof mockStore.state) => unknown) => selector(mockStore.state),
}));

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// Fixtures — placeholder IPs only (192.0.2.x, TEST-NET-1; public repo)
const makeHost = (over: Partial<Host> = {}): Host => ({
  id: "host-1",
  slug: "gpu-box-1",
  display_name: "GPU Box 1",
  kind: "ssh",
  ssh_host: "192.0.2.10",
  ssh_user: "operator",
  ssh_key_path: null,
  control_url: null,
  wol_mac_address: null,
  power_managed: false,
  notes: null,
  enabled: true,
  ui_order: 0,
  created_at: "2026-07-02T00:00:00Z",
  updated_at: "2026-07-02T00:00:00Z",
  ...over,
});

const SSH_METRICS: HostMetrics = {
  reachable: true,
  gpu_util_pct: 42,
  vram_used_mb: 8192,
  vram_total_mb: 24576,
  gpu_temp_c: 61,
  ram_used_mb: 16384,
  ram_total_mb: 65536,
};

describe("HostMetricsBar", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders nothing when there are 0 hosts", async () => {
    const listSpy = vi.spyOn(api.hosts, "list").mockResolvedValue([]);
    const metricsSpy = vi.spyOn(api.hosts, "metrics").mockResolvedValue(SSH_METRICS);

    const { container } = renderWithQuery(<HostMetricsBar />);
    await waitFor(() => expect(listSpy).toHaveBeenCalled());

    expect(container.firstChild).toBeNull();
    expect(metricsSpy).not.toHaveBeenCalled();
  });

  it("renders one bar with metrics for a single enabled ssh host", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([makeHost()]);
    vi.spyOn(api.hosts, "metrics").mockResolvedValue(SSH_METRICS);

    renderWithQuery(<HostMetricsBar />);

    expect(await screen.findByText("GPU Box 1")).toBeInTheDocument();
    expect(await screen.findByText("42%")).toBeInTheDocument();
    expect(screen.getByText("61°C")).toBeInTheDocument();
  });

  it("renders one bar per enabled host with metrics — disabled and local hosts are skipped", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([
      makeHost(),
      makeHost({ id: "host-2", slug: "gpu-box-2", display_name: "GPU Box 2" }),
      makeHost({ id: "host-3", slug: "off-box", display_name: "Off Box", enabled: false }),
      makeHost({ id: "host-4", slug: "mc-local", display_name: "MC Local", kind: "local" }),
    ]);
    const metricsSpy = vi.spyOn(api.hosts, "metrics").mockResolvedValue(SSH_METRICS);

    renderWithQuery(<HostMetricsBar />);

    expect(await screen.findByText("GPU Box 1")).toBeInTheDocument();
    expect(await screen.findByText("GPU Box 2")).toBeInTheDocument();
    expect(screen.queryByText("Off Box")).toBeNull();
    expect(screen.queryByText("MC Local")).toBeNull();
    await waitFor(() => expect(metricsSpy).toHaveBeenCalledTimes(2));
    expect(metricsSpy).toHaveBeenCalledWith("host-1");
    expect(metricsSpy).toHaveBeenCalledWith("host-2");
  });

  it("shows an unreachable row when host metrics report reachable=false", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([makeHost()]);
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({ reachable: false });

    renderWithQuery(<HostMetricsBar />);

    expect(await screen.findByText(/GPU Box 1 nicht erreichbar/)).toBeInTheDocument();
  });

  // flask_wol: backend sets reachable = awake — a sleeping power-managed
  // box (ADR-042) is a NORMAL state and must not render as a failure.
  it("renders a sleeping flask_wol host as 'Schläft', not as unreachable", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([
      makeHost({ id: "host-w", slug: "wol-box", display_name: "WoL Box", kind: "flask_wol", control_url: "http://192.0.2.20:5555", power_managed: true }),
    ]);
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({
      reachable: false,
      awake: false,
      status: "asleep",
    } as HostMetrics);

    renderWithQuery(<HostMetricsBar />);

    expect(await screen.findByText("Schläft")).toBeInTheDocument();
    expect(screen.queryByText(/nicht erreichbar/)).toBeNull();
  });

  it("renders an awake flask_wol host with the German 'Wach' label", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([
      makeHost({ id: "host-w", slug: "wol-box", display_name: "WoL Box", kind: "flask_wol", control_url: "http://192.0.2.20:5555", power_managed: true }),
    ]);
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({
      reachable: true,
      awake: true,
      status: "awake",
    } as HostMetrics);

    renderWithQuery(<HostMetricsBar />);

    expect(await screen.findByText("Wach")).toBeInTheDocument();
    // raw backend status ("awake") must not leak through
    expect(screen.queryByText("awake")).toBeNull();
  });
});

describe("HostsSection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mockStore.state.currentUser = null;
  });

  it("renders host cards with name, kind badge and bound-runtimes count", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([
      makeHost(),
      makeHost({ id: "host-2", slug: "wol-box", display_name: "WoL Box", kind: "flask_wol", control_url: "http://192.0.2.20:5555" }),
    ]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue({
      runtimes: [
        { id: "rt-1", host: { id: "host-1", slug: "gpu-box-1", display_name: "GPU Box 1" } },
        { id: "rt-2", host: { id: "host-1", slug: "gpu-box-1", display_name: "GPU Box 1" } },
        { id: "rt-3", host: null },
      ],
    } as never);

    renderWithQuery(<HostsSection />);

    expect(await screen.findByText("GPU Box 1")).toBeInTheDocument();
    expect(screen.getByText("WoL Box")).toBeInTheDocument();
    expect(screen.getByText("SSH")).toBeInTheDocument();
    expect(screen.getByText("Flask/WoL")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("2 Runtimes")).toBeInTheDocument());
    expect(screen.getByText("0 Runtimes")).toBeInTheDocument();
    // non-admin: no add/edit/delete controls
    expect(screen.queryByRole("button", { name: /^Host$/ })).toBeNull();
    expect(screen.queryByLabelText(/löschen/)).toBeNull();
  });

  it("shows an empty state with 0 hosts (fresh install)", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] } as never);

    renderWithQuery(<HostsSection />);

    expect(await screen.findByText(/Keine Hosts registriert/)).toBeInTheDocument();
  });

  it("admin sees the add button and gets the 409 guard message on delete", async () => {
    mockStore.state.currentUser = { id: "u1", email: "a@b.c", name: "Admin", role: "admin" };
    vi.spyOn(api.hosts, "list").mockResolvedValue([makeHost()]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] } as never);
    vi.spyOn(api.hosts, "delete").mockRejectedValue(
      new Error('API 409: {"detail":"Host hat 2 gebundene Runtimes — erst umbinden."}')
    );

    renderWithQuery(<HostsSection />);

    expect(await screen.findByRole("button", { name: "Host" })).toBeInTheDocument();

    const deleteBtn = await screen.findByLabelText("Host GPU Box 1 löschen");
    await userEvent.click(deleteBtn);

    expect(
      await screen.findByText("Host hat 2 gebundene Runtimes — erst umbinden.")
    ).toBeInTheDocument();
  });
});
