/**
 * The chat switch UI exists only while the chat record is on
 * (CHAT_RECORD_ENABLED). Off, the record answers 404 and the speed line and
 * the header line are exactly the ones before (S3 item D acceptance).
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const apiMock = vi.hoisted(() => ({
  listChats: vi.fn(),
  history: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      chatRecord: { listChats: apiMock.listChats, history: apiMock.history },
      runtimes: {
        listRows: vi.fn(async () => ({ runtimes: [{ id: "rt-1", model_identifier: "qwen38", host: null }] })),
        compatMatrix: vi.fn(async () => ({ harnesses: [], host_harnesses: [], runtimes: [] })),
      },
      hosts: { pulse: vi.fn(async () => ({ points: [], available: false })) },
    },
  };
});

import { SpeedLine } from "./SpeedLine";
import { ChatHeaderSubline } from "./ChatHeaderSubline";
import { TaskStepsAgentContext } from "@/hooks/useTaskStepDetail";

const CHAT = {
  id: "chat-1", title: "Demo", harness: "omp", model: "qwen38", legacyAgentId: "agent-1", agentDefault: true,
  nativeSessionId: null, pinned: false, archived: false, lastActivityAt: null, createdAt: null, runtimeId: "rt-1",
};

function mount(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TaskStepsAgentContext.Provider value="agent-1">{ui}</TaskStepsAgentContext.Provider>
    </QueryClientProvider>,
  );
}

const IDLE = { kind: "state", status: "idle", ts: "2026-11-19T10:00:00Z" } as never;

describe("record off (404) — today's UI exactly", () => {
  beforeEach(() => {
    apiMock.listChats.mockReset().mockRejectedValue(new Error('API 404: {"detail":{"reason":"chat_record_disabled"}}'));
    apiMock.history.mockReset();
  });

  it("the speed line stays a plain line, no picker door", async () => {
    mount(<SpeedLine runtimeId="rt-1" model="qwen38" streaming={false} />);
    const line = await screen.findByTestId("speed-line");
    await waitFor(() => expect(apiMock.listChats).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByTestId("speed-line").tagName).toBe("DIV");
    expect(line.getAttribute("aria-haspopup")).toBeNull();
    expect(apiMock.history).not.toHaveBeenCalled();
  });

  it("the header line says the state word", async () => {
    mount(<ChatHeaderSubline state={IDLE} aliveness="active" turnStartedAt={null} />);
    await waitFor(() => expect(apiMock.listChats).toHaveBeenCalled());
    expect(screen.getByTestId("chat-header-subline").textContent).toBe("Ready");
  });
});

describe("record on", () => {
  beforeEach(() => {
    apiMock.listChats.mockReset().mockResolvedValue({ chats: [CHAT] });
    apiMock.history.mockReset().mockResolvedValue({
      events: [{ id: "e1", seq: 1, sessionId: "s", event: { kind: "switch", status: "pending", path: "model_only", ts: "2026-11-19T10:00:00Z" } }],
      hasMore: false,
    });
  });

  it("the speed line is the door to the pair picker", async () => {
    mount(<SpeedLine runtimeId="rt-1" model="qwen38" streaming={false} />);
    await waitFor(() => expect(screen.getByTestId("speed-line").tagName).toBe("BUTTON"));
    fireEvent.click(screen.getByTestId("speed-line"));
    expect(await screen.findByTestId("pair-picker")).toBeTruthy();
  });

  it("an open switch replaces the state word in the header line", async () => {
    mount(<ChatHeaderSubline state={IDLE} aliveness="active" turnStartedAt={null} />);
    await waitFor(() => expect(screen.getByTestId("chat-header-subline").dataset.kind).toBe("switching"));
    expect(screen.getByTestId("chat-header-subline").textContent).toBe("Switching …");
  });
});
