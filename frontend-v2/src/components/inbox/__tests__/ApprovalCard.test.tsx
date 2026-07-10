import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ApprovalCard } from "../ApprovalCard";
import type { Approval } from "@/lib/types";

const mkApproval = (overrides: Partial<Approval> = {}): Approval => ({
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
  task_id: null,
  payload: {},
  ...overrides,
} as Approval);

describe("ApprovalCard — markdown rendering", () => {
  it("renders blocker description as markdown: bold as <strong>, paragraphs separated", () => {
    render(
      <ApprovalCard
        approval={mkApproval({
          payload: {
            blocker_type: "missing_info",
            description: "First paragraph.\n\nSecond paragraph with **bold** text.",
          },
        })}
        onResolve={vi.fn()}
      />
    );

    const strong = screen.getByText("bold");
    expect(strong.tagName).toBe("STRONG");

    const paragraphs = screen.getAllByText(/paragraph/i).map((el) => el.closest("p"));
    const uniqueParagraphs = new Set(paragraphs.filter(Boolean));
    expect(uniqueParagraphs.size).toBe(2);

    // no literal markdown syntax leaking into rendered text
    const container = screen.getByText("First paragraph.").closest("div")!;
    expect(container.textContent).not.toContain("**");
  });

  it("renders blocker question as markdown with paragraph separation", () => {
    render(
      <ApprovalCard
        approval={mkApproval({
          payload: {
            blocker_type: "decision_needed",
            question: "Should we use option A?\n\nOr option B with **emphasis**?",
          },
        })}
        onResolve={vi.fn()}
      />
    );

    const strong = screen.getByText("emphasis");
    expect(strong.tagName).toBe("STRONG");

    const paragraphs = screen.getAllByText(/option/i).map((el) => el.closest("p"));
    const uniqueParagraphs = new Set(paragraphs.filter(Boolean));
    expect(uniqueParagraphs.size).toBe(2);
  });

  it("renders single newlines in agent freetext as hard line breaks (<br>)", () => {
    const { container } = render(
      <ApprovalCard
        approval={mkApproval({
          payload: {
            blocker_type: "missing_info",
            question: "Zeile 1\nZeile 2",
          },
        })}
        onResolve={vi.fn()}
      />
    );

    // Agents write single \n, not markdown blank-line paragraphs. Without
    // remark-breaks this renders as a soft break (a space) and the lines
    // visually collapse onto one line.
    const line1 = screen.getByText(/Zeile 1/);
    const paragraph = line1.closest("p")!;
    expect(paragraph.querySelector("br")).not.toBeNull();
    expect(container.textContent).toContain("Zeile 2");
  });

  it("renders clarification question as markdown, not literal asterisks", () => {
    render(
      <ApprovalCard
        approval={mkApproval({
          action_type: "clarification_question",
          payload: {
            question: "Which env?\n\nProd or **staging**?",
            options: ["Prod", "Staging"],
          },
        })}
        onResolve={vi.fn()}
      />
    );

    const strong = screen.getByText("staging");
    expect(strong.tagName).toBe("STRONG");

    const paragraphs = screen.getAllByText(/(env|Prod or)/i).map((el) => el.closest("p"));
    const uniqueParagraphs = new Set(paragraphs.filter(Boolean));
    expect(uniqueParagraphs.size).toBe(2);
  });
});
