/** The WAITING FOR MODEL card: names what does not answer, says nothing is
 *  broken in the task — and stays away when there is nothing to add. */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { StateCard } from "@/lib/taskDetail/stateCard";
import { taskFixture } from "@/lib/taskDetail/__tests__/fixtures";

vi.mock("@/lib/api", () => ({ api: { approvals: { resolve: vi.fn() }, tasks: { thread: { post: vi.fn() } } } }));

import { TaskStateCard } from "../TaskStateCard";

function renderCard(card: StateCard) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <TaskStateCard card={card} task={taskFixture({ status: "inbox" })} onReply={() => {}} onOpenLog={() => {}} />
    </QueryClientProvider>,
  );
}

describe("TaskStateCard — waiting for the model", () => {
  it("names the runtime that does not answer and says the task is fine", () => {
    renderCard({ kind: "waiting_model", ready: false, delivered: true, runtime: "BOX-A :8000", reason: "x" });
    expect(screen.getByTestId("task-state-card")).toHaveAttribute("data-kind", "waiting_model");
    expect(screen.getByText("BOX-A :8000 does not answer.")).toBeInTheDocument();
    expect(screen.getByText(/Nothing is wrong with the task/)).toBeInTheDocument();
  });

  it("without a runtime name it still says the model does not answer", () => {
    renderCard({ kind: "waiting_model", ready: false, delivered: false, runtime: null, reason: null });
    expect(screen.getByText("The model does not answer.")).toBeInTheDocument();
  });

  it("model answers, first answer pending → no card (the state line says it)", () => {
    const { container } = renderCard({ kind: "waiting_model", ready: true, delivered: true, runtime: "BOX-A :8000", reason: null });
    expect(container.querySelector("[data-testid=task-state-card]")).toBeNull();
  });
});
