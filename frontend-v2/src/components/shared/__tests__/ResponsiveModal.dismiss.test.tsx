/**
 * ResponsiveModal — click outside.
 *
 * The backdrop <div> sits on top of the wrapper, so the old
 * `e.target === e.currentTarget` check never fired: a click next to the
 * dialog did nothing. Non-form dialogs should close; form dialogs opt out via
 * `dismissOnOutside={false}` so a stray click cannot throw away typed input.
 */
import { describe, it, expect, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, fireEvent } from "@testing-library/react";
import { ResponsiveModal } from "../ResponsiveModal";

function backdrop(): HTMLElement {
  const el = document.querySelector("[data-testid='responsive-modal-backdrop']");
  if (!el) throw new Error("backdrop not found");
  return el as HTMLElement;
}

describe("ResponsiveModal click outside", () => {
  it("closes on a backdrop click by default", () => {
    const onClose = vi.fn();
    render(<ResponsiveModal open onClose={onClose} aria-label="demo"><p>body</p></ResponsiveModal>);
    fireEvent.click(backdrop());
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not close on a click inside the panel", () => {
    const onClose = vi.fn();
    render(<ResponsiveModal open onClose={onClose} aria-label="demo"><p>body</p></ResponsiveModal>);
    fireEvent.click(screen.getByText("body"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("keeps a form dialog open on a backdrop click when dismissOnOutside is false", () => {
    const onClose = vi.fn();
    render(
      <ResponsiveModal open onClose={onClose} aria-label="demo" dismissOnOutside={false}>
        <p>body</p>
      </ResponsiveModal>,
    );
    fireEvent.click(backdrop());
    expect(onClose).not.toHaveBeenCalled();
    // Esc still closes — it is a deliberate key press, not a stray click.
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it.each([
    "components/loops/CreateLoopDialog.tsx",
    "app/repos/ImportRepoDialog.tsx",
    "app/runtimes/HostOnboardDialog.tsx",
    "app/runtimes/NodePairingDialog.tsx",
    "app/runtimes/AddDeviceDialog.tsx",
    "components/groupchat/CreateGroupModal.tsx",
  ])("form dialog %s opts out of click-outside dismissal", (rel) => {
    const src = readFileSync(join(__dirname, "../../..", rel), "utf-8");
    expect(src).toMatch(/dismissOnOutside=\{false\}/);
  });
});
