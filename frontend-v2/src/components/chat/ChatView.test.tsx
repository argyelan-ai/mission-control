/**
 * ChatView — Task B6 vitest (revised: Terminal moved from a side panel to a
 * center-view toggle in ChatView's own header, next to the detail switcher).
 *
 * Coverage: the Chat/Terminal header toggle switches the center content,
 * no-transcript agents (and the belt-and-braces runtime-404 case) force
 * terminal mode and disable the Chat segment, detail-level filtering
 * (Kompakt/Normal/Ausführlich), the approval card only on a permission
 * prompt, and outbound actions (send / stop / answer) reaching
 * `api.chat.*` with the right arguments.
 *
 * `useChatStream` and `TerminalPanel` are both mocked — the stream's own
 * reducer/hook wiring is covered by useChatStream.test.ts, and
 * TerminalPanel's xterm/WebSocket machinery is out of scope here (it's a
 * verbatim move covered by its own future test surface / manual live-gate).
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatView } from "./ChatView";
import { useChatStream, type UseChatStreamResult } from "@/hooks/useChatStream";
import { api } from "@/lib/api";
import type { AgentWithState } from "./TerminalPanel";
import type { MessageEvent, ThinkingEvent, ToolEvent } from "@/lib/chatTypes";

vi.mock("@/hooks/useChatStream", () => ({ useChatStream: vi.fn() }));
vi.mock("@/lib/api", () => ({
  api: {
    chat: {
      sendText: vi.fn().mockResolvedValue(undefined),
      sendKeys: vi.fn().mockResolvedValue(undefined),
    },
  },
}));
vi.mock("./TerminalPanel", async () => {
  const actual = await vi.importActual<typeof import("./TerminalPanel")>("./TerminalPanel");
  return {
    ...actual,
    TerminalPanel: ({ agent }: { agent: { name: string } }) => (
      <div data-testid="terminal-panel-stub">Terminal-Panel: {agent.name}</div>
    ),
  };
});

// cmdk's <Command.List> (inside Composer) reaches for ResizeObserver and
// scrollIntoView — neither exists in jsdom (same stub as Composer.test.tsx).
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  class MockResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
});

const mockUseChatStream = vi.mocked(useChatStream);

function mkAgent(overrides: Partial<AgentWithState> = {}): AgentWithState {
  return {
    id: "agent-1",
    board_id: null,
    name: "Cody",
    role: null,
    emoji: null,
    status: "idle",
    model: null,
    secret_id: null,
    is_board_lead: false,
    heartbeat_config: { interval: "5m", target: "boss" },
    skills: [],
    skill_filter: null,
    cli_plugins: null,
    cli_skills: null,
    mcp_servers: null,
    scopes: [],
    identity_md: null,
    soul_md: null,
    tools_md: null,
    heartbeat_md: null,
    rules_md: null,
    memory_md: null,
    last_seen_at: null,
    last_task_activity_at: null,
    current_task_id: null,
    context_tokens: 0,
    context_max: 200000,
    session_message_count: 0,
    total_tasks_completed: 0,
    total_compactions: 0,
    template_id: null,
    workspace_path: null,
    provision_status: "local",
    provisioned_at: null,
    archived_at: null,
    discord_channel_id: null,
    discord_channel_name: null,
    last_trigger_at: null,
    last_dispatch_error: null,
    run_state: "idle",
    operational_mode: "active",
    agent_runtime: "cli-bridge",
    runtime_id: null,
    pending_runtime_sync: false,
    harness: null,
    runtime_switchable: false,
    runtime_switch_blocked_reason: null,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    container_state: "running",
    ...overrides,
  };
}

function mkStream(overrides: Partial<UseChatStreamResult> = {}): UseChatStreamResult {
  return {
    events: [],
    state: null,
    usage: null,
    session: { sessionId: "s1", live: true, startedAt: "2026-08-15T10:00:00Z" },
    hasMore: false,
    connected: true,
    loading: false,
    error: null,
    ...overrides,
  };
}

const MSG: MessageEvent = {
  kind: "message",
  uuid: "u1",
  ts: "2026-08-15T10:00:00Z",
  role: "assistant",
  text: "Hallo!",
  model: "claude-sonnet-4-6",
  sidechain: false,
};

const TOOL: ToolEvent = {
  kind: "tool",
  uuid: "u2",
  ts: "2026-08-15T10:00:01Z",
  name: "Read",
  title: "Read foo.py",
  detail: { file_path: "/foo.py" },
  toolUseId: "tu-1",
  result: null,
  status: "done",
  stats: null,
  sidechain: false,
};

const THINKING: ThinkingEvent = {
  kind: "thinking",
  uuid: "u3",
  ts: "2026-08-15T10:00:02Z",
  text: "Hmm, let me check...",
  sidechain: false,
};

const noop = () => {};

function renderChatView(overrides: Partial<React.ComponentProps<typeof ChatView>> = {}) {
  return render(
    <ChatView
      agent={mkAgent()}
      hasTranscript
      detailLevel="normal"
      onDetailLevelChange={noop}
      centerView="chat"
      onCenterViewChange={noop}
      {...overrides}
    />
  );
}

describe("ChatView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the chat timeline in chat mode", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
    renderChatView();
    expect(screen.getByText("Hallo!")).toBeInTheDocument();
    expect(screen.queryByTestId("terminal-panel-stub")).not.toBeInTheDocument();
  });

  it("switching the header toggle to Terminal swaps the center content", async () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
    const onCenterViewChange = vi.fn();
    const user = userEvent.setup();
    renderChatView({ onCenterViewChange });

    await user.click(screen.getByRole("button", { name: "Terminal" }));
    expect(onCenterViewChange).toHaveBeenCalledWith("terminal");
  });

  it("centerView='terminal' renders TerminalPanel instead of the timeline/composer", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
    renderChatView({ centerView: "terminal" });

    expect(screen.getByTestId("terminal-panel-stub")).toHaveTextContent("Terminal-Panel: Cody");
    expect(screen.queryByText("Hallo!")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Nachricht an den Agenten…")).not.toBeInTheDocument();
  });

  it("switching back to Chat calls onCenterViewChange('chat')", async () => {
    mockUseChatStream.mockReturnValue(mkStream());
    const onCenterViewChange = vi.fn();
    const user = userEvent.setup();
    renderChatView({ centerView: "terminal", onCenterViewChange });

    await user.click(screen.getByRole("button", { name: "Chat" }));
    expect(onCenterViewChange).toHaveBeenCalledWith("chat");
  });

  it("hides the detail-level switcher while in terminal mode", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    renderChatView({ centerView: "terminal" });
    expect(screen.queryByRole("button", { name: "Kompakt" })).not.toBeInTheDocument();
  });

  it("shows the detail-level switcher in chat mode", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    renderChatView({ centerView: "chat" });
    expect(screen.getByRole("button", { name: "Kompakt" })).toBeInTheDocument();
  });

  it("no-transcript agents force terminal mode and disable the Chat segment", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    renderChatView({ hasTranscript: false, centerView: "chat" });

    expect(screen.getByTestId("terminal-panel-stub")).toBeInTheDocument();
    const chatButton = screen.getByRole("button", { name: "Chat" });
    expect(chatButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "Terminal" })).toHaveAttribute("aria-pressed", "true");
  });

  it("a runtime no_transcript 404 also forces terminal mode, even if hasTranscript was true", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({ error: new Error('API 404: {"reason":"no_transcript"}') })
    );
    renderChatView({ hasTranscript: true, centerView: "chat" });

    expect(screen.getByTestId("terminal-panel-stub")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Chat" })).toBeDisabled();
  });

  it("does not fetch the stream at all when hasTranscript is false", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    renderChatView({ hasTranscript: false });
    expect(mockUseChatStream).toHaveBeenCalledWith("agent-1", false);
  });

  it("Kompakt hides tool and thinking rows entirely", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG, TOOL, THINKING] }));
    renderChatView({ detailLevel: "compact" });

    expect(screen.getByText("Hallo!")).toBeInTheDocument();
    expect(screen.queryByText("Read foo.py")).not.toBeInTheDocument();
    expect(screen.queryByText("Denkt nach…")).not.toBeInTheDocument();
  });

  it("Normal shows tool/thinking rows collapsed by default", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG, TOOL, THINKING] }));
    renderChatView({ detailLevel: "normal" });

    expect(screen.getByText("Read foo.py")).toBeInTheDocument();
    expect(screen.getByText("Denkt nach…")).toBeInTheDocument();
    // Collapsed: the tool's JSON detail block isn't rendered until clicked.
    expect(screen.queryByText(/file_path/)).not.toBeInTheDocument();
  });

  it("Ausführlich expands tool/thinking rows by default", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG, TOOL, THINKING] }));
    renderChatView({ detailLevel: "verbose" });

    expect(screen.getByText(/file_path/)).toBeInTheDocument();
    expect(screen.getByText("Hmm, let me check...")).toBeInTheDocument();
  });

  it("clicking a detail-level button calls onDetailLevelChange", async () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
    const onChange = vi.fn();
    const user = userEvent.setup();
    renderChatView({ onDetailLevelChange: onChange });

    await user.click(screen.getByRole("button", { name: "Ausführlich" }));
    expect(onChange).toHaveBeenCalledWith("verbose");
  });

  it("shows the ApprovalCard only when state.status is permission_prompt", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        state: {
          kind: "state",
          status: "permission_prompt",
          prompt: { question: "Datei löschen?", options: [{ key: "y", label: "Ja" }] },
        },
      })
    );
    renderChatView();

    expect(screen.getByText("Datei löschen?")).toBeInTheDocument();
  });

  it("the ApprovalCard's terminal escape hatch calls onCenterViewChange('terminal')", async () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        state: {
          kind: "state",
          status: "permission_prompt",
          prompt: { question: "Löschen?", options: [{ key: "1", label: "Ja" }] },
        },
      })
    );
    const onCenterViewChange = vi.fn();
    const user = userEvent.setup();
    renderChatView({ onCenterViewChange });

    await user.click(screen.getByText("Im Terminal prüfen"));
    expect(onCenterViewChange).toHaveBeenCalledWith("terminal");
  });

  it("answering the approval sends the bare key via api.chat.sendKeys (no Enter)", async () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        state: {
          kind: "state",
          status: "permission_prompt",
          prompt: { question: "Löschen?", options: [{ key: "1", label: "Ja" }] },
        },
      })
    );
    const user = userEvent.setup();
    renderChatView();

    await user.click(screen.getByRole("button", { name: "Ja" }));
    expect(api.chat.sendKeys).toHaveBeenCalledWith("agent-1", ["1"]);
    expect(api.chat.sendKeys).toHaveBeenCalledTimes(1);
  });

  it("sending a composer message calls api.chat.sendText", async () => {
    mockUseChatStream.mockReturnValue(mkStream());
    const user = userEvent.setup();
    renderChatView();

    await user.type(screen.getByPlaceholderText("Nachricht an den Agenten…"), "Hi");
    await user.click(screen.getByRole("button", { name: "Senden" }));
    expect(api.chat.sendText).toHaveBeenCalledWith("agent-1", "Hi");
  });

  it("stopping sends Escape via api.chat.sendKeys", async () => {
    mockUseChatStream.mockReturnValue(mkStream({ state: { kind: "state", status: "working", prompt: null } }));
    const user = userEvent.setup();
    renderChatView();

    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(api.chat.sendKeys).toHaveBeenCalledWith("agent-1", ["Escape"]);
  });

  it("shows a neutral placeholder when no agent is selected", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    render(
      <ChatView
        agent={null}
        hasTranscript={false}
        detailLevel="normal"
        onDetailLevelChange={noop}
        centerView="chat"
        onCenterViewChange={noop}
      />
    );
    expect(screen.getByText("Wähle eine Session in der Seitenleiste.")).toBeInTheDocument();
  });
});
