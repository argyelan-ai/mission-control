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
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatView, buildTimelineItems, modelBadgeUuids, ACTIVITY_GROUP_MIN_SIZE } from "./ChatView";
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
      // The Composer rendered inside ChatView reaches for this one directly
      // (effort switching); stubbed so the mock stays a faithful stand-in even
      // though no test here drives the chip.
      setEffort: vi.fn().mockResolvedValue(undefined),
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
    capabilities: null,
    pendingEchoes: [],
    echoSent: vi.fn(),
    echoFailed: vi.fn(),
    awaitingResponse: false,
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

// ── Grouping logic ─────────────────────────────────────────────────────────
// Pure function, tested directly: the render tests below only need to prove
// the wiring, not re-derive every boundary case through the DOM.

function mkTool(overrides: Partial<ToolEvent> = {}): ToolEvent {
  return { ...TOOL, uuid: `t-${Math.random()}`, toolUseId: `tu-${Math.random()}`, ...overrides };
}

function mkThinking(overrides: Partial<ThinkingEvent> = {}): ThinkingEvent {
  return { ...THINKING, uuid: `th-${Math.random()}`, ...overrides };
}

function mkMsg(overrides: Partial<MessageEvent> = {}): MessageEvent {
  return { ...MSG, uuid: `m-${Math.random()}`, ...overrides };
}

describe("buildTimelineItems", () => {
  it("collapses consecutive tool/thinking events into one activity run", () => {
    const items = buildTimelineItems([mkTool(), mkThinking(), mkTool()]);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "activity" });
    expect(items[0].kind === "activity" && items[0].events).toHaveLength(3);
  });

  it("ends a run at an assistant message", () => {
    const items = buildTimelineItems([mkTool(), mkTool(), mkMsg(), mkTool(), mkTool()]);
    expect(items.map((i) => i.kind)).toEqual(["activity", "single", "activity"]);
  });

  it("ends a run at a user message", () => {
    const items = buildTimelineItems([mkTool(), mkTool(), mkMsg({ role: "user", text: "Weiter" }), mkTool(), mkTool()]);
    expect(items.map((i) => i.kind)).toEqual(["activity", "single", "activity"]);
  });

  it("ends a run at a slash command", () => {
    const cmd = { kind: "command", uuid: "c1", ts: MSG.ts, command: "/clear" } as const;
    const items = buildTimelineItems([mkTool(), mkTool(), cmd, mkTool(), mkTool()]);
    expect(items.map((i) => i.kind)).toEqual(["activity", "single", "activity"]);
  });

  it(`emits runs shorter than ${ACTIVITY_GROUP_MIN_SIZE} as plain rows`, () => {
    const items = buildTimelineItems([mkMsg(), mkTool(), mkMsg()]);
    expect(items.map((i) => i.kind)).toEqual(["single", "single", "single"]);
  });

  it("keeps sidechain runs separate from top-level activity runs", () => {
    const items = buildTimelineItems([
      mkTool(),
      mkTool(),
      mkTool({ sidechain: true }),
      mkTool({ sidechain: true }),
      mkTool(),
      mkTool(),
    ]);
    expect(items.map((i) => i.kind)).toEqual(["activity", "sidechain", "activity"]);
  });

  it("keeps a single sidechain event grouped (SubagentGroup owns its own header)", () => {
    const items = buildTimelineItems([mkTool({ sidechain: true })]);
    expect(items.map((i) => i.kind)).toEqual(["sidechain"]);
  });

  it("preserves event order across mixed input", () => {
    const first = mkMsg({ text: "A" });
    const last = mkMsg({ text: "B" });
    const items = buildTimelineItems([first, mkTool(), mkTool(), last]);
    expect(items[0]).toMatchObject({ kind: "single", event: first });
    expect(items[2]).toMatchObject({ kind: "single", event: last });
  });

  it("returns nothing for an empty timeline", () => {
    expect(buildTimelineItems([])).toEqual([]);
  });
});

