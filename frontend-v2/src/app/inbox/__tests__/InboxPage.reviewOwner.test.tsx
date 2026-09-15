/** REVIEW PROBE (independent reviewer) (PR #514) — inbox list level. Review evidence only. */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Task, Agent } from "@/lib/types";

const apiMock = vi.hoisted(() => ({
  agentsList: vi.fn(),
  tasksList: vi.fn(),
  comments: vi.fn(),
  approvals: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    agents: { list: (...a: unknown[]) => apiMock.agentsList(...a) },
    approvals: { list: () => apiMock.approvals(), resolve: vi.fn() },
    tasks: {
      list: (...a: unknown[]) => apiMock.tasksList(...a),
      review: vi.fn(),
      comments: { list: (...a: unknown[]) => apiMock.comments(...a) },
    },
  },
}));
vi.mock("@/lib/sse", () => ({ useApprovalStream: () => {} }));
vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
const mockState = vi.hoisted(() => ({ state: { activeBoardId: "board-1" as string | null } }));
vi.mock("@/lib/store", () => ({
  useAppStore: Object.assign(
    (sel?: (s: typeof mockState.state) => unknown) => (sel ? sel(mockState.state) : mockState.state),
    { setState: () => {} },
  ),
  useNotificationStore: (sel?: (s: { notifications: never[] }) => unknown) =>
    sel ? sel({ notifications: [] }) : { notifications: [] },
}));

import InboxPage from "../page";

const argus: Agent = { id: "agent-argus", name: "Argus", role: "reviewer", role_canonical: "reviewer" } as unknown as Agent;
const dev: Agent = { id: "agent-dev", name: "Delta", role: "developer", role_canonical: "developer" } as unknown as Agent;

function mkTask(o: Partial<Task> = {}): Task {
  return {
    id: "t1", board_id: "board-1", title: "Ship it", status: "review",
    assigned_agent_id: "agent-argus", human_review_required: false,
    review_decision: null, run_control: null, created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z", priority: "medium",
    // Real handoff by default — probes that want the self-review-stall case
    // (no handoff, W2) override this explicitly.
    dispatch_intent: "review_handoff",
    ...o,
  } as unknown as Task;
}

function renderInbox() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <InboxPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  Object.values(apiMock).forEach((m) => m.mockReset());
  apiMock.approvals.mockResolvedValue([]);
  // assignee has commented → old filter would have shown buttons
  apiMock.comments.mockResolvedValue([{ id: "c1", author_agent_id: "agent-argus", content: "review läuft" }]);
});

describe("PROBE E — inbox list", () => {
  it("Argus holds it → read-only row, no decision row", async () => {
    apiMock.tasksList.mockResolvedValue([mkTask()]);
    apiMock.agentsList.mockResolvedValue([argus, dev]);
    renderInbox();
    expect(await screen.findByTestId("agent-review-row")).toBeInTheDocument();
    expect(screen.queryByText("Approve")).not.toBeInTheDocument();
    expect(screen.getByText("Argus is reviewing")).toBeInTheDocument();
    expect(screen.getByText(/review with an agent|reviews with agents/i)).toBeInTheDocument();
  });

  it("COUNTER-PROBE human_review_required=true → still in the operator list", async () => {
    apiMock.tasksList.mockResolvedValue([mkTask({ human_review_required: true })]);
    apiMock.agentsList.mockResolvedValue([argus, dev]);
    renderInbox();
    await waitFor(() => expect(screen.queryByTestId("agent-review-row")).not.toBeInTheDocument());
    expect(await screen.findByText("Ship it")).toBeInTheDocument();
    // eslint-disable-next-line no-console
    console.log("PROBE E2 — operator section present:", !!screen.queryByText(/Tasks for review/i));
    expect(screen.getByText(/Tasks for review/i)).toBeInTheDocument();
  });

  it("COUNTER-PROBE unknown assignee → stays with the operator", async () => {
    apiMock.tasksList.mockResolvedValue([mkTask({ assigned_agent_id: "ghost" })]);
    apiMock.agentsList.mockResolvedValue([argus, dev]);
    apiMock.comments.mockResolvedValue([{ id: "c1", author_agent_id: "ghost", content: "done" }]);
    renderInbox();
    expect(await screen.findByText(/Tasks for review/i)).toBeInTheDocument();
    expect(screen.queryByTestId("agent-review-row")).not.toBeInTheDocument();
  });

  // W2 (PR #514 Rex review): Argus reviews its own card — backend skips the
  // handoff (task_lifecycle.py:handle_review_handoff), dispatch_intent never
  // becomes "review_handoff". The card must land with the operator, not in
  // the read-only agent-review bucket, and must carry decision buttons.
  it("W2 self-review stall (Argus is also the developer, no handoff) → operator list with decision buttons, not the read-only agent row", async () => {
    apiMock.tasksList.mockResolvedValue([mkTask({ dispatch_intent: "root" })]);
    apiMock.agentsList.mockResolvedValue([argus, dev]);
    renderInbox();
    expect(await screen.findByText("Ship it")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-review-row")).not.toBeInTheDocument();
    expect(await screen.findByText("Approve")).toBeInTheDocument();
    expect(screen.getByText(/No handoff happened.*awaiting lead.*Argus/i)).toBeInTheDocument();
  });

  // B2 (PR #517 Rex review): a Lead manually reassigning a stuck review to a
  // different reviewer sets dispatch_intent="manual_redispatch" — that must
  // read as a real handoff (agent-review row), not a self-review stall.
  it("B2 manual reassignment to a new reviewer (dispatch_intent 'manual_redispatch') → read-only agent-review row, not the operator's decision list", async () => {
    apiMock.tasksList.mockResolvedValue([mkTask({ dispatch_intent: "manual_redispatch" })]);
    apiMock.agentsList.mockResolvedValue([argus, dev]);
    renderInbox();
    expect(await screen.findByTestId("agent-review-row")).toBeInTheDocument();
    expect(screen.queryByText("Approve")).not.toBeInTheDocument();
  });

  it("DIRECTION agents request FAILS → operator keeps the card (inbox falls safe)", async () => {
    apiMock.tasksList.mockResolvedValue([mkTask()]);
    apiMock.agentsList.mockRejectedValue(new Error("500 boom"));
    renderInbox();
    await waitFor(() => expect(apiMock.agentsList).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 50));
    const inOperatorList = !!screen.queryByText(/Tasks for review/i);
    const inAgentList = !!screen.queryByTestId("agent-review-row");
    // eslint-disable-next-line no-console
    console.log("PROBE E4 — operator list:", inOperatorList, "| agent list:", inAgentList);
    expect({ inOperatorList, inAgentList }).toEqual({ inOperatorList: true, inAgentList: false });
  });
});
