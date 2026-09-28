/**
 * The open approval inside the task detail: one surface, one primary action,
 * no repeated type badge or time (the state sentence has them), details and
 * the long report only after "Show full", cancel asks first.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { TaskApprovalCard } from "../TaskApprovalCard";
import { approvalFixture } from "@/lib/taskDetail/__tests__/fixtures";

const isPrimary = (el: HTMLElement) => el.style.background.includes("--color-accent");

describe("TaskApprovalCard", () => {
  const blocker = approvalFixture({
    description: "alpha is blocked at Sample task",
    autonomy_level: "L2",
    payload: {
      blocked_agent_name: "alpha",
      blocker_type: "decision_needed",
      question: "Wrap long lines or **scroll** sideways?",
      description: "",
      blocker_comment: "## Status\n- A: wrap\n- B: scroll",
    },
  });

  it("leads with the question as markdown; one primary (Unblock), Cancel task is quiet", () => {
    render(<TaskApprovalCard approval={blocker} onResolve={() => {}} />);
    const lead = screen.getByTestId("approval-lead");
    expect(within(lead).getByText("scroll").tagName).toBe("STRONG");
    expect(isPrimary(screen.getByRole("button", { name: "Unblock" }))).toBe(true);
    expect(isPrimary(screen.getByRole("button", { name: "Cancel task" }))).toBe(false);
    // No type badge, autonomy pill or time — the state sentence says who asks since when.
    expect(screen.queryByText("Blocker")).toBeNull();
    expect(screen.queryByText("L2")).toBeNull();
    expect(screen.queryByText(/ago/)).toBeNull();
  });

  it("the report and the blocker kind show only after Show full, rendered", () => {
    render(<TaskApprovalCard approval={blocker} onResolve={() => {}} />);
    expect(screen.queryByTestId("approval-details")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show full" }));
    const details = screen.getByTestId("approval-details");
    expect(within(details).getByText("Decision needed")).toBeInTheDocument();
    expect(within(details).getByRole("heading", { name: "Status" })).toBeInTheDocument();
    expect(details).not.toHaveTextContent("##");
  });

  it("Unblock sends the instruction; Cancel task asks first", () => {
    const onResolve = vi.fn();
    render(<TaskApprovalCard approval={blocker} onResolve={onResolve} />);
    fireEvent.change(screen.getByLabelText("Instruction for the agent..."), { target: { value: "Wrap them" } });
    fireEvent.click(screen.getByRole("button", { name: "Unblock" }));
    expect(onResolve).toHaveBeenCalledWith("approved", "Wrap them");
    fireEvent.click(screen.getByRole("button", { name: "Cancel task" }));
    expect(onResolve).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("a question offers its options as 44 px chips that fill the answer", () => {
    const q = approvalFixture({
      action_type: "clarification_question",
      payload: { question: "Which branch?", options: ["main", "develop"] },
    });
    const onResolve = vi.fn();
    render(<TaskApprovalCard approval={q} onResolve={onResolve} />);
    const chip = screen.getByRole("button", { name: "develop" });
    expect(chip.className).toContain("h-11");
    fireEvent.click(chip);
    fireEvent.click(screen.getByRole("button", { name: "Reply" }));
    expect(onResolve).toHaveBeenCalledWith("approved", "develop");
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
  });
});
