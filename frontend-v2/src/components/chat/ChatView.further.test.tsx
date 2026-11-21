/**
 * ChatView — a further chat of an agent (chats slice S2): `?chat=<id>` reads
 * the chat record and sends with `chatId`; `/new` that came back as a new
 * chat (201 `newChat`) opens it; without the record flag (404) or without
 * `?chat=` the agent's own chat is exactly as before.
 *
 * `useChatStream` and `useRecordChat` are both mocked — their own wiring is
 * covered by useChatStream.test.ts and useRecordChat.test.tsx.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatView } from "./ChatView";
import { useChatStream, type UseChatStreamResult } from "@/hooks/useChatStream";
import { api } from "@/lib/api";
import type { AgentWithState } from "./TerminalPanel";
import type { MessageEvent } from "@/lib/chatTypes";
import type { RecordChat } from "@/lib/api";
import { sendToChat, useRecordChat, type RecordChatResult } from "./useRecordChat";
import { useOpenChat } from "@/components/layout/useChatList";

vi.mock("@/hooks/useChatStream", () => ({ useChatStream: vi.fn() }));
vi.mock("./useRecordChat", async () => {
  const actual = await vi.importActual<typeof import("./useRecordChat")>("./useRecordChat");
  return { ...actual, useRecordChat: vi.fn(), sendToChat: vi.fn() };
});
vi.mock("@/hooks/useChatTasks", () => ({ useChatTasks: vi.fn(() => ({ data: null, settled: false, error: false })) }));
vi.mock("@/hooks/useTaskSteps", () => ({ useTaskSteps: vi.fn(() => new Map()) }));
vi.mock("@/hooks/useEngineActivity", () => ({ useEngineBusy: vi.fn(() => false) }));
vi.mock("@/hooks/useCommsDirectory", () => ({ useCommsDirectory: vi.fn(() => ({ data: undefined, isError: false })) }));
vi.mock("@/hooks/useHeadTranscript", () => ({
  useHeadTranscript: vi.fn(() => ({
    events: [], subagentRuns: [], state: null, usage: null, session: null, hasMore: false,
    connected: false, loading: false, error: null, capabilities: null, pendingEchoes: [],
    echoSent: vi.fn(), echoFailed: vi.fn(), echoAgentStarting: vi.fn(), withdrawQueued: vi.fn(() => []),
    awaitingResponse: false, preview: null,
  })),
  useHeadTranscriptMeta: vi.fn(() => ({ source: null, reader: null, reason: null })),
}));
vi.mock("@/lib/api", () => ({
  api: {
    chat: {
      sendText: vi.fn().mockResolvedValue(undefined),
      sendKeys: vi.fn().mockResolvedValue(undefined),
      setEffort: vi.fn().mockResolvedValue(undefined),
      clearQueue: vi.fn().mockResolvedValue({ dropped: [] }),
    },
  },
}));
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => true }));
const navMock = vi.hoisted(() => ({ params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({ useSearchParams: () => navMock.params }));
vi.mock("@/components/layout/ChatDrawer", () => ({ DrawerButton: () => null }));
vi.mock("@/components/voice/VoiceWidget", () => ({ VoiceButton: () => null }));
// The live voice dock wraps only the agent's own composer — marked, so a
// test can see which of the two composers stands.
vi.mock("@/components/voice/live/VoiceLive", () => ({
  VoiceLiveDock: ({ children }: { children: (slot: null) => React.ReactNode }) => (
    <div data-testid="voice-dock">{children(null)}</div>
  ),
}));
vi.mock("./ChatChangesInline", () => ({ ChatChangesInline: () => null }));
vi.mock("./TerminalPanel", async () => {
  const actual = await vi.importActual<typeof import("./TerminalPanel")>("./TerminalPanel");
  return { ...actual, TerminalPanel: () => <div data-testid="terminal-panel-stub" /> };
});

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  class MockResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
});

const mockChatStream = vi.mocked(useChatStream);
const mockRecord = vi.mocked(useRecordChat);
const mockSendToChat = vi.mocked(sendToChat);

const AGENT = {
  id: "agent-1",
  name: "Cody",
  status: "idle",
  agent_runtime: "cli-bridge",
  headless_chat: true,
  tasks_in_own_session: false,
  model: null,
} as unknown as AgentWithState;

function msg(uuid: string, role: MessageEvent["role"], text: string): MessageEvent {
  return { kind: "message", uuid, ts: "2026-11-19T10:00:00Z", role, text, model: null, sidechain: false };
}

function mkStream(overrides: Partial<UseChatStreamResult> = {}): UseChatStreamResult {
  return {
    events: [],
    toolOutputs: {},
    elicitationResults: {},
    mode: null,
    subagentRuns: [],
    state: { kind: "state", status: "idle", prompt: null },
    usage: null,
    session: { sessionId: "s1", live: true, startedAt: "2026-11-19T09:00:00Z" },
    hasMore: false,
    connected: true,
    loading: false,
    error: null,
    capabilities: null,
    pendingEchoes: [],
    echoSent: vi.fn(),
    echoFailed: vi.fn(),
    echoAgentStarting: vi.fn(),
    awaitingResponse: false,
    preview: null,
    todos: null,
    withdrawQueued: vi.fn(() => []),
    ...overrides,
  };
}

const CHATS: RecordChat[] = [
  {
    id: "chat-own", title: "Cody", harness: "omp", model: null, legacyAgentId: "agent-1", agentDefault: true,
    nativeSessionId: null, pinned: false, archived: false, lastActivityAt: null, createdAt: "2026-11-01T00:00:00Z",
  },
  {
    id: "chat-2", title: "Cody", harness: "omp", model: null, legacyAgentId: "agent-1", agentDefault: false,
    nativeSessionId: null, pinned: false, archived: false, lastActivityAt: null, createdAt: "2026-11-19T08:00:00Z",
  },
];

function mkRecord(overrides: Partial<RecordChatResult> = {}): RecordChatResult {
  return {
    ...mkStream({ session: null }),
    events: [msg("r1", "user", "only in chat two"), msg("r2", "assistant", "answer in chat two")],
    disabled: false,
    missing: false,
    recordChats: CHATS,
    ...overrides,
  };
}

function renderView(props: Partial<React.ComponentProps<typeof ChatView>> = {}) {
  return render(
    <ChatView
      agent={AGENT}
      hasTranscript
      detailLevel="normal"
      onDetailLevelChange={() => {}}
      centerView="chat"
      onCenterViewChange={() => {}}
      {...props}
    />,
  );
}

function OpenChatProbe() {
  return <span data-testid="open-chat">{useOpenChat() ?? "none"}</span>;
}

async function send(text: string) {
  const user = userEvent.setup();
  const box = screen.getByRole("textbox");
  await user.type(box, text);
  await user.keyboard("{Enter}");
}

beforeEach(() => {
  vi.clearAllMocks();
  navMock.params = new URLSearchParams();
  mockChatStream.mockReturnValue(mkStream({ events: [msg("a1", "assistant", "main chat answer")] }));
  mockRecord.mockReturnValue(mkRecord());
  mockSendToChat.mockResolvedValue(undefined);
});

describe("ChatView — further chat (?chat=)", () => {
  it("without ?chat= it is the agent's own chat, exactly as before", () => {
    renderView();
    expect(mockRecord).toHaveBeenCalledWith(null);
    expect(mockChatStream).toHaveBeenLastCalledWith("agent-1", true);
    expect(screen.getByText("main chat answer")).toBeInTheDocument();
    expect(screen.queryByText("answer in chat two")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-header-title")).toHaveTextContent("Cody");
    expect(screen.getByTestId("chat-header-title")).not.toHaveTextContent("chat 2");
    expect(screen.getByTestId("voice-dock")).toBeInTheDocument();
  });

  it("reads ?chat= : the record's history, its own header name, no live stream", () => {
    navMock.params = new URLSearchParams("agent=agent-1&chat=chat-2");
    renderView();
    expect(mockRecord).toHaveBeenCalledWith("chat-2");
    // The agent stream is switched off — no second subscription for a chat
    // that is not on screen.
    expect(mockChatStream).toHaveBeenLastCalledWith("agent-1", false);
    expect(screen.getByText("answer in chat two")).toBeInTheDocument();
    expect(screen.queryByText("main chat answer")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-header-title")).toHaveTextContent("Cody · chat 2");
    // Live voice acts on the agent's own chat — not offered here.
    expect(screen.queryByTestId("voice-dock")).not.toBeInTheDocument();
  });

  it("ignores ?chat= of another agent (a desktop pick does not touch the URL)", () => {
    navMock.params = new URLSearchParams("agent=agent-9&chat=chat-2");
    renderView();
    expect(mockRecord).toHaveBeenCalledWith(null);
    expect(screen.getByText("main chat answer")).toBeInTheDocument();
  });

  it("record flag off (404): ?chat= shows the agent's own chat, unchanged", () => {
    navMock.params = new URLSearchParams("agent=agent-1&chat=chat-2");
    mockRecord.mockReturnValue(mkRecord({ disabled: true, events: [] }));
    renderView();
    expect(mockChatStream).toHaveBeenLastCalledWith("agent-1", true);
    expect(screen.getByText("main chat answer")).toBeInTheDocument();
    expect(screen.getByTestId("chat-header-title")).not.toHaveTextContent("chat 2");
    expect(screen.getByTestId("voice-dock")).toBeInTheDocument();
  });

  it("chat not there (404 'Chat not found'): own chat shows and the page is asked to drop &chat=", () => {
    navMock.params = new URLSearchParams("agent=agent-1&chat=chat-gone");
    mockRecord.mockReturnValue(mkRecord({ missing: true, events: [] }));
    const onChatMissing = vi.fn();
    renderView({ onChatMissing });
    expect(onChatMissing).toHaveBeenCalledTimes(1);
    expect(mockChatStream).toHaveBeenLastCalledWith("agent-1", true);
    expect(screen.getByText("main chat answer")).toBeInTheDocument();
    expect(screen.getByTestId("chat-header-title")).not.toHaveTextContent("chat 2");
  });

  it("a further chat that is there never asks to drop &chat=", () => {
    navMock.params = new URLSearchParams("agent=agent-1&chat=chat-2");
    const onChatMissing = vi.fn();
    renderView({ onChatMissing });
    expect(onChatMissing).not.toHaveBeenCalled();
  });

  it("sends into the further chat with its chatId — never into the agent's own", async () => {
    navMock.params = new URLSearchParams("agent=agent-1&chat=chat-2");
    const rec = mkRecord();
    mockRecord.mockReturnValue(rec);
    renderView();
    await send("hello two");
    await waitFor(() => expect(mockSendToChat).toHaveBeenCalledWith("agent-1", "chat-2", "hello two"));
    expect(api.chat.sendText).not.toHaveBeenCalled();
    expect(rec.echoSent).toHaveBeenCalledWith("hello two", false);
  });

  it("the agent's own chat still sends without a chatId", async () => {
    renderView();
    await send("hello main");
    await waitFor(() => expect(api.chat.sendText).toHaveBeenCalledWith("agent-1", "hello main"));
    expect(mockSendToChat).not.toHaveBeenCalled();
  });

  it("/new in the agent's own chat that became a new chat (201 newChat) opens it", async () => {
    const onOpenChat = vi.fn();
    vi.mocked(api.chat.sendText).mockResolvedValueOnce({ newChat: { id: "chat-3" } } as unknown as void);
    renderView({ onOpenChat });
    await send("/new");
    await waitFor(() => expect(onOpenChat).toHaveBeenCalledWith("chat-3"));
  });

  it("/new inside a further chat opens the next new chat too", async () => {
    const onOpenChat = vi.fn();
    navMock.params = new URLSearchParams("agent=agent-1&chat=chat-2");
    mockSendToChat.mockResolvedValueOnce({ newChat: { id: "chat-4" } });
    renderView({ onOpenChat });
    await send("/new");
    await waitFor(() => expect(onOpenChat).toHaveBeenCalledWith("chat-4"));
  });

  it("a plain reply (204) opens nothing", async () => {
    const onOpenChat = vi.fn();
    renderView({ onOpenChat });
    await send("just text");
    await waitFor(() => expect(api.chat.sendText).toHaveBeenCalled());
    await act(async () => {});
    expect(onOpenChat).not.toHaveBeenCalled();
  });

  it("tells the chat list which further chat is open — and forgets it on leave", () => {
    navMock.params = new URLSearchParams("agent=agent-1&chat=chat-2");
    const { unmount } = render(<OpenChatProbe />);
    const view = renderView();
    expect(screen.getByTestId("open-chat")).toHaveTextContent("chat-2");
    view.unmount();
    expect(screen.getByTestId("open-chat")).toHaveTextContent("none");
    unmount();
  });

  it("the agent's own chat marks no further chat as open", () => {
    render(<OpenChatProbe />);
    renderView();
    expect(screen.getByTestId("open-chat")).toHaveTextContent("none");
  });
});

describe("ChatView — a new, empty further chat (New chat → pick)", () => {
  beforeEach(() => {
    navMock.params = new URLSearchParams("agent=agent-1&chat=chat-2");
    mockRecord.mockReturnValue(mkRecord({ events: [] }));
  });

  it("says it is a new chat with the agent — not the terminal hint of the agent's own chat", () => {
    renderView();
    const empty = screen.getByTestId("chat-empty-new");
    expect(empty).toHaveTextContent("New chat with Cody");
    expect(empty).toHaveTextContent("Write your first message below.");
    expect(empty).not.toHaveTextContent("terminal");
    expect(screen.queryByTestId("chat-empty")).not.toBeInTheDocument();
  });

  it("opens with the composer focused", async () => {
    renderView();
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveFocus());
  });

  it("the agent's own empty chat keeps its old empty state and does not grab focus", () => {
    navMock.params = new URLSearchParams();
    mockChatStream.mockReturnValue(mkStream({ events: [] }));
    renderView();
    expect(screen.getByTestId("chat-empty")).toHaveTextContent("No messages yet");
    expect(screen.getByRole("textbox")).not.toHaveFocus();
  });

  it("the first message goes into the new chat (chatId), the agent's own chat gets nothing", async () => {
    renderView();
    await send("hi");
    await waitFor(() => expect(mockSendToChat).toHaveBeenCalledWith("agent-1", "chat-2", "hi"));
    expect(api.chat.sendText).not.toHaveBeenCalled();
  });
});

