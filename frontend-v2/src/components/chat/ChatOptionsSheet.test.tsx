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
});
