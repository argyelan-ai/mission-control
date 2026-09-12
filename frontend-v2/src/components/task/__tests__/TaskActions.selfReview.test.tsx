/**
 * PROBE W2 (PR #514 Rex review) — card view level.
 *
 * When a reviewer agent is also the developer of its own card, the backend
 * skips the review handoff entirely (task_lifecycle.py:handle_review_handoff,
 * "Reviewer must not be the same agent"). Before this fix, ReviewOwnerGate
 * still read the assigned agent's role as "reviewer" and rendered the
 * read-only "<agent> is reviewing" note with the decision buttons hidden —
 * the card looked handled while it was actually waiting on a human. These
 * probes pin down that the card view now shows the decision buttons
 * directly, labeled as waiting on the Lead.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Task, Agent } from "@/lib/types";

const apiMock = vi.hoisted(() => ({
  agentsList: vi.fn(),
  tasksReview: vi.fn(),
  tasksUpdate: vi.fn(),
  tasksPromote: vi.fn(),
  tasksStop: vi.fn(),
  tasksResume: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    agents: { list: (...a: unknown[]) => apiMock.agentsList(...a) },
    tasks: {
      review: (...a: unknown[]) => apiMock.tasksReview(...a),
      update: (...a: unknown[]) => apiMock.tasksUpdate(...a),
      promote: (...a: unknown[]) => apiMock.tasksPromote(...a),
      stop: (...a: unknown[]) => apiMock.tasksStop(...a),
      resume: (...a: unknown[]) => apiMock.tasksResume(...a),
    },
  },
}));

import { TaskActions } from "../TaskActions";

const argus: Agent = { id: "agent-argus", name: "Argus", role: "reviewer", role_canonical: "reviewer" } as unknown as Agent;

function mkTask(o: Partial<Task> = {}): Task {
  return {
    id: "t1", board_id: "board-1", title: "Ship it", status: "review",
    assigned_agent_id: "agent-argus", human_review_required: false,
    review_decision: null, run_control: null, dispatch_phase: null,
    parent_task_id: null, dispatched_at: null,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
    priority: "medium",
    dispatch_intent: "review_handoff",
    ...o,
  } as unknown as Task;
}

function renderActions(task: Task) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TaskActions task={task} boardId="board-1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  Object.values(apiMock).forEach((m) => m.mockReset());
});

describe("PROBE W2 — card view review gate", () => {
  it("real handoff (dispatch_intent=review_handoff) → read-only 'is reviewing' note, no decision buttons", async () => {
    apiMock.agentsList.mockResolvedValue([argus]);
    renderActions(mkTask());
    expect(await screen.findByTestId("agent-review-note")).toBeInTheDocument();
    expect(screen.getByText("Argus is reviewing")).toBeInTheDocument();
    expect(screen.queryByText("Approve", { exact: false })).not.toBeInTheDocument();
  });

  it("self-review stall (Argus is also the developer, dispatch_intent stayed 'root') → decision buttons shown directly, labeled 'awaiting lead'", async () => {
    apiMock.agentsList.mockResolvedValue([argus]);
    renderActions(mkTask({ dispatch_intent: "root" }));
    expect(await screen.findByTestId("self-review-stall-note")).toBeInTheDocument();
    expect(screen.getByText(/No handoff happened.*awaiting lead.*Argus/i)).toBeInTheDocument();
    expect(screen.queryByTestId("agent-review-note")).not.toBeInTheDocument();
    expect(screen.getByText("Approve", { exact: false })).toBeInTheDocument();
  });

  it("manual reassignment to a different reviewer (dispatch_intent='manual_redispatch') → real handoff, NOT a self-review stall (B2, PR #517 Rex review)", async () => {
    apiMock.agentsList.mockResolvedValue([argus]);
    renderActions(mkTask({ dispatch_intent: "manual_redispatch" }));
    expect(await screen.findByTestId("agent-review-note")).toBeInTheDocument();
    expect(screen.getByText("Argus is reviewing")).toBeInTheDocument();
    expect(screen.queryByTestId("self-review-stall-note")).not.toBeInTheDocument();
    expect(screen.queryByText("Approve", { exact: false })).not.toBeInTheDocument();
  });
});
