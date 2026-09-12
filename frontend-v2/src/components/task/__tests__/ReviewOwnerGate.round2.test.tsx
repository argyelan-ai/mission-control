/**
 * REVIEW PROBE ROUND 2 (independent reviewer, PR #514) — review evidence only.
 *
 * Round 1 proved the two dangerous states with human_review_required=true
 * (PROBE C / C2). Round 2 asks what the gate does in the states nobody has
 * pinned down yet:
 *   G1 agents fetch FAILS while a reviewer holds the card (no explicit
 *      operator request) — is the fallback the safe direction?
 *   G2 agents fetch is still PENDING while a reviewer holds the card — do
 *      the buttons flash before the note appears?
 *   G3 the reviewer's run was stopped (run_control) — the gate must not
 *      change its mind about who owns the decision.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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

const argus: Agent = { id: "agent-argus", name: "Argus", role: "reviewer", role_canonical: "reviewer" } as unknown as Agent;

function mkTask(o: Partial<Task> = {}): Task {
  return {
    id: "task-1", board_id: "board-1", title: "Ship it", status: "review",
    assigned_agent_id: "agent-argus", human_review_required: false,
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

describe("PROBE G1 — agents fetch fails, no explicit operator request", () => {
  it("reviewer holds the card + /agents 500 → decision falls back to the operator", async () => {
    apiMock.agentsList.mockRejectedValue(new Error("500 boom"));
    renderGate(mkTask());
    await waitFor(() => expect(apiMock.agentsList).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 50));
    const approve = screen.queryByText("Approve");
    const note = screen.queryByTestId("agent-review-note");
    // eslint-disable-next-line no-console
    console.log("PROBE G1 RESULT — Approve:", !!approve, "| note:", !!note);
    // Safe direction per lib/reviewRouting.ts ("unknown agent → do not hide
    // silently"): the operator sees the decision UI rather than an empty card.
    expect({ approve: !!approve, note: !!note }).toEqual({ approve: true, note: false });
  });
});

describe("PROBE G2 — no flash while the agent list is still loading", () => {
  it("reviewer holds the card + /agents pending → neither buttons nor note, then the note", async () => {
    let resolve!: (v: Agent[]) => void;
    apiMock.agentsList.mockReturnValue(new Promise<Agent[]>((r) => { resolve = r; }));
    renderGate(mkTask());
    await new Promise((r) => setTimeout(r, 50));
    const approveWhileLoading = screen.queryByText("Approve");
    // eslint-disable-next-line no-console
    console.log("PROBE G2 RESULT (loading) — Approve:", !!approveWhileLoading);
    expect(approveWhileLoading).toBeNull();
    resolve([argus]);
    expect(await screen.findByTestId("agent-review-note")).toBeInTheDocument();
    expect(screen.queryByText("Approve")).not.toBeInTheDocument();
  });
});

describe("PROBE G3 — a stopped reviewer run does not move the decision", () => {
  it("reviewer holds the card, run_control=stopped → still the agent's review", async () => {
    apiMock.agentsList.mockResolvedValue([argus]);
    renderGate(mkTask({ run_control: "stopped" }));
    expect(await screen.findByTestId("agent-review-note")).toBeInTheDocument();
    expect(screen.queryByText("Approve")).not.toBeInTheDocument();
  });
});
