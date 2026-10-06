/**
 * Inbox ↔ heads (heads-sichtbar PR 3, bauplan §4): a head's open question
 * links into its own chat (not the task), and a review card a head finished
 * shows "Head · <pair> · passed ›" instead of an assigned agent's name.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Task, Agent } from "@/lib/types";
import { mkRun } from "@/lib/__tests__/headFixtures";

const apiMock = vi.hoisted(() => ({
  agentsList: vi.fn(),
  tasksList: vi.fn(),
  comments: vi.fn(),
  approvals: vi.fn(),
  headsList: vi.fn(),
  headsOccupancy: vi.fn(),
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
    heads: {
      list: (...a: unknown[]) => apiMock.headsList(...a),
      occupancy: (...a: unknown[]) => apiMock.headsOccupancy(...a),
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

function mkTask(o: Partial<Task> = {}): Task {
  return {
    id: "t1", board_id: "board-1", title: "Ship it", status: "review",
    assigned_agent_id: null, human_review_required: false,
    review_decision: null, run_control: "manual_hold", created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z", priority: "medium", dispatch_intent: "root",
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

describe("Inbox — head questions link into the head chat", () => {
  it("an open head question links to /sessions?head=<id>, naming the pair", async () => {
    apiMock.agentsList.mockResolvedValue([] as Agent[]);
    apiMock.approvals.mockResolvedValue([]);
    apiMock.tasksList.mockResolvedValue([]);
    apiMock.headsOccupancy.mockResolvedValue({ boxes: {} });
    const run = mkRun({
      run_id: "run-needs-you", task_id: "t2", title: "Insights: week view", harness: "omp",
      model: "GLM-5.3-Flash-EXL3", state: "needs_you", question: "Which week do you mean?",
    });
    apiMock.headsList.mockResolvedValue({ runs: [run] });

    renderInbox();
    const link = await screen.findByTestId("inbox-head-question");
    expect(link).toHaveAttribute("href", "/sessions?head=run-needs-you");
    expect(link).toHaveTextContent("Head · omp × GLM-5.3");
    expect(link).toHaveTextContent("Which week do you mean?");
  });
});

describe("Inbox — a head-finished review card shows its pair, not an agent", () => {
  it("'Head · <pair> · passed ›' links into the head chat", async () => {
    apiMock.agentsList.mockResolvedValue([] as Agent[]);
    apiMock.approvals.mockResolvedValue([]);
    apiMock.tasksList.mockResolvedValue([mkTask()]);
    apiMock.headsOccupancy.mockResolvedValue({ boxes: {} });
    const run = mkRun({ run_id: "run-passed", task_id: "t1", harness: "omp", model: "GLM-5.3-Flash-EXL3", state: "passed", pr_url: "https://github.com/o/r/pull/1" });
    apiMock.headsList.mockResolvedValue({ runs: [run] });

    renderInbox();
    const chip = await screen.findByTestId("review-row-head-chip");
    expect(chip).toHaveTextContent("Head · omp × GLM-5.3 · passed");
    expect(chip).toHaveAttribute("href", "/sessions?head=run-passed");
  });
});
