/**
 * In the agent chat a bench run's card leads to its benchmark result — the
 * task page only shows the raw deliverable (27.10.). Other cards keep the
 * task link.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ChatTaskStub } from "@/lib/chatTypes";
import { TaskSectionHeader } from "../TaskSection";

const TEST_ID = "bb467c57-8213-4438-a405-08b1d4cc7de2";

function stub(over: Partial<ChatTaskStub> = {}): ChatTaskStub {
  return {
    taskId: "t-1",
    title: "[Bench] Pagoda Garden",
    status: "done",
    live: false,
    deliveredAt: null,
    startedAt: null,
    startKey: null,
    endedAt: null,
    assignee: null,
    outcome: null,
    deliverables: 1,
    ...over,
  } as ChatTaskStub;
}

function renderHead(s: ChatTaskStub) {
  return render(<TaskSectionHeader stub={s} />);
}

describe("chat task head", () => {
  it("a bench run leads to its benchmark result", () => {
    renderHead(stub({ autoReason: `bench_studio challenge ${TEST_ID}` }));
    const head = screen.getByTestId("chat-task-head");
    expect(head).toHaveAttribute("href", `/bench/r/${TEST_ID}`);
    expect(head).toHaveTextContent("Benchmark");
  });

  it("any other task keeps its task link", () => {
    renderHead(stub({ autoReason: null }));
    expect(screen.getByTestId("chat-task-head")).toHaveAttribute("href", "/tasks?task=t-1");
  });
});
