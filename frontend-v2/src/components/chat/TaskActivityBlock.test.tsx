import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { TaskActivityBlock } from "./TaskActivityBlock";
import { useTaskActivity } from "@/hooks/useTaskActivity";
import type { TaskActivity } from "@/lib/chatTypes";

vi.mock("@/hooks/useTaskActivity", () => ({ useTaskActivity: vi.fn() }));
const mockActivity = vi.mocked(useTaskActivity);

function running(overrides: Partial<Extract<TaskActivity, { applies: true }>> = {}): TaskActivity {
  return {
    applies: true,
    taskId: "d3e397c0",
    hasSession: true,
    deliveredAt: new Date(Date.now() - 2 * 60_000).toISOString(),
    events: [
      { kind: "tool", uuid: "a1", ts: "2026-10-20T09:00:00Z", name: "bash", title: "$ mc ack d3e397c0",
        detail: {}, result: "ok", status: "done", toolUseId: "c1" },
      { kind: "thinking", uuid: "a2", ts: "2026-10-20T09:00:20Z", text: "Plan: three steps.",
        sidechain: false, durationMs: 15_000 },
    ] as never,
    preview: {
      kind: "preview", uuid: null, ts: new Date().toISOString(), source: "acp", block: "thinking",
      chars: 23_600, startedAt: new Date(Date.now() - 105_000).toISOString(), text: "…build a single list.",
    },
    queued: 1,
    ...overrides,
  };
}

describe("TaskActivityBlock", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the task's delivery, its newest steps, the running thought and the waiting tasks", () => {
    mockActivity.mockReturnValue(running());
    render(<TaskActivityBlock agentId="agent-1" />);
    const block = screen.getByTestId("chat-task-activity");
    expect(block).toHaveAttribute("data-region", "chat-task-activity");
    expect(block).toHaveTextContent("From the task");
    expect(block).toHaveTextContent(/Delivered 2 minutes ago/);
    expect(block).toHaveTextContent("$ mc ack d3e397c0");
    expect(block).toHaveTextContent("Thought · 0:15");
    expect(screen.getByTestId("live-thinking")).toHaveTextContent("≈5,900 tokens");
    expect(block).toHaveTextContent("1 more task waiting");
  });

  it("asks only while a task is bound and only for this agent", () => {
    mockActivity.mockReturnValue(running());
    render(<TaskActivityBlock agentId="agent-1" />);
    expect(mockActivity).toHaveBeenCalledWith("agent-1");
  });

  it("an answer being written shows as a quiet preview, not as a thought", () => {
    mockActivity.mockReturnValue(running({
      preview: { kind: "preview", uuid: null, ts: new Date().toISOString(), source: "acp", text: "Fertig: drei Dateien." },
    }));
    render(<TaskActivityBlock agentId="agent-1" />);
    expect(screen.queryByTestId("live-thinking")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-task-activity")).toHaveTextContent("Fertig: drei Dateien.");
  });

  it("no waiting tasks -> no line about them (nothing empty)", () => {
    mockActivity.mockReturnValue(running({ queued: 0 }));
    render(<TaskActivityBlock agentId="agent-1" />);
    expect(screen.getByTestId("chat-task-activity")).not.toHaveTextContent(/waiting/);
  });

  it("renders nothing when the chat itself shows the task (or there is none)", () => {
    mockActivity.mockReturnValue({ applies: false });
    const { container } = render(<TaskActivityBlock agentId="agent-1" />);
    expect(container).toBeEmptyDOMElement();
    mockActivity.mockReturnValue(null);
    const again = render(<TaskActivityBlock agentId="agent-1" />);
    expect(again.container).toBeEmptyDOMElement();
  });
});
