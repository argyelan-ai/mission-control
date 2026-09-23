/**
 * ResponsiveModal — click outside.
 *
 * The backdrop <div> sits on top of the wrapper, so the old
 * `e.target === e.currentTarget` check never fired: a click next to the
 * dialog did nothing. Non-form dialogs should close; form dialogs opt out via
 * `dismissOnOutside={false}` so a stray click cannot throw away typed input.
 */
import { describe, it, expect, vi } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
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

  // Guard: any <ResponsiveModal> whose own JSX holds a text field or a <form>
  // must opt out, so a new form dialog cannot silently reintroduce the
  // "stray click throws away what was typed" trap. File pickers upload on
  // pick and do not count as typed input.
  it("every ResponsiveModal with typed input opts out of click-outside dismissal", () => {
    const root = join(__dirname, "../../..");
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const full = join(dir, name);
        if (statSync(full).isDirectory()) {
          if (name !== "__tests__" && name !== "node_modules") walk(full);
          continue;
        }
        if (!full.endsWith(".tsx")) continue;
        const src = readFileSync(full, "utf-8");
        let from = 0;
        for (;;) {
          const start = src.indexOf("<ResponsiveModal", from);
          if (start < 0) break;
          const end = src.indexOf("</ResponsiveModal>", start);
          if (end < 0) break;
          const block = src.slice(start, end);
          const openTag = block.slice(0, block.indexOf(">") + 1);
          const typed =
            /<form\b|<textarea\b/.test(block) ||
            (block.match(/<input\b[^>]*/g) ?? []).some((tag) => !/type="file"/.test(tag));
          if (typed && !/dismissOnOutside=\{false\}/.test(openTag)) {
            offenders.push(`${full.slice(root.length + 1)}:${src.slice(0, start).split("\n").length}`);
          }
          from = end;
        }
      }
    };
    walk(root);
    expect(offenders).toEqual([]);
  });
});
