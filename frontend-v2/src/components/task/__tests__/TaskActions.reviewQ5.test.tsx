/**
 * Round-2 Q5 (t4 N-neu-5/6, r2 S7): a task in the operator's review offered
 * "Stop run" (there is no run) and, on the phone, two "Approve" buttons —
 * the one in the decision panel and its mirror in the bottom action bar.
 * Now: Stop run only while a reviewer agent really holds the review; on the
 * phone the bar's Approve is the one, the panel's steps aside (it comes back
 * while the keyboard is up, when the bar hides). Setup from
 * TaskActions.calm.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Task } from "@/lib/types";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";

const apiMock = vi.hoisted(() => ({
  agentsList: vi.fn(),
  tasksReview: vi.fn(),
  tasksPromote: vi.fn(),
  tasksStop: vi.fn(),
  tasksResume: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    agents: { list: (...a: unknown[]) => apiMock.agentsList(...a) },
    tasks: {
      review: (...a: unknown[]) => apiMock.tasksReview(...a),
      promote: (...a: unknown[]) => apiMock.tasksPromote(...a),
      stop: (...a: unknown[]) => apiMock.tasksStop(...a),
      resume: (...a: unknown[]) => apiMock.tasksResume(...a),
    },
  },
}));

import { TaskActions } from "../TaskActions";

function mkTask(o: Partial<Task> = {}): Task {
  return {
    id: "t1", board_id: "board-1", title: "Ship it", status: "in_progress",
    assigned_agent_id: null, human_review_required: false,
    review_decision: null, run_control: null, dispatch_phase: null,
    parent_task_id: null, dispatched_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
    priority: "medium",
    ...o,
  } as unknown as Task;
}

function renderActions(task: Task, primaryTaken = false, mirroredOnPhone = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TaskActions task={task} boardId="board-1" primaryTaken={primaryTaken} mirroredOnPhone={mirroredOnPhone} />
    </QueryClientProvider>,
  );
}

const isPrimary = (el: HTMLElement) => el.style.background.includes("--color-accent");

beforeEach(() => {
  Object.values(apiMock).forEach((m) => m.mockReset());
  apiMock.agentsList.mockResolvedValue([]);
  apiMock.tasksStop.mockResolvedValue({});
  apiMock.tasksResume.mockResolvedValue({});
  apiMock.tasksReview.mockResolvedValue({});
});

describe("review: Stop run only while an agent reviews", () => {
  it("the operator's review has no run to stop", () => {
    renderActions(mkTask({ status: "review", human_review_required: true, dispatched_at: null }));
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop run" })).toBeNull();
  });

  it("a card nobody holds (operator's call) has no run to stop either", () => {
    renderActions(mkTask({ status: "review", assigned_agent_id: null }));
    expect(screen.queryByRole("button", { name: "Stop run" })).toBeNull();
  });

  // PR #963 review MUST 1: the shape the backend really produces. A worker's
  // current_task_id is never set for a review card (in_progress only, the
  // heartbeat clears it otherwise); the live run shows as status "working".
  it("a reviewer agent working on the card can still be stopped (pointer empty, as the backend leaves it)", async () => {
    apiMock.agentsList.mockResolvedValue([
      { id: "rev", name: "Checker", role_canonical: "reviewer", status: "working", run_state: "running", current_task_id: null },
    ]);
    renderActions(mkTask({ status: "review", assigned_agent_id: "rev", dispatch_intent: "review_handoff" } as Partial<Task>));
    expect(await screen.findByRole("button", { name: "Stop run" })).toBeInTheDocument();
  });

  it("a reviewer whose pointer names this card keeps Stop run too", async () => {
    apiMock.agentsList.mockResolvedValue([
      { id: "rev", name: "Checker", role_canonical: "reviewer", status: "working", run_state: "running", current_task_id: "t1" },
    ]);
    renderActions(mkTask({ status: "review", assigned_agent_id: "rev", dispatch_intent: "review_handoff" } as Partial<Task>));
    expect(await screen.findByRole("button", { name: "Stop run" })).toBeInTheDocument();
  });

  it("a self-review stall while the builder still works on its card keeps Stop run", async () => {
    apiMock.agentsList.mockResolvedValue([
      { id: "dev", name: "Builder", role_canonical: "reviewer", status: "working", run_state: "running", current_task_id: null },
    ]);
    renderActions(mkTask({ status: "review", assigned_agent_id: "dev", dispatch_intent: "root" } as Partial<Task>));
    expect(await screen.findByRole("button", { name: "Stop run" })).toBeInTheDocument();
  });

  // Round-3 t4 Q5 rest: "In review · with <agent>", the agent idle,
  // or working on another task — "Stop run" stopped nothing.
  it.each([
    ["idle, pointer empty", { status: "idle", run_state: "idle", current_task_id: null }],
    ["idle, pointer still on the card", { status: "idle", run_state: "idle", current_task_id: "t1" }],
    ["working on another task", { status: "working", run_state: "running", current_task_id: "other" }],
  ])("an agent that holds the card but is %s has no run to stop", async (_label, shape) => {
    apiMock.agentsList.mockResolvedValue([{ id: "rev", name: "Checker", role_canonical: "reviewer", ...shape }]);
    renderActions(mkTask({ status: "review", assigned_agent_id: "rev", dispatch_intent: "review_handoff" } as Partial<Task>));
    await screen.findByText(/is reviewing/);
    expect(screen.queryByRole("button", { name: "Stop run" })).toBeNull();
  });

  it("a self-review stall with the builder idle has no run to stop", async () => {
    apiMock.agentsList.mockResolvedValue([
      { id: "dev", name: "Builder", role_canonical: "reviewer", status: "idle", run_state: "idle", current_task_id: null },
    ]);
    renderActions(mkTask({ status: "review", assigned_agent_id: "dev", dispatch_intent: "root" } as Partial<Task>));
    await waitFor(() => expect(apiMock.agentsList).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole("button", { name: "Stop run" })).toBeNull();
  });

  it("a review card never dispatched to its agent has no run, even with the agent busy", async () => {
    apiMock.agentsList.mockResolvedValue([
      { id: "rev", name: "Checker", role_canonical: "reviewer", status: "working", run_state: "running", current_task_id: null },
    ]);
    renderActions(
      mkTask({ status: "review", assigned_agent_id: "rev", dispatch_intent: "review_handoff", dispatched_at: null } as Partial<Task>),
    );
    await screen.findByText(/is reviewing/);
    expect(screen.queryByRole("button", { name: "Stop run" })).toBeNull();
  });

  it("no Stop run flashes while the agents are still loading", () => {
    apiMock.agentsList.mockReturnValue(new Promise(() => {}));
    renderActions(mkTask({ status: "review", assigned_agent_id: "rev", dispatch_intent: "review_handoff" } as Partial<Task>));
    expect(screen.queryByRole("button", { name: "Stop run" })).toBeNull();
  });

  it("in progress keeps Stop run", () => {
    renderActions(mkTask());
    expect(screen.getByRole("button", { name: "Stop run" })).toBeInTheDocument();
  });
});

describe("review on the phone: one Approve", () => {
  const review = () => mkTask({ status: "review", human_review_required: true, dispatched_at: null });

  it("with the bar mirroring it, the panel's Approve is hidden below md", () => {
    renderActions(review(), false, true);
    const approve = screen.getByRole("button", { name: "Approve", hidden: true });
    expect(approve.className).toContain("max-md:hidden");
    // still the next step's primary: the bar finds and runs it
    expect(approve.className).toContain("main-action");
  });

  it("without the phone bar it stays where it is", () => {
    renderActions(review());
    expect(screen.getByRole("button", { name: "Approve" }).className).not.toContain("max-md:hidden");
  });
});
