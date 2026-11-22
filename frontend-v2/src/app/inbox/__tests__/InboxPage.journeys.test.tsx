/**
 * Inbox ↔ journey tests: a red nightly journey run waits on the operator
 * like an approval — it shows in "For you" and counts in the tab's number,
 * not only as a quiet line on /home.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { JourneysResult } from "@/lib/journeys";

const apiMock = vi.hoisted(() => ({ journeys: vi.fn() }));

vi.mock("@/lib/api", () => ({
  api: {
    agents: { list: vi.fn(async () => []) },
    approvals: { list: vi.fn(async () => []), resolve: vi.fn() },
    tasks: { list: vi.fn(async () => []), review: vi.fn(), comments: { list: vi.fn(async () => []) } },
    heads: { list: vi.fn(async () => ({ runs: [] })), occupancy: vi.fn(async () => ({ boxes: {} })) },
    system: { journeys: () => apiMock.journeys() },
  },
}));
vi.mock("@/lib/sse", () => ({ useApprovalStream: () => {} }));
vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/components/inbox/BetweenAgents", () => ({
  BetweenAgents: () => null,
  useConversations: () => ({ data: [] }),
}));
vi.mock("@/lib/store", () => ({
  useAppStore: Object.assign(
    (sel?: (s: { activeBoardId: string }) => unknown) => (sel ? sel({ activeBoardId: "board-1" }) : { activeBoardId: "board-1" }),
    { setState: () => {} },
  ),
  useNotificationStore: (sel?: (s: { notifications: never[] }) => unknown) =>
    sel ? sel({ notifications: [] }) : { notifications: [] },
}));

import InboxPage from "../page";

const RED: JourneysResult = {
  status: "red", finished_at: new Date().toISOString(), commit: "abc1234", total: 3, passed: 1,
  failed: ["J-usage", "J-setup"], known_gaps: 0, reason: null,
};

function renderInbox() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <InboxPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => apiMock.journeys.mockReset());

describe("Inbox — failed journey tests", () => {
  it("shows a red run under For you and counts it in the tab", async () => {
    apiMock.journeys.mockResolvedValue({ result: RED });
    renderInbox();
    const section = await screen.findByTestId("inbox-journeys");
    expect(within(section).getByTestId("journeys-line")).toHaveTextContent("Cost & local share");
    expect(screen.getByTestId("inbox-tab-you-count")).toHaveTextContent("2");
    expect(screen.queryByText("All clear")).toBeNull();
  });

  it("a green run is not something to answer: no section, nothing counted", async () => {
    apiMock.journeys.mockResolvedValue({ result: { ...RED, status: "green", passed: 3, failed: [] } });
    renderInbox();
    expect(await screen.findByText("All clear")).toBeInTheDocument();
    expect(screen.queryByTestId("inbox-journeys")).toBeNull();
    expect(screen.queryByTestId("inbox-tab-you-count")).toBeNull();
  });
});
