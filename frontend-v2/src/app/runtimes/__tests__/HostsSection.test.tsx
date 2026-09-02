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
  ssh_credential_id: null, role: null, fabric_ip: null,
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

  // ── P2: Rolle, SSH-Adresse, Fabric-Adresse ────────────────────────────────

  it("P2: manual form suggests 'head' for the first box and the hint says solo recipes ignore it", async () => {
    const create = vi.spyOn(api.hosts, "create").mockResolvedValue(makeHost({ id: "new" }));
    await userEvent.click(await renderAsAdmin([]));
    await userEvent.click(await screen.findByTestId("add-device-manual"));
    await screen.findByText("Enter device manually");

    expect(screen.getByTestId("host-role-head")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("host-role-worker")).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("host-role-suggested")).toBeInTheDocument();
    expect(screen.getByText(/Single-box recipes ignore the role/)).toBeInTheDocument();
    // Beim Anlegen gibt es kein „None" — die Vorbelegung ist immer gesetzt.
    expect(screen.queryByTestId("host-role-none")).toBeNull();

    await userEvent.type(screen.getByLabelText("Slug"), "box-a");
    await userEvent.type(screen.getByLabelText("Display name"), "Box A");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toMatchObject({ slug: "box-a", role: "head" });
  });

  it("P2: with a box already registered the manual form suggests 'worker' — and one click makes it 'head'", async () => {
    const create = vi.spyOn(api.hosts, "create").mockResolvedValue(makeHost({ id: "new" }));
    await userEvent.click(await renderAsAdmin([makeHost({ id: "host-a", slug: "box-a", display_name: "Box A" })]));
    await userEvent.click(await screen.findByTestId("add-device-manual"));
    await screen.findByText("Enter device manually");

    expect(screen.getByTestId("host-role-worker")).toHaveAttribute("aria-checked", "true");
    await userEvent.click(screen.getByTestId("host-role-head"));
    expect(screen.getByTestId("host-role-head")).toHaveAttribute("aria-checked", "true");

    await userEvent.type(screen.getByLabelText("Slug"), "box-b");
    await userEvent.type(screen.getByLabelText("Display name"), "Box B");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toMatchObject({ slug: "box-b", role: "head" });
  });

  it("P2: the settings mask PATCHes role, SSH address and fabric address for a paired (agent) box", async () => {
    const update = vi.spyOn(api.hosts, "update").mockResolvedValue(makeHost({ kind: "agent" }));
    await renderAsAdmin([
      makeHost({ id: "host-b", slug: "box-b", display_name: "Box B", kind: "agent", ssh_host: null, role: null, fabric_ip: null }),
    ]);

    await userEvent.click(await screen.findByLabelText("Edit host Box B"));
    await screen.findByText("Edit host — Box B");

    // Bearbeiten: nichts vorbelegt, „None" ist wählbar, kein Vorschlags-Satz.
    expect(screen.getByTestId("host-role-none")).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByTestId("host-role-suggested")).toBeNull();
    expect(screen.getByText(/Without SSH access the box only reports in/)).toBeInTheDocument();
    expect(screen.getByText(/Address the boxes use to reach each other/)).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("host-role-worker"));
    await userEvent.type(screen.getByTestId("host-field-ssh-host"), "192.0.2.22");
    await userEvent.type(screen.getByTestId("host-field-fabric-ip"), "192.0.2.122");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update.mock.calls[0][0]).toBe("host-b");
    expect(update.mock.calls[0][1]).toMatchObject({
      kind: "agent", role: "worker", ssh_host: "192.0.2.22", fabric_ip: "192.0.2.122",
    });
  });

  it("P2: the settings mask can clear a role again (→ null) and an emptied fabric address is sent as null", async () => {
    const update = vi.spyOn(api.hosts, "update").mockResolvedValue(makeHost());
    await renderAsAdmin([
      makeHost({ id: "host-a", slug: "box-a", display_name: "Box A", role: "head", fabric_ip: "192.0.2.111" }),
    ]);

    await userEvent.click(await screen.findByLabelText("Edit host Box A"));
    await screen.findByText("Edit host — Box A");
    expect(screen.getByTestId("host-role-head")).toHaveAttribute("aria-checked", "true");

    await userEvent.click(screen.getByTestId("host-role-none"));
    await userEvent.clear(screen.getByTestId("host-field-fabric-ip"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update.mock.calls[0][1]).toMatchObject({ role: null, fabric_ip: null });
  });

  it("P2: the device list shows the role chip only when a role is set", async () => {
    await renderAsAdmin([
      makeHost({ id: "host-a", slug: "box-a", display_name: "Box A", role: "head" }),
      makeHost({ id: "host-b", slug: "box-b", display_name: "Box B", role: null }),
    ]);
    await screen.findByText("Box A");
    const rows = screen.getAllByTestId("host-row");
    expect(rows[0]).toHaveTextContent("Head");
    expect(rows[1]).not.toHaveTextContent(/Head|Worker/);
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
