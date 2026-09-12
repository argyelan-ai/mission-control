/**
 * REVIEW PROBE ROUND 2 (independent reviewer, PR #514) — review evidence only.
 *
 * W3 shipped without a test: the agent-review row now shows an "on hold" badge
 * when review_decision === "hold" (app/inbox/page.tsx). These probes pin the
 * behaviour down in both directions, plus the routing premise the badge rests
 * on — a held review must still land in the agent bucket, otherwise the badge
 * is dead code.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Task, Agent } from "@/lib/types";

const apiMock = vi.hoisted(() => ({
  tasksList: vi.fn(),
  agentsList: vi.fn(),
  approvals: vi.fn(),
  comments: vi.fn(),
  resolve: vi.fn(),
  review: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    agents: { list: (...a: unknown[]) => apiMock.agentsList(...a) },
    approvals: { list: () => apiMock.approvals(), resolve: vi.fn() },
    tasks: {
      list: (...a: unknown[]) => apiMock.tasksList(...a),
      review: (...a: unknown[]) => apiMock.review(...a),
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

// FALLBACK-PROBE (W1 follow-up): `role_canonical` is resolved server-side
// (app/scopes.py:normalize_agent_role) and only shipped by a backend that has
// already deployed that change. Until every environment is on that backend,
// `GET /api/v1/agents` can still answer without the field — so the fixture
// must model "field absent", not "field null", to match what actually happens
// on the wire. Ownership while that gap exists: lib/reviewRouting.ts's
// documented fail-safe (`!== "reviewer"` on `undefined`) routes the row to the
// operator, never to this agent bucket — that's the contract this probe pins.
const argusNoCanonical: Agent = { id: "agent-argus", name: "Argus", role: "reviewer" } as unknown as Agent;

function mkTask(o: Partial<Task> = {}): Task {
  return {
    id: "t1", board_id: "board-1", title: "Ship it", status: "review",
    assigned_agent_id: "agent-argus", human_review_required: false,
    review_decision: null, run_control: null, created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z", priority: "medium",
    // Real handoff by default — a held review still requires a genuine handoff.
    dispatch_intent: "review_handoff",
    ...o,
  } as unknown as Task;
}

function renderInbox() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <InboxPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  Object.values(apiMock).forEach((m) => m.mockReset());
  apiMock.approvals.mockResolvedValue([]);
  apiMock.comments.mockResolvedValue([{ id: "c1", author_agent_id: "agent-argus", content: "review läuft" }]);
});

describe("PROBE W3 — held reviews are recognisable in the inbox", () => {
  it("review_decision=hold → row carries the 'on hold' badge", async () => {
    apiMock.tasksList.mockResolvedValue([mkTask({ review_decision: "hold" })]);
    apiMock.agentsList.mockResolvedValue([argus]);
    renderInbox();
    expect(await screen.findByTestId("agent-review-row")).toBeInTheDocument();
    const badge = await screen.findByTestId("agent-review-hold");
    // eslint-disable-next-line no-console
    console.log("PROBE W3 RESULT — badge text:", JSON.stringify(badge.textContent));
    expect(badge).toHaveTextContent(/on hold/i);
    // the row stays read-only — no decision buttons sneak in with the badge
    expect(screen.queryByText("Approve")).not.toBeInTheDocument();
  });

  it("COUNTER-PROBE review_decision=null → no badge", async () => {
    apiMock.tasksList.mockResolvedValue([mkTask()]);
    apiMock.agentsList.mockResolvedValue([argus]);
    renderInbox();
    expect(await screen.findByTestId("agent-review-row")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-review-hold")).not.toBeInTheDocument();
  });

  it("COUNTER-PROBE hold + human_review_required=true → operator list, no agent row", async () => {
    apiMock.tasksList.mockResolvedValue([mkTask({ review_decision: "hold", human_review_required: true })]);
    apiMock.agentsList.mockResolvedValue([argus]);
    renderInbox();
    await waitFor(() => expect(screen.queryByTestId("agent-review-row")).not.toBeInTheDocument());
    expect(await screen.findByText("Ship it")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-review-hold")).not.toBeInTheDocument();
  });

  it("FALLBACK-PROBE agent has no role_canonical (backend not yet redeployed) → operator's, no agent row", async () => {
    apiMock.tasksList.mockResolvedValue([mkTask()]);
    apiMock.agentsList.mockResolvedValue([argusNoCanonical]);
    renderInbox();
    await waitFor(() => expect(screen.queryByTestId("agent-review-row")).not.toBeInTheDocument());
    expect(await screen.findByText("Ship it")).toBeInTheDocument();
  });
});
