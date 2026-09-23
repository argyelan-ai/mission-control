import { describe, it, expect, vi } from "vitest";
import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApprovalCard } from "../ApprovalCard";
import type { Approval } from "@/lib/types";

// "Cancel task" on a blocker card fails the task and unassigns the agent
// (approvals.py: blocker_decision + rejected → failed). It sat 8 px next to
// "Unblock" and fired on the first tap.

const blocker = {
  id: "a1",
  board_id: "b1",
  agent_id: "agent-1",
  action_type: "blocker_decision",
  description: "Blocked on missing credentials",
  status: "pending",
  created_at: new Date().toISOString(),
  resolved_at: null,
  resolver_note: null,
  failure_reason: null,
  expires_at: null,
  confidence: null,
  autonomy_level: "L2",
  task_id: "t1",
  payload: { blocker_type: "missing_info" },
} as Approval;

describe("ApprovalCard — Cancel task asks first", () => {
  it("does not resolve on the first click", async () => {
    const onResolve = vi.fn();
    render(<ApprovalCard approval={blocker} onResolve={onResolve} />);
    await userEvent.click(screen.getByRole("button", { name: /Cancel task/ }));
    expect(onResolve).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toHaveTextContent(/Work in progress is lost/);
  });

  it("keep-task closes the dialog without resolving", async () => {
    const onResolve = vi.fn();
    render(<ApprovalCard approval={blocker} onResolve={onResolve} />);
    await userEvent.click(screen.getByRole("button", { name: /Cancel task/ }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Keep task" }));
    expect(onResolve).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("confirming rejects with the typed note", async () => {
    const onResolve = vi.fn();
    render(<ApprovalCard approval={blocker} onResolve={onResolve} />);
    await userEvent.type(screen.getByRole("textbox"), "not needed anymore");
    await userEvent.click(screen.getByRole("button", { name: /Cancel task/ }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Yes, cancel task" }));
    expect(onResolve).toHaveBeenCalledWith("rejected", "not needed anymore");
  });

  it("Unblock still resolves directly", async () => {
    const onResolve = vi.fn();
    render(<ApprovalCard approval={blocker} onResolve={onResolve} />);
    await userEvent.click(screen.getByRole("button", { name: /Unblock/ }));
    expect(onResolve).toHaveBeenCalledWith("approved", undefined);
  });

  it("keeps the two actions apart (16 px) and thumb-sized on phones", () => {
    render(<ApprovalCard approval={blocker} onResolve={vi.fn()} />);
    const cancel = screen.getByRole("button", { name: /Cancel task/ });
    expect(cancel.parentElement!.className).toMatch(/\bgap-4\b/);
    expect(cancel.className).toMatch(/min-h-\[44px\]/);
    expect(screen.getByRole("button", { name: /Unblock/ }).className).toMatch(/min-h-\[44px\]/);
  });
});
