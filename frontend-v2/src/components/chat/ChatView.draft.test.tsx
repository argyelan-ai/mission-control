/**
 * ChatView — the "New chat" draft (concept "Neuer Chat", P4): an empty chat
 * that does not exist yet. No history and no stream, "New chat" in the
 * header, the agent chip above the composer, and the first send handed to
 * the page (which makes the chat). The chat it became shows the first line
 * as its own echo (hand-over), so it never blinks out.
 *
 * `useChatStream` and `useRecordChat` are mocked, as in ChatView.further.test.tsx.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatView } from "./ChatView";
import { useChatStream, type UseChatStreamResult } from "@/hooks/useChatStream";
import { api } from "@/lib/api";
import type { AgentWithState } from "./TerminalPanel";
import { sendToChat, useRecordChat, type RecordChatResult } from "./useRecordChat";
import { useOpenChat } from "@/components/layout/useChatList";
import { handOverFirstMessage } from "@/lib/chatList";

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
const adminMock = vi.hoisted(() => ({ admin: true }));
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => adminMock.admin }));
vi.mock("./SpeedLine", () => ({ SpeedLine: () => <div data-testid="speed-line-stub" /> }));
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

function mkRecord(overrides: Partial<RecordChatResult> = {}): RecordChatResult {
  return { ...mkStream({ session: null }), disabled: false, missing: false, recordChats: [], ...overrides };
}

function OpenChatProbe() {
  return <span data-testid="open-chat">{useOpenChat() ?? "none"}</span>;
}

const CHIP = <div data-testid="agent-chip-stub">With Cody</div>;

function renderDraft(onSend: (text: string) => Promise<void>, agent: AgentWithState | null = AGENT) {
  return render(
    <>
      <OpenChatProbe />
      <ChatView
        agent={agent}
        hasTranscript
        detailLevel="normal"
        onDetailLevelChange={() => {}}
        centerView="terminal"
        onCenterViewChange={() => {}}
        draft={{ chip: CHIP, onSend }}
      />
    </>,
  );
}

let record: RecordChatResult;

beforeEach(() => {
  vi.clearAllMocks();
  adminMock.admin = true;
  navMock.params = new URLSearchParams("new=1");
  mockChatStream.mockReturnValue(mkStream());
  record = mkRecord();
  mockRecord.mockImplementation(() => record);
  mockSendToChat.mockResolvedValue(undefined);
});

describe("ChatView — New chat draft", () => {
  it("is an empty chat: no stream, 'New chat' in the header, the chip above the composer, focus in the input", async () => {
    renderDraft(vi.fn().mockResolvedValue(undefined));
    expect(mockChatStream).toHaveBeenLastCalledWith("agent-1", false);
    expect(mockRecord).toHaveBeenLastCalledWith(null);
    expect(screen.getByTestId("chat-header-title")).toHaveTextContent("New chat");
    expect(screen.getByTestId("chat-header-title")).not.toHaveTextContent("Cody");
    expect(screen.getByTestId("chat-empty-draft")).toHaveTextContent("Write your first message below.");
    // A stored "terminal" view never parks the draft in a terminal.
    expect(screen.queryByTestId("terminal-panel-stub")).toBeNull();
    // The chip sits in the composer, above the input pill.
    const chip = screen.getByTestId("agent-chip-stub");
    const pill = screen.getByTestId("composer-dropzone");
    expect(chip.compareDocumentPosition(pill) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Nothing to set yet, no live voice on a chat that does not exist.
    expect(screen.queryByRole("button", { name: "Chat options" })).toBeNull();
    expect(screen.queryByTestId("voice-dock")).toBeNull();
    // No state line: nothing runs until the first send.
    expect(screen.queryByText("Ready")).toBeNull();
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveFocus());
    // No row of the chat list is marked while the draft is open.
    expect(screen.getByTestId("open-chat")).toHaveTextContent("draft");
  });

  it("the first send goes to the page (which makes the chat), shown as an echo — never straight to the agent", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    renderDraft(onSend);
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox"), "Sag PONG");
    await user.keyboard("{Enter}");

    expect(onSend).toHaveBeenCalledWith("Sag PONG");
    expect(record.echoSent).toHaveBeenCalledWith("Sag PONG", false);
    expect(api.chat.sendText).not.toHaveBeenCalled();
    expect(mockSendToChat).not.toHaveBeenCalled();
  });

  it("a refused first send takes the echo back and puts the text into the input again", async () => {
    const onSend = vi.fn().mockRejectedValue(new Error("API 500"));
    renderDraft(onSend);
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox"), "Sag PONG");
    await user.keyboard("{Enter}");

    await waitFor(() => expect(record.echoFailed).toHaveBeenCalledWith("Sag PONG"));
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("Sag PONG"));
  });

  it("no admin: the chip still stands (it says new chats are off), above the admin notice", () => {
    adminMock.admin = false;
    renderDraft(vi.fn());
    const chip = screen.getByTestId("agent-chip-stub");
    const notice = screen.getByText("Read-only: only admins can type into a live session.");
    expect(chip.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("claims nothing about a chat that does not exist yet: no model or effort chip, no speed line", () => {
    // Capabilities that put the effort chip on any other composer.
    record = mkRecord({ capabilities: { effortReason: "model_no_effort", effortLevels: [], canSwitchEffort: false } as never });
    renderDraft(vi.fn());
    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(screen.queryByTestId("effort-chip")).toBeNull();
    expect(screen.queryByTestId("effort-chip-static")).toBeNull();
    expect(screen.queryByTestId("model-chip")).toBeNull();
    expect(screen.queryByTestId("model-chip-static")).toBeNull();
    expect(screen.queryByTestId("speed-line-stub")).toBeNull();
  });

  it("nobody alive: header and chip, no input to type into", () => {
    renderDraft(vi.fn(), null);
    expect(screen.getByTestId("chat-draft")).toHaveTextContent("New chat");
    expect(screen.getByTestId("agent-chip-stub")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});

describe("ChatView — the chat a draft became", () => {
  it("shows the draft's first line as its own echo, once", () => {
    navMock.params = new URLSearchParams("agent=agent-1&chat=chat-7");
    handOverFirstMessage("chat-7", "Sag PONG");
    const view = render(
      <ChatView agent={AGENT} hasTranscript detailLevel="normal" onDetailLevelChange={() => {}} centerView="chat" onCenterViewChange={() => {}} />,
    );
    expect(record.echoSent).toHaveBeenCalledTimes(1);
    expect(record.echoSent).toHaveBeenCalledWith("Sag PONG", false);
    view.unmount();
    render(
      <ChatView agent={AGENT} hasTranscript detailLevel="normal" onDetailLevelChange={() => {}} centerView="chat" onCenterViewChange={() => {}} />,
    );
    expect(record.echoSent).toHaveBeenCalledTimes(1);
  });

  it("the agent's own chat (new chats off) takes its hand-over too", () => {
    navMock.params = new URLSearchParams("agent=agent-1");
    const stream = mkStream();
    mockChatStream.mockReturnValue(stream);
    handOverFirstMessage("agent:agent-1", "Sag PONG");
    render(
      <ChatView agent={AGENT} hasTranscript detailLevel="normal" onDetailLevelChange={() => {}} centerView="chat" onCenterViewChange={() => {}} />,
    );
    expect(stream.echoSent).toHaveBeenCalledWith("Sag PONG", false);
    expect(record.echoSent).not.toHaveBeenCalled();
  });
});
