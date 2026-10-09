/**
 * ChatOptionsSheet vitest — the mobile chat screen's only control surface, so
 * every control the desktop toolbar offers has to be reachable here and has to
 * report the same intent.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatOptionsSheet } from "./ChatOptionsSheet";

function renderSheet(props: Partial<React.ComponentProps<typeof ChatOptionsSheet>> = {}) {
  return render(
    <ChatOptionsSheet
      open
      onClose={vi.fn()}
      centerView="chat"
      onCenterViewChange={vi.fn()}
      canChat
      headlessChat={false}
      detailLevel="normal"
      onDetailLevelChange={vi.fn()}
      onOpenPanel={vi.fn()}
      {...props}
    />
  );
}

describe("ChatOptionsSheet", () => {
  it("renders nothing while closed", () => {
    renderSheet({ open: false });
    expect(screen.queryByTestId("chat-options-sheet")).not.toBeInTheDocument();
  });

  it("offers both center views and marks the current one", () => {
    renderSheet({ centerView: "terminal" });
    expect(screen.getByRole("radio", { name: /Terminal/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: /Chat/ })).toHaveAttribute("aria-checked", "false");
  });

  it("switching the view reports it and closes the sheet", async () => {
    const onCenterViewChange = vi.fn();
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderSheet({ onCenterViewChange, onClose });

    await user.click(screen.getByRole("radio", { name: /Terminal/ }));
    expect(onCenterViewChange).toHaveBeenCalledWith("terminal");
    expect(onClose).toHaveBeenCalled();
  });

  it("disables the Chat row for an agent with no transcript", () => {
    renderSheet({ canChat: false, centerView: "terminal" });
    expect(screen.getByRole("radio", { name: /Chat/ })).toBeDisabled();
  });

  it("opens a side panel and closes the sheet", async () => {
    const onOpenPanel = vi.fn();
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderSheet({ onOpenPanel, onClose });

    await user.click(screen.getByRole("button", { name: "Diff" }));
    expect(onOpenPanel).toHaveBeenCalledWith("diff");
    expect(onClose).toHaveBeenCalled();
  });

  it("omits the Panels section when the caller offers none", () => {
    renderSheet({ onOpenPanel: undefined });
    expect(screen.queryByRole("button", { name: "Diff" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Browser" })).not.toBeInTheDocument();
  });

  it("offers the detail level in chat view and reports a change", async () => {
    const onDetailLevelChange = vi.fn();
    const user = userEvent.setup();
    renderSheet({ onDetailLevelChange });

    expect(screen.getByRole("radio", { name: "Normal" })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("radio", { name: "Verbose" }));
    expect(onDetailLevelChange).toHaveBeenCalledWith("verbose");
  });

  it("hides the detail level in terminal view — there is no timeline to filter", () => {
    renderSheet({ centerView: "terminal" });
    expect(screen.queryByRole("radio", { name: "Compact" })).not.toBeInTheDocument();
  });

  it("closes on the explicit close button", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderSheet({ onClose });

    await user.click(screen.getByRole("button", { name: "Close options" }));
    expect(onClose).toHaveBeenCalled();
  });

  /**
   * Operator report, 09.10.2026: a "Terminal" entry sat in this sheet for
   * every agent, including ACP-driven ones whose second console runs no
   * job — tapping it showed a checkmark but never changed the screen,
   * because ChatView's `effectiveView` ignores the stored choice for a
   * `headless_chat` agent (docs/specs/chat-over-acp.md). The desktop header
   * already withheld this toggle for such agents (ChatView.tsx); the sheet
   * did not. Proof #6 of that spec requires "no Chat/Terminal toggle" —
   * this closes the gap on the one surface that still showed it.
   */
  it("withholds the View section for a headless-chat (ACP) agent", () => {
    renderSheet({ headlessChat: true });
    expect(screen.queryByText("View")).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /Terminal/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /Chat/ })).not.toBeInTheDocument();
  });

  // Sabotage-Probe: without the flag the toggle must stand unchanged.
  it("still offers the View section for a non-headless agent", () => {
    renderSheet({ headlessChat: false });
    expect(screen.getByRole("radio", { name: /Terminal/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Chat/ })).toBeInTheDocument();
  });

  it("keeps Panels and Detail Level for a headless-chat agent", () => {
    renderSheet({ headlessChat: true, onOpenPanel: vi.fn() });
    expect(screen.getByRole("button", { name: "Diff" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Normal" })).toBeInTheDocument();
  });
});
