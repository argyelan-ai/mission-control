/**
 * ADR-100 §6: an approval raised soon after a session message names that
 * message's sender — "triggered by a message from X" — so the operator sees
 * where the wish came from. The brake card ("let it continue?") gets its own
 * label and both lead to the conversation.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ApprovalCard } from "../ApprovalCard";
import type { Approval } from "@/lib/types";

const mk = (o: Partial<Approval> = {}): Approval =>
  ({
    id: "a1", board_id: "b1", agent_id: "agent-1", action_type: "config_change",
    description: "Stop the engine on box one", status: "pending", created_at: new Date().toISOString(),
    resolved_at: null, resolver_note: null, failure_reason: null, expires_at: null, confidence: null,
    autonomy_level: null, task_id: null, payload: {}, ...o,
  }) as Approval;

describe("ApprovalCard — triggered by a message", () => {
  it("names the sender and links to the conversation", () => {
    render(
      <ApprovalCard
        approval={mk({ triggered_by: { from: "worker", thread_id: "th-9", message_id: "m1", at: "2026-11-02T14:00:00Z" } })}
        onResolve={vi.fn()}
      />,
    );
    const line = screen.getByTestId("approval-triggered-by");
    expect(line).toHaveTextContent("Triggered by a message from worker");
    expect(screen.getByRole("link", { name: "View conversation" })).toHaveAttribute(
      "href",
      "/inbox?tab=agents&c=th-9",
    );
  });

  it("an unknown sender still says a message came first", () => {
    render(
      <ApprovalCard
        approval={mk({ triggered_by: { from: null, thread_id: "th-9", message_id: "m1", at: "2026-11-02T14:00:00Z" } })}
        onResolve={vi.fn()}
      />,
    );
    expect(screen.getByTestId("approval-triggered-by")).toHaveTextContent("Triggered by a message from another session");
  });

  it("no trigger, no line (absent field from an older backend too)", () => {
    const { rerender } = render(<ApprovalCard approval={mk({ triggered_by: null })} onResolve={vi.fn()} />);
    expect(screen.queryByTestId("approval-triggered-by")).toBeNull();
    rerender(<ApprovalCard approval={mk()} onResolve={vi.fn()} />);
    expect(screen.queryByTestId("approval-triggered-by")).toBeNull();
    expect(screen.queryByRole("link", { name: "View conversation" })).toBeNull();
  });

  it("the brake card has a label of its own and leads to its conversation", () => {
    render(
      <ApprovalCard
        approval={mk({
          action_type: "comms_continue",
          description: "lead and worker exchanged 6 messages without you. Let the conversation continue?",
          payload: { thread_id: "th-4", between: ["lead", "worker"] },
        })}
        onResolve={vi.fn()}
      />,
    );
    expect(screen.getByText("Conversation brake")).toBeInTheDocument();
    expect(screen.queryByText("comms_continue")).toBeNull();
    expect(screen.getByRole("link", { name: "View conversation" })).toHaveAttribute(
      "href",
      "/inbox?tab=agents&c=th-4",
    );
  });

  it.each([
    ["credential_read", "Credential access"],
    ["config_change", "Config change"],
  ])("%s cards show a readable label, not the raw type", (actionType, label) => {
    render(<ApprovalCard approval={mk({ action_type: actionType })} onResolve={vi.fn()} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText(actionType)).toBeNull();
  });
});
