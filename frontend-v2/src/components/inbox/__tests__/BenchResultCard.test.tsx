/**
 * Bench W1 (package D): a finished test lands in the Inbox as
 * Approval(action_type="bench_result", payload {test_id, title}). The item
 * says "Test fertig", links to the result page (2 taps from the Inbox) and
 * leaves the Inbox once opened or marked done (resolve has no side effect).
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApprovalCard } from "../ApprovalCard";
import { benchResultHref } from "@/lib/benchLinks";
import type { Approval } from "@/lib/types";

const approval = (payload: Record<string, unknown> | null): Approval =>
  ({
    id: "ap-1",
    board_id: "b1",
    agent_id: "agent-1",
    action_type: "bench_result",
    description: "Bench test finished",
    status: "pending",
    created_at: new Date().toISOString(),
    resolved_at: null,
    resolver_note: null,
    failure_reason: null,
    expires_at: null,
    confidence: null,
    autonomy_level: null,
    task_id: null,
    payload,
  }) as Approval;

describe("Inbox — Test fertig (bench_result)", () => {
  it("names the test and links to its result", () => {
    render(<ApprovalCard approval={approval({ test_id: "t-7", title: "Pagoda Garden" })} onResolve={vi.fn()} />);
    const card = screen.getByTestId("bench-result-card");
    expect(card).toHaveTextContent("Test done");
    expect(card).toHaveTextContent("Pagoda Garden");
    expect(screen.getByRole("link", { name: "View result" })).toHaveAttribute("href", benchResultHref("t-7"));
    // no approve/reject vocabulary on a notice
    expect(screen.queryByText("Approve")).toBeNull();
    expect(screen.queryByText("Reject")).toBeNull();
  });

  it("opening the result or 'Done' clears the item", async () => {
    const onResolve = vi.fn();
    render(<ApprovalCard approval={approval({ test_id: "t-7", title: "Pagoda Garden" })} onResolve={onResolve} />);
    const link = screen.getByRole("link", { name: "View result" });
    link.addEventListener("click", (e) => e.preventDefault());
    await userEvent.click(link);
    expect(onResolve).toHaveBeenCalledWith("approved");
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onResolve).toHaveBeenCalledTimes(2);
  });

  it("without a test id: still a quiet notice, no dead link", () => {
    render(<ApprovalCard approval={approval(null)} onResolve={vi.fn()} />);
    expect(screen.getByTestId("bench-result-card")).toHaveTextContent("Benchmark test");
    expect(screen.queryByRole("link")).toBeNull();
  });
});
