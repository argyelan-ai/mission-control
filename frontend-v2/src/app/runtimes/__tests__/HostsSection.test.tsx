import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HostsSection } from "../HostsSection";
import { api } from "@/lib/api";
import type { Device, Host } from "@/lib/types";

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
  ssh_credential_id: null,
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

/** Ein Gerät, wie /nodes/devices es liefert. Nur je gepaarte Boxen stehen
 *  dort — ein Host ohne node-agent taucht gar nicht auf. */
const makeDevice = (over: Partial<Device> = {}): Device => ({
  host_id: "host-1",
  slug: "gpu-box-1",
  display_name: "GPU Box 1",
  has_agent: true,
  desired_state: { gpu_mode: "eco" },
  device_state: {
    gpu_mode: "eco",
    gpu_clock_mhz: 1989,
    gpu_power_w: 33,
    gpu_temp_c: 63,
    min_free_kbytes: 5242880,
    oom_guard: "active",
    latency_tune: true,
    mtu: { iface: "enP7s7", value: 9000 },
    applied_at: "2026-09-01T00:12:00Z",
    last_error: null,
  },
  device_state_updated_at: "2026-09-01T00:12:00Z",
  agent_last_seen_at: "2026-09-01T00:12:00Z",
  status: "green",
  reason: "in_sync",
  diff: [],
  last_error: null,
  age_s: 4,
  ...over,
});

describe("HostsSection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mockStore.state.currentUser = null;
    // Standard: keine gepaarte Box. Wer den Schalter testet, überschreibt das.
    vi.spyOn(api.nodes, "devices").mockResolvedValue([]);
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
    expect(screen.queryByLabelText(/Delete host/)).toBeNull();
  });

  it("shows an empty state with 0 hosts (fresh install)", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] } as never);

    renderWithQuery(<HostsSection />);

    expect(await screen.findByText(/No hosts registered/)).toBeInTheDocument();
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

    // Delete is destructive and now sits in the row overflow menu.
    await userEvent.click(await screen.findByTestId("host-more-gpu-box-1"));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));

    expect(
      await screen.findByText("Host hat 2 gebundene Runtimes — erst umbinden.")
    ).toBeInTheDocument();
  });

  // Der Schalter gehört in die Zeile des Geräts, das er steuert — und nur
  // dorthin. Ein SSH- oder WoL-Host ohne node-agent bekommt keinen, und auch
  // keine leere Fläche, wo einer sein könnte.
  it("puts the mode switch inside the row of a paired box only", async () => {
    mockStore.state.currentUser = { id: "u1", email: "a@b.c", name: "Admin", role: "admin" };
    vi.spyOn(api.hosts, "list").mockResolvedValue([
      makeHost(),
      makeHost({ id: "host-2", slug: "wol-box", display_name: "WoL Box", kind: "flask_wol" }),
    ]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] } as never);
    vi.spyOn(api.nodes, "devices").mockResolvedValue([makeDevice()]);

    renderWithQuery(<HostsSection />);

    const rows = await screen.findAllByTestId("host-row");
    const paired = rows.find((r) => r.getAttribute("data-slug") === "gpu-box-1")!;
    const unpaired = rows.find((r) => r.getAttribute("data-slug") === "wol-box")!;

    await waitFor(() =>
      expect(within(paired).getByTestId("device-control")).toBeInTheDocument(),
    );
    expect(within(paired).getByTestId("compact-mode-eco")).toHaveAttribute("aria-checked", "true");
    // Die Zeile ohne Agent bleibt exakt wie vorher
    expect(within(unpaired).queryByTestId("device-control")).toBeNull();
  });

  it("carries the traffic light and the live readings in the row head", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([makeHost()]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] } as never);
    vi.spyOn(api.nodes, "devices").mockResolvedValue([
      makeDevice({ status: "yellow", reason: "stale" }),
    ]);

    renderWithQuery(<HostsSection />);

    const row = await screen.findByTestId("host-row");
    await waitFor(() =>
      expect(within(row).getByTestId("device-status-chip")).toHaveTextContent("no report"),
    );
    // Takt · Watt · Temperatur, dort wo der Operator den Zustand sucht
    expect(within(row).getByTitle("1989 MHz · 33.0 W · 63 °C")).toBeInTheDocument();
  });

  it("shows no switch on a disabled host, even when it is paired", async () => {
    mockStore.state.currentUser = { id: "u1", email: "a@b.c", name: "Admin", role: "admin" };
    vi.spyOn(api.hosts, "list").mockResolvedValue([makeHost({ enabled: false })]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] } as never);
    vi.spyOn(api.nodes, "devices").mockResolvedValue([makeDevice()]);

    renderWithQuery(<HostsSection />);

    const row = await screen.findByTestId("host-row");
    expect(within(row).queryByTestId("device-control")).toBeNull();
  });
});
