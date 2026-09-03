import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NodePairingDialog, installCommandRows, installLabelParts } from "../NodePairingDialog";
import { api } from "@/lib/api";

describe("NodePairingDialog", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
  });

  it("generates a code and shows the install command", async () => {
    vi.spyOn(api.nodes, "createPairingCode").mockResolvedValue({
      code: "ABCD1234",
      expires_at: "2026-08-30T12:15:00Z",
      host_id: null,
      install_command: "sudo curl -fsSL https://mc.tailnet-name.ts.net/api/v1/nodes/agent-script -o /usr/local/bin/mc-node-agent.py && sudo python3 /usr/local/bin/mc-node-agent.py --mc-url https://mc.tailnet-name.ts.net --pair ABCD1234 --install",
    });

    render(<NodePairingDialog open onClose={vi.fn()} />);

    await userEvent.type(screen.getByTestId("pairing-display-name"), "box-c");
    await userEvent.click(screen.getByTestId("pairing-generate"));

    expect(await screen.findByTestId("pairing-code")).toHaveTextContent("ABCD1234");
    expect(screen.getByTestId("pairing-install-command")).toHaveTextContent("--pair ABCD1234 --install");
    // P2: ohne Eingabe geht die SSH-Adresse als null mit (nicht als "").
    // hosts.list nicht gemockt → Bestand unbekannt → KEIN stiller Vorschlag, role null.
    expect(api.nodes.createPairingCode).toHaveBeenCalledWith({ display_name_hint: "box-c", ssh_host: null, role: null });
  });

  // ── P2: SSH-Adresse + Install-Befehle je Netz ─────────────────────────────

  it("P2: sends the SSH address with the pairing request and explains what it is for", async () => {
    vi.spyOn(api.nodes, "createPairingCode").mockResolvedValue({
      code: "AAAA1111", expires_at: "2026-09-02T12:15:00Z", host_id: null,
      install_command: "sudo python3 /usr/local/bin/mc-node-agent.py --pair AAAA1111 --install",
    });
    render(<NodePairingDialog open onClose={vi.fn()} />);

    expect(screen.getByText(/Without SSH access the box only reports in/)).toBeInTheDocument();
    await userEvent.type(screen.getByTestId("pairing-display-name"), "box-b");
    await userEvent.type(screen.getByTestId("pairing-ssh-host"), "192.0.2.22");
    await userEvent.click(screen.getByTestId("pairing-generate"));
    await screen.findByTestId("pairing-code");

    expect(api.nodes.createPairingCode).toHaveBeenCalledWith({ display_name_hint: "box-b", ssh_host: "192.0.2.22", role: null });
  });

  it("P2: unknown inventory (list fails) → no suggestion, nothing preselected, role null is sent — sending is NOT blocked", async () => {
    vi.spyOn(api.hosts, "list").mockRejectedValue(new Error("API 500"));
    vi.spyOn(api.nodes, "createPairingCode").mockResolvedValue({
      code: "FFFF6666", expires_at: "2026-09-02T12:15:00Z", host_id: null,
      install_command: "sudo python3 /usr/local/bin/mc-node-agent.py --pair FFFF6666 --install",
    });
    render(<NodePairingDialog open onClose={vi.fn()} />);
    await waitFor(() => expect(api.hosts.list).toHaveBeenCalled());

    expect(screen.getByTestId("pairing-role-head")).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("pairing-role-worker")).toHaveAttribute("aria-checked", "false");
    await userEvent.click(screen.getByTestId("pairing-generate"));
    await screen.findByTestId("pairing-code");
    expect(vi.mocked(api.nodes.createPairingCode).mock.calls[0][0]).toMatchObject({ role: null });
  });

  it("P2: empty inventory (0 boxes, list known) → suggestion 'head' is sent", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([]);
    vi.spyOn(api.nodes, "createPairingCode").mockResolvedValue({
      code: "GGGG7777", expires_at: "2026-09-02T12:15:00Z", host_id: null,
      install_command: "sudo python3 /usr/local/bin/mc-node-agent.py --pair GGGG7777 --install",
    });
    render(<NodePairingDialog open onClose={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByTestId("pairing-role-head")).toHaveAttribute("aria-checked", "true"),
    );
    await userEvent.click(screen.getByTestId("pairing-generate"));
    await screen.findByTestId("pairing-code");
    expect(vi.mocked(api.nodes.createPairingCode).mock.calls[0][0]).toMatchObject({ role: "head" });
  });

  it("P2: suggests 'worker' when a box exists, and a click on Head is what gets sent", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([{ id: "host-a", slug: "box-a" } as never]);
    vi.spyOn(api.nodes, "createPairingCode").mockResolvedValue({
      code: "DDDD4444", expires_at: "2026-09-02T12:15:00Z", host_id: null,
      install_command: "sudo python3 /usr/local/bin/mc-node-agent.py --pair DDDD4444 --install",
    });
    render(<NodePairingDialog open onClose={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByTestId("pairing-role-worker")).toHaveAttribute("aria-checked", "true"),
    );
    expect(screen.getByTestId("pairing-role-suggested")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("pairing-generate"));
    await screen.findByTestId("pairing-code");
    expect(vi.mocked(api.nodes.createPairingCode).mock.calls[0][0]).toMatchObject({ role: "worker" });
  });

  it("P2: the clicked role wins over the suggestion", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([{ id: "host-a", slug: "box-a" } as never]);
    vi.spyOn(api.nodes, "createPairingCode").mockResolvedValue({
      code: "EEEE5555", expires_at: "2026-09-02T12:15:00Z", host_id: null,
      install_command: "sudo python3 /usr/local/bin/mc-node-agent.py --pair EEEE5555 --install",
    });
    render(<NodePairingDialog open onClose={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByTestId("pairing-role-worker")).toHaveAttribute("aria-checked", "true"),
    );
    await userEvent.click(screen.getByTestId("pairing-role-head"));
    expect(screen.queryByTestId("pairing-role-suggested")).toBeNull();
    await userEvent.click(screen.getByTestId("pairing-generate"));
    await screen.findByTestId("pairing-code");
    expect(vi.mocked(api.nodes.createPairingCode).mock.calls[0][0]).toMatchObject({ role: "head" });
  });

  it("P2: lists every install command with its network label and its own copy button", async () => {
    vi.spyOn(api.nodes, "createPairingCode").mockResolvedValue({
      code: "BBBB2222", expires_at: "2026-09-02T12:15:00Z", host_id: null,
      install_command: "curl https://mc.tailnet-name.ts.net/agent | sh -s -- --pair BBBB2222",
      install_commands: [
        { label: "Tailscale", url: "https://mc.tailnet-name.ts.net", cmd: "curl https://mc.tailnet-name.ts.net/agent | sh -s -- --pair BBBB2222" },
        { label: "LAN", url: "http://192.0.2.5:8000", cmd: "curl http://192.0.2.5:8000/agent | sh -s -- --pair BBBB2222" },
      ],
    });
    render(<NodePairingDialog open onClose={vi.fn()} />);
    await userEvent.click(screen.getByTestId("pairing-generate"));
    await screen.findByTestId("pairing-code");

    expect(screen.getByTestId("pairing-install-label-0")).toHaveTextContent("on the tailnet");
    expect(screen.getByTestId("pairing-install-label-1")).toHaveTextContent("on the LAN");
    expect(screen.getByTestId("pairing-install-command")).toHaveTextContent("mc.tailnet-name.ts.net");
    expect(screen.getByTestId("pairing-install-command-1")).toHaveTextContent("192.0.2.5:8000");

    // Zweite Zeile kopieren → genau deren Befehl landet in der Zwischenablage.
    await userEvent.click(screen.getByTestId("pairing-copy-1"));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      "curl http://192.0.2.5:8000/agent | sh -s -- --pair BBBB2222",
    );
    expect(await screen.findByText("Copied!")).toBeInTheDocument();
    // Die erste Zeile zeigt weiterhin „Copy" — der Zustand ist je Zeile.
    expect(screen.getByTestId("pairing-copy")).toHaveTextContent("Copy");
  });

  it("P2: falls back to the single install_command when the list is missing (old backend)", async () => {
    vi.spyOn(api.nodes, "createPairingCode").mockResolvedValue({
      code: "CCCC3333", expires_at: "2026-09-02T12:15:00Z", host_id: null,
      install_command: "sudo python3 /usr/local/bin/mc-node-agent.py --pair CCCC3333 --install",
    });
    render(<NodePairingDialog open onClose={vi.fn()} />);
    await userEvent.click(screen.getByTestId("pairing-generate"));
    await screen.findByTestId("pairing-code");

    expect(screen.getByTestId("pairing-install-command")).toHaveTextContent("--pair CCCC3333 --install");
    expect(screen.queryByTestId("pairing-install-command-1")).toBeNull();
    expect(screen.queryByTestId("pairing-install-label-0")).toBeNull();
  });

  it("P2: installLabelParts — known backend labels map to i18n keys, counters survive, unknown stays raw", () => {
    expect(installLabelParts("Tailscale")).toEqual({ key: "pairingInstallLabelTailscale", suffix: "" });
    expect(installLabelParts("LAN 2")).toEqual({ key: "pairingInstallLabelLan", suffix: " 2" });
    expect(installLabelParts("Adresse")).toEqual({ key: "pairingInstallLabelAddress", suffix: "" });
    expect(installLabelParts("Öffentlich")).toEqual({ key: "pairingInstallLabelPublic", suffix: "" });
    expect(installLabelParts("Kabelnetz")).toEqual({ key: null, suffix: "" });
  });

  it("P2: installCommandRows — list wins, empty list and missing list fall back", () => {
    const base = { code: "X", expires_at: "", host_id: null, install_command: "one" };
    expect(installCommandRows(base)).toEqual([{ label: "", url: "", cmd: "one" }]);
    expect(installCommandRows({ ...base, install_commands: [] })).toEqual([{ label: "", url: "", cmd: "one" }]);
    expect(installCommandRows({ ...base, install_commands: [{ label: "LAN", url: "u", cmd: "two" }] }))
      .toEqual([{ label: "LAN", url: "u", cmd: "two" }]);
  });

  it("copies the install command to the clipboard", async () => {
    vi.spyOn(api.nodes, "createPairingCode").mockResolvedValue({
      code: "WXYZ9876",
      expires_at: "2026-08-30T12:15:00Z",
      host_id: null,
      install_command: "sudo python3 /usr/local/bin/mc-node-agent.py --pair WXYZ9876 --install",
    });

    render(<NodePairingDialog open onClose={vi.fn()} />);
    await userEvent.click(screen.getByTestId("pairing-generate"));
    await screen.findByTestId("pairing-code");

    await userEvent.click(screen.getByTestId("pairing-copy"));

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      "sudo python3 /usr/local/bin/mc-node-agent.py --pair WXYZ9876 --install"
    );
    expect(await screen.findByText("Copied!")).toBeInTheDocument();
  });

  it("shows the backend error message on failure", async () => {
    vi.spyOn(api.nodes, "createPairingCode").mockRejectedValue(
      new Error('API 403: {"detail":"Requires admin role or higher"}')
    );

    render(<NodePairingDialog open onClose={vi.fn()} />);
    await userEvent.click(screen.getByTestId("pairing-generate"));

    expect(await screen.findByText("Requires admin role or higher")).toBeInTheDocument();
  });

  it("calls onClose from the close button", async () => {
    const onClose = vi.fn();
    render(<NodePairingDialog open onClose={onClose} />);
    // Both the header icon button and the footer text button are labeled
    // "Close" — either one calling onClose proves the wiring.
    const [closeButton] = screen.getAllByRole("button", { name: "Close" });
    await userEvent.click(closeButton);
    expect(onClose).toHaveBeenCalled();
  });
});
