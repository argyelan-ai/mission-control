/**
 * ChatView header — the chat's name is the page's h1 (a11y audit: the chat
 * page had no top-level heading), with the same look as before.
 * Setup copied from ChatView.further.test.tsx (same mocks).
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

describe("ChatView — header name is the h1", () => {
  it("names the chat in an h1 inside the header, same size as before", () => {
    renderView();
    const title = screen.getByTestId("chat-header-title");
    const h1 = screen.getByRole("heading", { level: 1, name: "Cody" });
    expect(title).toContainElement(h1);
    expect(h1.className).toMatch(/\btext-base\b/);
    expect(h1.className).toMatch(/\btruncate\b/);
  });
});
