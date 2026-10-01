/**
 * NEEDS YOU on a task that waits for an answer (journey J-phone-needs-you):
 * the card shows the agent's open question and "Reply" answers THAT question
 * — a thread reply with reply_to, which resumes the task. Without an open
 * question, Reply still jumps to the comment field.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { StateCard } from "@/lib/taskDetail/stateCard";
import { openQuestionFixture, taskFixture } from "@/lib/taskDetail/__tests__/fixtures";

const apiMock = vi.hoisted(() => ({ threadPost: vi.fn(), resolve: vi.fn() }));

vi.mock("@/lib/api", () => ({
  api: {
    approvals: { resolve: (...a: unknown[]) => apiMock.resolve(...a) },
    tasks: { thread: { post: (...a: unknown[]) => apiMock.threadPost(...a) } },
  },
}));

import { TaskStateCard } from "../TaskStateCard";

function renderCard(card: StateCard, onReply = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TaskStateCard card={card} task={taskFixture({ status: "waiting" })} onReply={onReply} onOpenLog={() => {}} />
    </QueryClientProvider>,
  );
  return onReply;
}

function needsYou(question: ReturnType<typeof openQuestionFixture> | null): StateCard {
  return {
    kind: "needs_you",
    approval: null,
    reason: question?.body ?? null,
    since: null,
    askerAgentId: null,
    question,
  };
}

beforeEach(() => {
  apiMock.threadPost.mockReset();
  apiMock.threadPost.mockResolvedValue({ message_id: "m1", thread_id: "th1", task_status: "in_progress" });
});

describe("TaskStateCard — answering an open question", () => {
  it("shows the question and Reply sends a thread reply addressed to it", async () => {
    const onReply = renderCard(needsYou(openQuestionFixture()));
    expect(screen.getByText("Which release name: Aurora or Borealis?")).toBeInTheDocument();
    expect(screen.queryByText("No reason was given yet.")).toBeNull();

    const reply = screen.getByRole("button", { name: "Reply" });
    expect(reply).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Your answer" }), { target: { value: "Aurora." } });
    fireEvent.click(reply);

    await waitFor(() => expect(apiMock.threadPost).toHaveBeenCalledWith("task-1", "Aurora.", { replyTo: "q-1" }));
    expect(onReply).not.toHaveBeenCalled();
  });

  it("an offered option fills the answer", async () => {
    renderCard(needsYou(openQuestionFixture({ options: ["Aurora", "Borealis"] })));
    fireEvent.click(screen.getByRole("button", { name: "Borealis" }));
    expect(screen.getByRole("textbox", { name: "Your answer" })).toHaveValue("Borealis");
    fireEvent.click(screen.getByRole("button", { name: "Reply" }));
    await waitFor(() => expect(apiMock.threadPost).toHaveBeenCalledWith("task-1", "Borealis", { replyTo: "q-1" }));
  });

  it("without an open question, Reply still jumps to the comment field", () => {
    const onReply = renderCard(needsYou(null));
    expect(screen.getByText("No reason was given yet.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Your answer" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Reply" }));
    expect(onReply).toHaveBeenCalled();
    expect(apiMock.threadPost).not.toHaveBeenCalled();
  });
});
