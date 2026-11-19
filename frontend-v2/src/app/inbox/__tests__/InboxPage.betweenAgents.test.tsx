/**
 * Inbox tabs "For you" / "Between agents" (ADR-100 §4, stage 2d).
 *
 * - "For you" is today's inbox, unchanged; only it counts for the badge.
 * - "Between agents" lists session conversations read-only; opening one
 *   shows its messages under a visible "info only, not from you" notice.
 * - A brake-paused conversation names the brake and sends the operator to
 *   "For you", where the approval card is decided.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Approval } from "@/lib/types";
import type { Conversation } from "@/lib/comms";

const apiMock = vi.hoisted(() => ({
  approvals: vi.fn(),
  conversations: vi.fn(),
  messages: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    agents: { list: () => Promise.resolve([]) },
    approvals: { list: () => apiMock.approvals(), resolve: vi.fn() },
    tasks: { list: () => Promise.resolve([]), review: vi.fn(), comments: { list: () => Promise.resolve([]) } },
    heads: { list: () => Promise.resolve({ runs: [] }) },
    comms: {
      conversations: () => apiMock.conversations(),
      messages: (id: string) => apiMock.messages(id),
    },
  },
}));

const nav = vi.hoisted(() => ({ params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({ useSearchParams: () => nav.params }));
vi.mock("@/lib/sse", () => ({ useApprovalStream: () => {} }));
vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/components/heads/useHeadRuns", () => ({ useHeadRuns: () => ({ byTask: new Map() }) }));
vi.mock("@/lib/store", () => ({
  useAppStore: Object.assign(
    (sel?: (s: { activeBoardId: string }) => unknown) =>
      sel ? sel({ activeBoardId: "board-1" }) : { activeBoardId: "board-1" },
    { setState: () => {} },
  ),
  useNotificationStore: (sel?: (s: { notifications: never[] }) => unknown) =>
    sel ? sel({ notifications: [] }) : { notifications: [] },
}));

import InboxPage from "../page";

function conv(o: Partial<Conversation> = {}): Conversation {
  return {
    thread_id: "th-1",
    participants: [
      { ref: "agent:1", kind: "agent", address: "lead", label: "Lead", harness: "omp" },
      { ref: "session:2", kind: "session", address: "claude@desk:runtimes", label: "runtimes", harness: "claude" },
    ],
    message_count: 2,
    last_message: { id: "m2", seq: 2, from: "claude@desk:runtimes", text: "Measuring, done at 15:30.", created_at: "2026-11-02T14:03:00Z" },
    last_message_at: "2026-11-02T14:03:00Z",
    state: "running",
    brake_approval_id: null,
    ...o,
  };
}

const approval = {
  id: "ap-1", board_id: "b", task_id: null, agent_id: "a", action_type: "config_change",
  description: "Stop box one", payload: null, confidence: null, status: "pending", autonomy_level: null,
  resolved_at: null, resolver_note: null, failure_reason: null, expires_at: null,
  created_at: "2026-11-02T14:00:00Z", triggered_by: null,
} as unknown as Approval;

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
  nav.params = new URLSearchParams();
  apiMock.approvals.mockResolvedValue([approval]);
  apiMock.conversations.mockResolvedValue({
    conversations: [
      conv(),
      conv({ thread_id: "th-2", state: "paused", message_count: 6, brake_approval_id: "ap-brake" }),
      conv({ thread_id: "th-3", state: "stopped" }),
    ],
  });
  apiMock.messages.mockImplementation((id: string) =>
    Promise.resolve({
      conversation: conv({ thread_id: id, state: id === "th-2" ? "paused" : "running" }),
      messages: [
        { id: "m1", seq: 1, from: "lead", text: "Is box one free?", created_at: "2026-11-02T14:01:00Z" },
        { id: "m2", seq: 2, from: "claude@desk:runtimes", text: "Measuring, done at 15:30.", created_at: "2026-11-02T14:03:00Z" },
      ],
    }),
  );
});

describe("Inbox tabs", () => {
  it("opens on For you; only For you carries the badge count", async () => {
    renderInbox();
    expect(screen.getByTestId("inbox-tab-you")).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(screen.getByTestId("inbox-tab-you-count")).toHaveTextContent("1"));
    // three conversations, one paused — none of them adds to "For you"
    await waitFor(() => expect(screen.getByTestId("inbox-tab-agents-count")).toHaveTextContent("3"));
    expect(screen.getByTestId("inbox-tab-you-count")).toHaveTextContent("1");
    expect(screen.getByText("Stop box one")).toBeInTheDocument();
    expect(screen.queryByTestId("conversation-row")).toBeNull();
  });

  it("Between agents lists who with whom, the last message and the brake", async () => {
    renderInbox();
    fireEvent.click(screen.getByTestId("inbox-tab-agents"));
    const rows = await screen.findAllByTestId("conversation-row");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("lead ↔ claude@desk:runtimes");
    expect(rows[0]).toHaveTextContent("Measuring, done at 15:30.");
    expect(rows[0]).toHaveTextContent("2 messages");
    expect(rows[0]).toHaveTextContent("info only");
    expect(within(rows[1]).getByTestId("conversation-brake")).toHaveTextContent("Brake · let it continue?");
    expect(within(rows[2]).getByTestId("conversation-stopped")).toBeInTheDocument();
    expect(within(rows[0]).queryByTestId("conversation-brake")).toBeNull();
    // the approval of "For you" is not shown on this tab
    expect(screen.queryByText("Stop box one")).toBeNull();
  });

  it("opening a conversation shows its messages marked as not from the operator", async () => {
    renderInbox();
    fireEvent.click(screen.getByTestId("inbox-tab-agents"));
    fireEvent.click((await screen.findAllByTestId("conversation-row"))[0]);
    const msgs = await screen.findAllByTestId("conversation-message");
    expect(apiMock.messages).toHaveBeenCalledWith("th-1");
    expect(msgs).toHaveLength(2);
    expect(msgs[0]).toHaveTextContent("lead");
    expect(msgs[0]).toHaveTextContent("Is box one free?");
    expect(screen.getByTestId("conversation-info-only")).toHaveTextContent(/info only.*not by you/i);
    expect(window.location.search).toBe("?tab=agents&c=th-1");

    fireEvent.click(screen.getByRole("button", { name: /between agents/i }));
    expect(await screen.findAllByTestId("conversation-row")).toHaveLength(3);
  });

  it("a brake-paused conversation says so and leads to For you", async () => {
    renderInbox();
    fireEvent.click(screen.getByTestId("inbox-tab-agents"));
    fireEvent.click((await screen.findAllByTestId("conversation-row"))[1]);
    expect(await screen.findByTestId("conversation-state")).toHaveTextContent("Paused after 6 messages without you.");
    fireEvent.click(screen.getByTestId("conversation-decide"));
    expect(screen.getByTestId("inbox-tab-you")).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("Stop box one")).toBeInTheDocument();
  });

  it("a link with ?tab=agents&c=<id> opens the conversation directly", async () => {
    nav.params = new URLSearchParams("tab=agents&c=th-2");
    renderInbox();
    expect(await screen.findAllByTestId("conversation-message")).toHaveLength(2);
    expect(apiMock.messages).toHaveBeenCalledWith("th-2");
  });

  it("empty: says nothing was said yet and that the operator only reads along", async () => {
    apiMock.conversations.mockResolvedValue({ conversations: [] });
    renderInbox();
    fireEvent.click(screen.getByTestId("inbox-tab-agents"));
    expect(await screen.findByTestId("between-empty")).toHaveTextContent("No conversations between agents yet");
    expect(screen.queryByTestId("inbox-tab-agents-count")).toBeNull();
  });
});
