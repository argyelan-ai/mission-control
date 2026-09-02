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
    expect(screen.queryByRole("button", { name: "Add device" })).toBeNull();
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

    expect(await screen.findByRole("button", { name: "Add device" })).toBeInTheDocument();

    // Delete is destructive and now sits in the row overflow menu.
    await userEvent.click(await screen.findByTestId("host-more-gpu-box-1"));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));

    expect(
      await screen.findByText("Host hat 2 gebundene Runtimes — erst umbinden.")
    ).toBeInTheDocument();
  });

  // ── „Gerät hinzufügen": ein Knopf, vier Wege ──────────────────────────────

  async function renderAsAdmin(hosts: Host[] = [makeHost()]) {
    mockStore.state.currentUser = { id: "u1", email: "a@b.c", name: "Admin", role: "admin" };
    vi.spyOn(api.hosts, "list").mockResolvedValue(hosts);
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] } as never);
    renderWithQuery(<HostsSection />);
    return await screen.findByRole("button", { name: "Add device" });
  }

  it("shows exactly one add button — the four old entry points are gone from the header", async () => {
    await renderAsAdmin();
    expect(screen.queryByRole("button", { name: "Add box" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Host" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Auto-onboard device" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Device reports in by itself" })).toBeNull();
  });

  it("the chooser asks for the situation and offers all three routes plus the manual side path", async () => {
    const addBtn = await renderAsAdmin();
    await userEvent.click(addBtn);

    const dialog = await screen.findByRole("dialog", { name: "Add device" });
    expect(dialog).toBeInTheDocument();
    expect(screen.getByTestId("add-device-onboard")).toHaveTextContent("I have the box's username and password");
    expect(screen.getByTestId("add-device-pairing")).toHaveTextContent("I can work on the box myself");
    expect(screen.getByTestId("add-device-wizard")).toHaveTextContent("already reaches the box with a key");
    // The wizard needs a key the box already accepts — the sentence must say
    // where to go when the check fails, instead of leaving the operator in
    // reachable:false without an explanation.
    expect(screen.getByTestId("add-device-wizard")).toHaveTextContent(/If the test fails.*first path/);
    expect(screen.getByTestId("add-device-manual")).toHaveTextContent("Create entry manually");
  });

  it("routes: password → onboarding dialog", async () => {
    await userEvent.click(await renderAsAdmin());
    await userEvent.click(await screen.findByTestId("add-device-onboard"));
    expect(await screen.findByTestId("onboard-address")).toBeInTheDocument();
    // The chooser closes (framer-motion exit animation → waitFor).
    await waitFor(() => expect(screen.queryByTestId("add-device-onboard")).toBeNull());
  });

  it("routes: work on the box myself → pairing dialog", async () => {
    await userEvent.click(await renderAsAdmin());
    await userEvent.click(await screen.findByTestId("add-device-pairing"));
    expect(await screen.findByTestId("pairing-generate")).toBeInTheDocument();
  });

  it("routes: already reachable → box wizard", async () => {
    await userEvent.click(await renderAsAdmin());
    await userEvent.click(await screen.findByTestId("add-device-wizard"));
    expect(await screen.findByRole("dialog", { name: "Add box" })).toBeInTheDocument();
  });

  it("routes: side path → manual form (flask_wol / local only live here)", async () => {
    await userEvent.click(await renderAsAdmin());
    await userEvent.click(await screen.findByTestId("add-device-manual"));
    expect(await screen.findByText("Enter device manually")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Flask/WoL" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Local" })).toBeInTheDocument();
  });

  // ── Typ „Agent" im Formular gesperrt ─────────────────────────────────────

  it("manual form: kind 'agent' is not selectable and the hint points to pairing", async () => {
    await userEvent.click(await renderAsAdmin());
    await userEvent.click(await screen.findByTestId("add-device-manual"));
    await screen.findByText("Enter device manually");

    expect(screen.getByRole("button", { name: "SSH" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Agent" })).toBeNull();
    expect(screen.getByText(/Devices that report in by themselves can't be created here/)).toBeInTheDocument();

    // The hint's link jumps straight to the pairing route.
    await userEvent.click(screen.getByTestId("host-kind-agent-hint-link"));
    expect(await screen.findByTestId("pairing-generate")).toBeInTheDocument();
    expect(screen.queryByText("Enter device manually")).toBeNull();
  });

  it("manual form: creating saves the chosen kind (never 'agent')", async () => {
    const create = vi.spyOn(api.hosts, "create").mockResolvedValue(makeHost({ id: "new", kind: "local" }));
    await userEvent.click(await renderAsAdmin());
    await userEvent.click(await screen.findByTestId("add-device-manual"));
    await screen.findByText("Enter device manually");

    await userEvent.type(screen.getByLabelText("Slug"), "mini");
    await userEvent.type(screen.getByLabelText("Display name"), "Mac Mini");
    await userEvent.click(screen.getByRole("button", { name: "Local" }));
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toMatchObject({ slug: "mini", display_name: "Mac Mini", kind: "local" });
  });

  it("editing an existing agent host keeps kind='agent' locked — the row is not touched", async () => {
    const update = vi.spyOn(api.hosts, "update").mockResolvedValue(makeHost({ kind: "agent" }));
    await renderAsAdmin([makeHost({ id: "host-a", slug: "gx10", display_name: "GX10", kind: "agent", ssh_host: null })]);

    await userEvent.click(await screen.findByLabelText("Edit host GX10"));
    await screen.findByText("Edit host — GX10");

    expect(screen.getByTestId("host-kind-locked")).toHaveTextContent("Agent");
    expect(screen.queryByRole("button", { name: "SSH" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Agent" })).toBeNull();
    // No "create via pairing" hint on edit — nothing to redirect here.
    expect(screen.queryByTestId("host-kind-agent-hint-link")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update.mock.calls[0][0]).toBe("host-a");
    expect(update.mock.calls[0][1]).toMatchObject({ kind: "agent" });
  });
});
