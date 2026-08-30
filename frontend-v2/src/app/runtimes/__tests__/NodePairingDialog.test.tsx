import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NodePairingDialog } from "../NodePairingDialog";
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

    render(<NodePairingDialog onClose={vi.fn()} />);

    await userEvent.type(screen.getByTestId("pairing-display-name"), "GX10");
    await userEvent.click(screen.getByTestId("pairing-generate"));

    expect(await screen.findByTestId("pairing-code")).toHaveTextContent("ABCD1234");
    expect(screen.getByTestId("pairing-install-command")).toHaveTextContent("--pair ABCD1234 --install");
    expect(api.nodes.createPairingCode).toHaveBeenCalledWith({ display_name_hint: "GX10" });
  });

  it("copies the install command to the clipboard", async () => {
    vi.spyOn(api.nodes, "createPairingCode").mockResolvedValue({
      code: "WXYZ9876",
      expires_at: "2026-08-30T12:15:00Z",
      host_id: null,
      install_command: "sudo python3 /usr/local/bin/mc-node-agent.py --pair WXYZ9876 --install",
    });

    render(<NodePairingDialog onClose={vi.fn()} />);
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

    render(<NodePairingDialog onClose={vi.fn()} />);
    await userEvent.click(screen.getByTestId("pairing-generate"));

    expect(await screen.findByText("Requires admin role or higher")).toBeInTheDocument();
  });

  it("calls onClose from the close button", async () => {
    const onClose = vi.fn();
    render(<NodePairingDialog onClose={onClose} />);
    // Both the header icon button and the footer text button are labeled
    // "Close" — either one calling onClose proves the wiring.
    const [closeButton] = screen.getAllByRole("button", { name: "Close" });
    await userEvent.click(closeButton);
    expect(onClose).toHaveBeenCalled();
  });
});