describe("modelBadgeUuids", () => {
  it("flags the first assistant message so the reader knows what is answering", () => {
    const a = mkMsg({ model: "sonnet" });
    expect(modelBadgeUuids([a])).toEqual(new Set([a.uuid]));
  });

  it("does not repeat the model on every turn", () => {
    const a = mkMsg({ model: "sonnet" });
    const b = mkMsg({ model: "sonnet" });
    expect(modelBadgeUuids([a, b])).toEqual(new Set([a.uuid]));
  });

  it("flags the turn where the model actually changed", () => {
    const a = mkMsg({ model: "sonnet" });
    const b = mkMsg({ model: "sonnet" });
    const c = mkMsg({ model: "opus" });
    expect(modelBadgeUuids([a, b, c])).toEqual(new Set([a.uuid, c.uuid]));
  });

  it("ignores user messages and events without a model", () => {
    const user = mkMsg({ role: "user", model: null, text: "Wechsle das Modell" });
    const a = mkMsg({ model: null });
    expect(modelBadgeUuids([user, a, mkTool()])).toEqual(new Set());
  });

  it("renders the model line only on the changed turn", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        events: [
          { ...MSG, uuid: "m1", text: "Erste", model: "sonnet" },
          { ...MSG, uuid: "m2", text: "Zweite", model: "sonnet" },
          { ...MSG, uuid: "m3", text: "Dritte", model: "opus" },
        ],
      })
    );
    renderChatView();

    expect(screen.getByText("sonnet")).toBeInTheDocument();
    expect(screen.getByText("opus")).toBeInTheDocument();
    // Once each, not once per message.
    expect(screen.getAllByText("sonnet")).toHaveLength(1);
  });
});

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
    // No group chip either — Kompakt hides the activity entirely, it doesn't
    // trade a wall of rows for a wall of chips.
    expect(screen.queryByTestId("tool-group")).not.toBeInTheDocument();
  });

  it("Normal collapses a run of tool/thinking events into one group chip", async () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG, TOOL, THINKING] }));
    const user = userEvent.setup();
    renderChatView({ detailLevel: "normal" });

    // The wall of rows is gone by default — one summary chip stands in for it.
    const chip = screen.getByRole("button", { name: /1 Tool verwendet, nachgedacht/ });
    expect(screen.queryByText("Read foo.py")).not.toBeInTheDocument();
    expect(screen.queryByText("Denkt nach…")).not.toBeInTheDocument();

    await user.click(chip);
    expect(screen.getByText("Read foo.py")).toBeInTheDocument();
    expect(screen.getByText("Denkt nach…")).toBeInTheDocument();
    // Rows themselves are still collapsed at Normal — that's Ausführlich's job.
    expect(screen.queryByText(/file_path/)).not.toBeInTheDocument();
  });

  it("Normal leaves a lone tool event as a plain row (no group chip)", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG, TOOL, { ...MSG, uuid: "u9", text: "Fertig." }] }));
    renderChatView({ detailLevel: "normal" });

    expect(screen.getByText("Read foo.py")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-group")).not.toBeInTheDocument();
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

  // Regression: the mobile stack keeps the off-screen pane mounted with
  // `display: none`, where the scroll container measures 0 and the
  // scroll-to-bottom effect is a silent no-op. Nothing re-triggers it when the
  // pane becomes visible, so the chat opened at the very top of a long
  // history. A ResizeObserver on the container catches the box appearing.
  it("scrolls to the bottom when the timeline box changes size (pane became visible)", () => {
    const original = window.ResizeObserver;
    const observed: Element[] = [];
    let fire: (() => void) | null = null;
    class CapturingResizeObserver {
      constructor(cb: ResizeObserverCallback) {
        fire = () => cb([], this as unknown as ResizeObserver);
      }
      observe(el: Element) { observed.push(el); }
      unobserve() {}
      disconnect() {}
    }
    window.ResizeObserver = CapturingResizeObserver as unknown as typeof ResizeObserver;

    try {
      mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
      renderChatView();

      expect(observed).toHaveLength(1);
      const el = observed[0] as HTMLElement;
      // jsdom reports 0 for every layout metric, so stand in for a long history.
      Object.defineProperty(el, "scrollHeight", { value: 5000, configurable: true });
      el.scrollTop = 0;

      fire!();
      expect(el.scrollTop).toBe(5000);
    } finally {
      window.ResizeObserver = original;
    }
  });

  // ── Mobile stack header ───────────────────────────────────────────────────

  it("shows no back chevron when the caller has no list to go back to (desktop)", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    renderChatView();
    expect(screen.queryByRole("button", { name: "Zurück zur Sessionliste" })).not.toBeInTheDocument();
  });

  it("the back chevron reports the intent to return to the list", async () => {
    mockUseChatStream.mockReturnValue(mkStream());
    const onBack = vi.fn();
    const user = userEvent.setup();
    renderChatView({ onBack });

    await user.click(screen.getByRole("button", { name: "Zurück zur Sessionliste" }));
    expect(onBack).toHaveBeenCalled();
  });

  it("shows the context line under the agent name", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    renderChatView({ contextLine: "Login reparieren" });
    expect(screen.getByText("Login reparieren")).toBeInTheDocument();
  });

  it("keeps the options sheet closed until the header button is used", async () => {
    mockUseChatStream.mockReturnValue(mkStream());
    const user = userEvent.setup();
    renderChatView();

    expect(screen.queryByTestId("chat-options-sheet")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Chat-Optionen" }));
    expect(screen.getByTestId("chat-options-sheet")).toBeInTheDocument();
  });

  it("passes the effective view to the sheet, so a forced-terminal agent can't pick Chat there", async () => {
    mockUseChatStream.mockReturnValue(mkStream());
    const user = userEvent.setup();
    renderChatView({ hasTranscript: false, centerView: "chat" });

    await user.click(screen.getByRole("button", { name: "Chat-Optionen" }));
    expect(screen.getByRole("radio", { name: /Chat/ })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /Terminal/ })).toHaveAttribute("aria-checked", "true");
  });

  // ── Optimistic echo ───────────────────────────────────────────────────────
  // The bubble must exist in the frame the send happens, not a tailer poll
  // later. ChatView's job here is narrow: echo BEFORE the request, drop the
  // echo if the request fails, render pending echoes last.

  it("echoes the message before the request is even dispatched", async () => {
    const echoSent = vi.fn();
    let sendResolved = false;
    vi.mocked(api.chat.sendText).mockImplementation(() => {
      // Asserted inside the request: the echo must already have happened.
      expect(echoSent).toHaveBeenCalledWith("los gehts");
      sendResolved = true;
      return Promise.resolve(undefined);
    });
    mockUseChatStream.mockReturnValue(mkStream({ echoSent }));
    const user = userEvent.setup();
    renderChatView();

    await user.type(screen.getByPlaceholderText("Nachricht an den Agenten…"), "los gehts");
    await user.click(screen.getByRole("button", { name: "Senden" }));

    expect(sendResolved).toBe(true);
  });

  it("withdraws the echo when the send fails", async () => {
    const echoFailed = vi.fn();
    vi.mocked(api.chat.sendText).mockRejectedValue(new Error("API 500"));
    mockUseChatStream.mockReturnValue(mkStream({ echoFailed }));
    const user = userEvent.setup();
    renderChatView();

    await user.type(screen.getByPlaceholderText("Nachricht an den Agenten…"), "geht nicht");
    await user.click(screen.getByRole("button", { name: "Senden" }));

    // A bubble that outlived a failed send would claim a delivery that never
    // happened — worse than the delay it was meant to hide.
    await waitFor(() => expect(echoFailed).toHaveBeenCalledWith("geht nicht"));
  });

  it("renders a pending echo as a dimmed bubble after the real timeline", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        events: [MSG],
        pendingEchoes: [{ id: "echo-1", text: "gerade abgeschickt", sentAt: Date.now(), status: "pending" }],
      })
    );
    renderChatView();

    const bubble = screen.getByTestId("echo-bubble");
    expect(bubble).toHaveAttribute("data-echo-status", "pending");
    expect(bubble).toHaveTextContent("gerade abgeschickt");
    // After the confirmed content, because it is by definition the newest thing.
    expect(screen.getByText("Hallo!").compareDocumentPosition(bubble)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING
    );
  });

  it("says so when an echo stays unconfirmed instead of looking delivered", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        pendingEchoes: [{ id: "echo-1", text: "keine Antwort", sentAt: Date.now() - 20_000, status: "unconfirmed" }],
      })
    );
    renderChatView();

    expect(screen.getByTestId("echo-bubble")).toHaveAttribute("data-echo-status", "unconfirmed");
    expect(screen.getByText("Nicht bestätigt — Terminal prüfen")).toBeInTheDocument();
  });

  it("does not show the empty state while an echo is on screen", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        events: [],
        pendingEchoes: [{ id: "echo-1", text: "erste Nachricht", sentAt: Date.now(), status: "pending" }],
      })
    );
    renderChatView();

    expect(screen.queryByText("Noch keine Nachrichten")).not.toBeInTheDocument();
    expect(screen.getByTestId("echo-bubble")).toBeInTheDocument();
  });

  it('shows "Gesendet…" until the transcript shows a sign of the turn', () => {
    mockUseChatStream.mockReturnValue(
      mkStream({ awaitingResponse: true, state: { kind: "state", status: "idle", prompt: null } })
    );
    renderChatView();

    // Outranks the pane probe's stale "idle" — otherwise the line reads
    // "Bereit" one frame after the operator hit send.
    expect(screen.getByText("Gesendet…")).toBeInTheDocument();
    expect(screen.queryByText("Bereit")).not.toBeInTheDocument();
  });

  it("hands the server's capabilities to the composer", async () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        usage: {
          kind: "usage",
          uuid: "u9",
          ts: "2026-08-17T10:00:00Z",
          inputTokens: 10,
          outputTokens: 1,
          model: "claude-opus-5",
          effort: "high",
        },
        capabilities: { effortLevels: ["low", "high", "max"], canSwitchEffort: true },
      })
    );
    const user = userEvent.setup();
    renderChatView();

    await user.click(screen.getByTestId("effort-chip"));
    expect(screen.getAllByRole("option").map((o) => o.getAttribute("data-level"))).toEqual([
      "low",
      "high",
      "max",
    ]);
  });

  // ── Chunked first paint ───────────────────────────────────────────────────

  it("mounts the tail of a long transcript first, then the rest", async () => {
    // 120 alternating messages -> 120 items, well past the window.
    const many = Array.from({ length: 120 }, (_, i) =>
      mkMsg({ uuid: `m${i}`, text: `Nachricht ${i}`, role: i % 2 === 0 ? "assistant" : "user" })
    );
    mockUseChatStream.mockReturnValue(mkStream({ events: many }));
    renderChatView();

    // First commit: the end of the conversation is on screen, the beginning is
    // not — that is what makes the page answer immediately on a long history.
    expect(screen.getByText("Nachricht 119")).toBeInTheDocument();
    expect(screen.queryByText("Nachricht 0")).not.toBeInTheDocument();

    // One frame later the remainder joins, without the reader losing the end.
    await waitFor(() => expect(screen.getByText("Nachricht 0")).toBeInTheDocument());
    expect(screen.getByText("Nachricht 119")).toBeInTheDocument();
  });

  it("does not defer anything when the transcript is short", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
    renderChatView();
    // Below the window the slice is a no-op — no reason to make a short
    // conversation arrive in two steps.
    expect(screen.getByText("Hallo!")).toBeInTheDocument();
  });

  // ── Loading / empty states ────────────────────────────────────────────────

  it("shows a skeleton shaped like the timeline while history loads", () => {
    mockUseChatStream.mockReturnValue(mkStream({ loading: true }));
    renderChatView();
    expect(screen.getByTestId("timeline-skeleton")).toBeInTheDocument();
    expect(screen.getByText("Transkript wird geladen…")).toBeInTheDocument();
  });

  it("names both ways forward in the empty state instead of just reporting emptiness", () => {
    mockUseChatStream.mockReturnValue(mkStream({ loading: false }));
    renderChatView();
    expect(screen.getByText("Noch keine Nachrichten")).toBeInTheDocument();
    expect(screen.getByText(/Schreib unten die erste Nachricht an Cody/)).toBeInTheDocument();
    expect(screen.queryByTestId("timeline-skeleton")).not.toBeInTheDocument();
  });

  it("drops the skeleton as soon as there is anything to render", () => {
    mockUseChatStream.mockReturnValue(mkStream({ loading: true, events: [MSG] }));
    renderChatView();
    expect(screen.queryByTestId("timeline-skeleton")).not.toBeInTheDocument();
    expect(screen.getByText("Hallo!")).toBeInTheDocument();
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
