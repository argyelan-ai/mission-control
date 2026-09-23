/**
 * ConfirmDialog — the Esc that OPENS the dialog must not also close it.
 *
 * CreateTaskModal opens "Discard draft?" from its own Esc handler. In a real
 * browser React commits that update (and runs the dialog's effects) in the
 * microtask checkpoint right after the root listener returns — while the
 * same keydown is still on its way to window. The dialog's window listener,
 * added in that effect, then saw the same Esc and cancelled at once: the
 * dialog opened and closed in one keystroke.
 *
 * jsdom runs no microtask checkpoint between listeners, so the harness
 * reproduces the browser order with flushSync in a document listener:
 * render + effects happen before the event reaches window.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { useEffect, useState } from "react";
import { flushSync } from "react-dom";
import { ConfirmDialog } from "../ConfirmDialog";

function Harness({ onCancel }: { onCancel: () => void }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "Escape") flushSync(() => setOpen(true));
    };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, []);
  return (
    <ConfirmDialog
      open={open}
      title="Discard draft?"
      body="body"
      onConfirm={() => setOpen(false)}
      onCancel={() => {
        onCancel();
        setOpen(false);
      }}
    />
  );
}

const dialog = () => screen.queryByRole("dialog", { name: "Discard draft?" });

describe("ConfirmDialog — Esc that opens it", () => {
  let restore: (() => void) | undefined;
  afterEach(() => restore?.());

  it("stays open after the keystroke that opened it", () => {
    // flushSync inside a native listener is outside act(); silence the
    // act-environment warning for this deliberately browser-like dispatch.
    const g = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
    const prev = g.IS_REACT_ACT_ENVIRONMENT;
    g.IS_REACT_ACT_ENVIRONMENT = false;
    restore = () => { g.IS_REACT_ACT_ENVIRONMENT = prev; };

    const onCancel = vi.fn();
    render(<Harness onCancel={onCancel} />);
    expect(dialog()).toBeNull();

    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(dialog()).not.toBeNull();
    // The opening Esc reached the dialog's window listener too; it must not
    // count as "keep editing".
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("a later Esc (not the opening one) still cancels", async () => {
    function Plain() {
      const [open, setOpen] = useState(true);
      return (
        <ConfirmDialog open={open} title="Discard draft?" body="b"
          onConfirm={() => setOpen(false)} onCancel={() => setOpen(false)} />
      );
    }
    render(<Plain />);
    expect(dialog()).not.toBeNull();
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(dialog()).toBeNull());
  });
});
