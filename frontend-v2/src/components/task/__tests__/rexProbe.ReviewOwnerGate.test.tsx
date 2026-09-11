/**
 * REX REVIEW PROBE (PR #514) — not part of the PR, review evidence only.
 * Question: can a card the OPERATOR must decide lose its buttons?
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Task, Agent } from "@/lib/types";

const apiMock = vi.hoisted(() => ({
  agentsList: vi.fn(),
  tasksReview: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    agents: { list: (...a: unknown[]) => apiMock.agentsList(...a) },
    tasks: {
      review: (...a: unknown[]) => apiMock.tasksReview(...a),
      stop: vi.fn(),
      resume: vi.fn(),
      promote: vi.fn(),
      update: vi.fn(),
    },
  },
}));

import { TaskActions } from "../TaskActions";

const rex: Agent = { id: "agent-rex", name: "Rex", role: "reviewer" } as unknown as Agent;

function mkTask(o: Partial<Task> = {}): Task {
  return {
    id: "task-1", board_id: "board-1", title: "Ship it", status: "review",
    assigned_agent_id: "agent-rex", human_review_required: false,
    run_control: null, review_decision: null, dispatch_phase: null,
    parent_task_id: null, dispatched_at: null,
    ...o,
  } as unknown as Task;
}

function renderGate(task: Task) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TaskActions task={task} boardId="board-1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  apiMock.agentsList.mockReset();
  apiMock.tasksReview.mockReset();
  apiMock.tasksReview.mockResolvedValue({ status: "ok" });
});

describe("PROBE A — agent review is hidden (the intended fix)", () => {
  it("Rex holds the review → note instead of buttons", async () => {
    apiMock.agentsList.mockResolvedValue([rex]);
    renderGate(mkTask());
    expect(await screen.findByTestId("agent-review-note")).toBeInTheDocument();
    expect(screen.queryByText("Approve")).not.toBeInTheDocument();
    expect(screen.getByText("Rex is reviewing")).toBeInTheDocument();
    expect(screen.getByText("Decide yourself")).toBeInTheDocument();
  });
});

describe("PROBE B — counter-probe: the operator's own review keeps its buttons", () => {
  it("human_review_required=true + Rex assigned → Approve still there", async () => {
    apiMock.agentsList.mockResolvedValue([rex]);
    renderGate(mkTask({ human_review_required: true }));
    expect(await screen.findByText("Approve")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-review-note")).not.toBeInTheDocument();
  });

  it("unknown assignee (not in the agent list) → Approve still there", async () => {
    apiMock.agentsList.mockResolvedValue([]);
    renderGate(mkTask({ assigned_agent_id: "ghost-agent" }));
    expect(await screen.findByText("Approve")).toBeInTheDocument();
  });

  it("no assignee at all → Approve still there", async () => {
    apiMock.agentsList.mockResolvedValue([rex]);
    renderGate(mkTask({ assigned_agent_id: null }));
    expect(await screen.findByText("Approve")).toBeInTheDocument();
  });
});

describe("PROBE C — DANGER: agent list unavailable", () => {
  it("agents request FAILS + human_review_required=true → what does the operator see?", async () => {
    apiMock.agentsList.mockRejectedValue(new Error("500 boom"));
    renderGate(mkTask({ human_review_required: true }));
    await waitFor(() => expect(apiMock.agentsList).toHaveBeenCalled());
    // give react-query a tick to settle into the error state
    await new Promise((r) => setTimeout(r, 50));
    const approve = screen.queryByText("Approve");
    const note = screen.queryByTestId("agent-review-note");
    // eslint-disable-next-line no-console
    console.log("PROBE C RESULT — Approve:", !!approve, "| note:", !!note);
    expect({ approve: !!approve, note: !!note }).toEqual({ approve: true, note: false });
  });

  it("agents request HANGS + human_review_required=true → what does the operator see?", async () => {
    apiMock.agentsList.mockReturnValue(new Promise(() => {}));
    renderGate(mkTask({ human_review_required: true }));
    await new Promise((r) => setTimeout(r, 50));
    const approve = screen.queryByText("Approve");
    const note = screen.queryByTestId("agent-review-note");
    // eslint-disable-next-line no-console
    console.log("PROBE C2 RESULT (loading) — Approve:", !!approve, "| note:", !!note);
    expect({ approve: !!approve, note: !!note }).toEqual({ approve: true, note: false });
  });
});

describe("PROBE D — 'Decide yourself' override", () => {
  it("override opens the real decision section and posts a real operator decision", async () => {
    apiMock.agentsList.mockResolvedValue([rex]);
    renderGate(mkTask());
    fireEvent.click(await screen.findByText("Decide yourself"));
    expect(await screen.findByText("Approve")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Review-Begruendung"), { target: { value: "passt" } });
    fireEvent.click(screen.getByText("Approve"));
    await waitFor(() => expect(apiMock.tasksReview).toHaveBeenCalled());
    // eslint-disable-next-line no-console
    console.log("PROBE D RESULT — review call:", JSON.stringify(apiMock.tasksReview.mock.calls[0]));
  });
});
