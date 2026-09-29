/**
 * The two dialogs that still bound their own window Esc listener with the
 * caller's (per-render) handler as effect dependency: ConfirmDialog and the
 * modal TaskDetailPanel. With another window keydown listener that re-renders
 * the page mid-press (see browserKeydownCheckpoints.ts), a real Esc must still
 * reach them.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEffect, useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import TaskDetailPanel from "@/components/task/TaskDetailPanel";
import type { Agent, Task } from "@/lib/types";
import { emulateBrowserKeydownCheckpoints } from "./browserKeydownCheckpoints";

vi.mock("@/components/task/TaskDetailBody", () => ({
  TaskDetailBody: () => <div>body</div>,
}));

/** Re-renders on every keydown (a window listener registered BEFORE the
 *  dialog's), then hands the dialog a fresh inline handler. */
function useRerenderOnKeydown() {
  const [, setTick] = useState(0);
  useEffect(() => {
    const onKey = () => setTick((t) => t + 1);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

function ConfirmPage({ onCancelled }: { onCancelled: () => void }) {
  useRerenderOnKeydown();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      <ConfirmDialog open={open} title="Delete job" body="b"
        onConfirm={() => setOpen(false)}
        onCancel={() => { onCancelled(); setOpen(false); }} />
    </>
  );
}

function TaskPage({ onClosed }: { onClosed: () => void }) {
  useRerenderOnKeydown();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      {open && (
        <TaskDetailPanel task={{ id: "t1" } as Task} agents={[] as Agent[]} boardId="b1"
          variant="modal" onClose={() => { onClosed(); setOpen(false); }} />
      )}
    </>
  );
}

let browser: ReturnType<typeof emulateBrowserKeydownCheckpoints>;
beforeEach(() => { browser = emulateBrowserKeydownCheckpoints(); });
afterEach(() => browser.restore());

describe("a real Esc reaches the remaining dialogs while the page re-renders mid-press", () => {
  it("cancels the ConfirmDialog", async () => {
    const onCancelled = vi.fn();
    render(<ConfirmPage onCancelled={onCancelled} />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("dialog", { name: "Delete job" })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    await browser.settled();

    expect(onCancelled).toHaveBeenCalledTimes(1);
  });

  it("closes the modal TaskDetailPanel", async () => {
    const onClosed = vi.fn();
    render(<TaskPage onClosed={onClosed} />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("dialog", { name: "Task details" })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    await browser.settled();

    expect(onClosed).toHaveBeenCalledTimes(1);
  });

  it("does not close the TaskDetailPanel while a menu inside it is open", async () => {
    const onClosed = vi.fn();
    render(<TaskPage onClosed={onClosed} />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    const menu = document.createElement("div");
    menu.setAttribute("role", "menu");
    document.body.appendChild(menu);
    try {
      await userEvent.keyboard("{Escape}");
      await browser.settled();
      expect(onClosed).not.toHaveBeenCalled();
    } finally {
      menu.remove();
    }
  });
});
