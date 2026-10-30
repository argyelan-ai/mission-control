/**
 * The running task's own context fill in its chat section (live 30.10.2026:
 * a new bench task ran in a fresh session at 3 % while the only figure in
 * the chat was the composer ring — the chat session's 29 %).
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ChatTaskStub } from "@/lib/chatTypes";
import { TaskCard, TaskSegment } from "../TaskSection";
import { stub, TASK_ID } from "./fixtures";

function segment(s: ChatTaskStub) {
  return render(
    <TaskSegment
      entry={{ kind: "segment", key: `seg:${TASK_ID}`, taskId: TASK_ID, first: true, head: "full", units: [] }}
      stub={s}
      detailLevel="normal"
      queuedLabel={() => ""}
    />,
  );
}

describe("chat task section — the task's context fill", () => {
  it("a running task shows its own fill in the state line, tokens in the tooltip", () => {
    segment(stub({ context: { tokens: 10_224, max: 393_216 } }));
    const state = screen.getByTestId("chat-task-state");
    expect(state).toHaveTextContent(/Task in progress.*· Context 3%/);
    const ctx = screen.getByTestId("chat-task-context");
    expect(ctx).toHaveAttribute("title", expect.stringMatching(/10k.*393k/));
  });

  it("a fill that rounds to zero reads <1%, not 0 %", () => {
    segment(stub({ context: { tokens: 1_200, max: 393_216 } }));
    expect(screen.getByTestId("chat-task-context")).toHaveTextContent("Context <1%");
  });

  it("nothing reported -> no figure (unknown is not 0 %)", () => {
    segment(stub({ context: null }));
    expect(screen.getByTestId("chat-task-state")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-task-context")).not.toBeInTheDocument();
    segment(stub({}));
    expect(screen.queryByTestId("chat-task-context")).not.toBeInTheDocument();
  });

  it("a section that is no longer live shows no figure, even with one in the data", () => {
    segment(stub({ status: "done", live: false, endedAt: stub().startedAt, context: { tokens: 10_224, max: 393_216 } }));
    expect(screen.queryByTestId("chat-task-context")).not.toBeInTheDocument();
  });

  it("a finished task's card shows no figure", () => {
    render(
      <TaskCard
        stub={stub({ status: "done", live: false, endedAt: stub().startedAt, context: { tokens: 10_224, max: 393_216 } })}
        open={false}
        onToggle={() => {}}
        steps={null}
        loading={false}
        error={false}
        onRetry={() => {}}
        frozenAtMs={null}
        detailLevel="normal"
      />,
    );
    expect(screen.getByTestId("chat-task-card")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-task-context")).not.toBeInTheDocument();
  });
});
