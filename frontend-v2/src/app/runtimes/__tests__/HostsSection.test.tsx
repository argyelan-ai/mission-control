import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HostsSection } from "../HostsSection";
import { api } from "@/lib/api";
import type { Host } from "@/lib/types";

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
});
