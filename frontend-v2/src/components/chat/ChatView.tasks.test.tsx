/**
 * ChatView with task sections (spec A, PR-3): tests 3/10/11/12/13/15 of the
 * spec's PR-3 plan, against the real merge pipeline, the real hooks (with a
 * QueryClient) and a mocked API. Synthetic data only.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ChatView } from "./ChatView";
import { useChatStream, type UseChatStreamResult } from "@/hooks/useChatStream";
import { api } from "@/lib/api";
import type { AgentWithState } from "./TerminalPanel";
import type { ChatTasksResponse, MessageEvent, TaskStepRow, TaskStepsResponse } from "@/lib/chatTypes";
import { at, stub, TASK_ID, TASK_ID_2 } from "./taskSteps/__tests__/fixtures";

vi.mock("@/hooks/useChatStream", () => ({ useChatStream: vi.fn() }));
vi.mock("@/hooks/useHeadTranscript", () => ({
  useHeadTranscript: vi.fn(() => ({
    events: [], subagentRuns: [], state: null, usage: null, session: null, hasMore: false,
    connected: false, loading: false, error: null, capabilities: null, pendingEchoes: [],
    echoSent: vi.fn(), echoFailed: vi.fn(), echoAgentStarting: vi.fn(), withdrawQueued: vi.fn(() => []),
    awaitingResponse: false, preview: null,
  })),
  useHeadTranscriptMeta: vi.fn(() => ({ source: null, reader: null, reason: null })),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      chat: {
        sendText: vi.fn().mockResolvedValue(undefined),
        sendKeys: vi.fn().mockResolvedValue(undefined),
        setEffort: vi.fn().mockResolvedValue(undefined),
        clearQueue: vi.fn().mockResolvedValue({ dropped: [] }),
        tasks: vi.fn(),
        taskSteps: vi.fn(),
        taskStepDetail: vi.fn(),
        engine: vi.fn(),
      },
    },
  };
});
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => true }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock("@/components/voice/VoiceWidget", () => ({ VoiceButton: () => <button type="button">mic</button> }));
// The live voice dock: only its `onForwarded` matters here (M1 for voice).
vi.mock("@/components/voice/live/VoiceLive", () => ({
  VoiceLiveDock: ({ children, onForwarded }: { children: (slot: null) => React.ReactNode; onForwarded: (t: string, m: boolean) => void }) => (
    <>
      <button type="button" data-testid="voice-forward" onClick={() => onForwarded("from voice", true)}>forward</button>
      {children(null)}
    </>
  ),
}));
vi.mock("./TerminalPanel", async () => {
  const actual = await vi.importActual<typeof import("./TerminalPanel")>("./TerminalPanel");
  return { ...actual, TerminalPanel: () => <div data-testid="terminal-panel-stub" /> };
});
/* Render-count seam (spec test 15): every chapter row computes its line 2
   once per render — counting the calls counts chapter renders. */
const seam = vi.hoisted(() => ({ summaries: 0 }));
vi.mock("./taskSteps/buildTaskUnits", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./taskSteps/buildTaskUnits")>();
  return {
    ...actual,
    chapterSummary: (...args: Parameters<typeof actual.chapterSummary>) => {
      seam.summaries += 1;
      return actual.chapterSummary(...args);
    },
  };
});

/** Every ResizeObserver callback ever created, to fire late deliveries. */
const observers: ResizeObserverCallback[] = [];
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  class CapturingRO {
    constructor(cb: ResizeObserverCallback) {
      observers.push(cb);
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  window.ResizeObserver = CapturingRO as unknown as typeof ResizeObserver;
});

const mockStream = vi.mocked(useChatStream);

function agent(over: Partial<AgentWithState> = {}): AgentWithState {
  return {
    id: "agent-a", name: "agent-a", harness: "omp", headless_chat: true, tasks_in_own_session: true, agent_runtime: "cli-bridge",
    current_task_id: null, ...over,
  } as AgentWithState;
}

function msg(ts: string, text: string, role: MessageEvent["role"] = "user"): MessageEvent {
  return { kind: "message", uuid: `m-${text}`, ts, role, text, model: null, sidechain: false };
}

function streamOf(over: Partial<UseChatStreamResult> = {}): UseChatStreamResult {
  return {
    events: [], toolOutputs: {}, elicitationResults: {}, mode: null, subagentRuns: [], state: null, usage: null,
    session: { sessionId: "s1", live: true, startedAt: at(-60) }, hasMore: false, connected: true, loading: false,
    error: null, capabilities: null, pendingEchoes: [], echoSent: vi.fn(), echoFailed: vi.fn(),
    echoAgentStarting: vi.fn(), awaitingResponse: false, preview: null, todos: null, withdrawQueued: vi.fn(() => []),
    ...over,
  };
}

let rowN = 0;
const think = (ts: string, lead: string): TaskStepRow => ({ id: `1:${++rowN}`, ts, k: "think", lead });
const tool = (ts: string, title: string): TaskStepRow => ({ id: `1:${++rowN}`, ts, k: "tool", name: "bash", title, st: "done" });

function steps(rows: TaskStepRow[], over: Partial<Extract<TaskStepsResponse, { applies: true }>> = {}): TaskStepsResponse {
  return {
    applies: true, taskId: TASK_ID, status: "in_progress", live: true, hasSession: true,
    attempts: [{ n: 1, startedAt: at(0) }], rows, updated: [], cursor: `v1.${rowN}`, hasMore: false,
    reset: false, omitted: 0, liveRow: null, ...over,
  };
}

function serve(tasks: ChatTasksResponse, stepsByTask: Record<string, TaskStepsResponse>) {
  vi.mocked(api.chat.tasks).mockResolvedValue({ data: tasks, etag: '"t1"' });
  vi.mocked(api.chat.taskSteps).mockImplementation(async (_a, taskId) => ({ data: stepsByTask[taskId], etag: `"s-${taskId}"` }));
  vi.mocked(api.chat.taskStepDetail).mockResolvedValue({ data: { id: "1:1", ts: at(1), k: "think", text: "full" }, etag: '"d"' });
  vi.mocked(api.chat.engine).mockResolvedValue({ engine: null });
}

function renderView(over: Partial<React.ComponentProps<typeof ChatView>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const props = {
    agent: agent(), hasTranscript: true, detailLevel: "normal" as const, onDetailLevelChange: () => {},
    centerView: "chat" as const, onCenterViewChange: () => {}, ...over,
  };
  const utils = render(
    <QueryClientProvider client={qc}>
      <ChatView {...props} />
    </QueryClientProvider>,
  );
  return {
    ...utils,
    qc,
    rerenderView: (o: Partial<typeof props> = {}) =>
      utils.rerender(
        <QueryClientProvider client={qc}>
          <ChatView {...props} {...o} />
        </QueryClientProvider>,
      ),
  };
}

/** The scroll container with a measurable layout (jsdom has none). */
function scroller(height = 3000, client = 600) {
  const el = screen.getByTestId("chat-timeline").parentElement as HTMLElement;
  let h = height;
  Object.defineProperty(el, "clientHeight", { value: client, configurable: true });
  Object.defineProperty(el, "scrollHeight", { get: () => h, configurable: true });
  el.getBoundingClientRect = () => ({ top: 0, bottom: client, left: 0, right: 390, width: 390, height: client, x: 0, y: 0, toJSON() {} }) as DOMRect;
  return { el, setHeight: (n: number) => (h = n) };
}

/** Scroll up by a real gesture (wheel arms it) — following ends. */
function scrollUp(el: HTMLElement, to: number) {
  fireEvent.wheel(el, { deltaY: -400 });
  el.scrollTop = to;
  fireEvent.scroll(el);
}

beforeEach(() => {
  vi.clearAllMocks();
  observers.length = 0;
  seam.summaries = 0;
  rowN = 0;
});

describe("ChatView — task sections (spec A)", () => {
  it("S6: asks for the task stubs while the history is still loading", async () => {
    serve({ applies: true, tasks: [], queued: 0, hasMore: false }, {});
    mockStream.mockReturnValue(streamOf({ loading: true }));
    renderView();
    await waitFor(() => expect(api.chat.tasks).toHaveBeenCalledWith("agent-a", expect.objectContaining({ limit: 20 })));
  });

  it("N6: another harness never asks and renders as before", async () => {
    mockStream.mockReturnValue(streamOf({ events: [msg(at(1), "hello", "assistant")] }));
    renderView({ agent: agent({ harness: "claude", headless_chat: false, tasks_in_own_session: false }) });
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(api.chat.tasks).not.toHaveBeenCalled();
  });

  it("N6: the gate is the backend's flag, not the harness name (omp under the native driver never asks)", async () => {
    mockStream.mockReturnValue(streamOf({ events: [msg(at(1), "hello", "assistant")] }));
    renderView({ agent: agent({ harness: "omp", headless_chat: false, tasks_in_own_session: false }) });
    expect(screen.getByText("hello")).toBeInTheDocument();
    await act(async () => {});
    expect(api.chat.tasks).not.toHaveBeenCalled();
  });

  it("§2.2/MUST-2: a chat-state flip asks for the stubs at once — no wait for the 60 s idle poll", async () => {
    serve({ applies: true, tasks: [], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "Delivered work")]) });
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")], state: { kind: "state", status: "idle", prompt: null } }));
    const { rerenderView } = renderView();
    await screen.findByText("go");
    await waitFor(() => expect(api.chat.tasks).toHaveBeenCalledTimes(1));
    // The task is delivered: the server now lists it, the SSE says so.
    vi.mocked(api.chat.tasks).mockResolvedValue({
      data: { applies: true, tasks: [stub()], queued: 0, hasMore: false },
      etag: '"t2"',
    });
    mockStream.mockReturnValue(
      streamOf({ events: [msg(at(0), "go")], state: { kind: "state", status: "task_working", prompt: null } }),
    );
    rerenderView();
    expect(await screen.findByTestId("chat-task-segment")).toBeInTheDocument();
    expect(api.chat.tasks).toHaveBeenCalledTimes(2);
    // A flip between two states outside the set (working ↔ waiting_input) asks nothing.
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")], state: { kind: "state", status: "working", prompt: null } }));
    rerenderView();
    await waitFor(() => expect(api.chat.tasks).toHaveBeenCalledTimes(3)); // task_working → working: a crossing
    mockStream.mockReturnValue(
      streamOf({ events: [msg(at(0), "go")], state: { kind: "state", status: "waiting_input", prompt: null } }),
    );
    rerenderView();
    await act(async () => {});
    expect(api.chat.tasks).toHaveBeenCalledTimes(3);
  });

  it("1/D2: task rows stand at their time, the operator's message between two segments", async () => {
    serve(
      { applies: true, tasks: [stub()], queued: 0, hasMore: false },
      { [TASK_ID]: steps([think(at(1), "Read the files first"), think(at(8), "Now build the page")]) },
    );
    mockStream.mockReturnValue(streamOf({ events: [msg(at(5), "How far are you?")] }));
    renderView();
    const segments = await screen.findAllByTestId("chat-task-segment");
    expect(segments).toHaveLength(2);
    const question = screen.getByText("How far are you?");
    expect(segments[0].compareDocumentPosition(question)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(question.compareDocumentPosition(segments[1])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(within(segments[1]).getByTestId("chat-task-head")).toHaveTextContent("Build the report page · continues");
  });

  it("11: the empty state is hidden when only a section exists; the old block and row never show", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "Start")]) });
    mockStream.mockReturnValue(streamOf());
    renderView({ workingOn: { taskId: TASK_ID, title: "Build the report page" } });
    await screen.findByTestId("chat-task-segment");
    expect(screen.queryByText("No messages yet")).not.toBeInTheDocument();
    expect(screen.queryByTestId("chat-working-on")).not.toBeInTheDocument();
    expect(screen.queryByTestId("chat-task-activity")).not.toBeInTheDocument();
  });

  it("11: WorkingOnRow stays for an agent whose tasks run inside the chat (applies: false)", async () => {
    serve({ applies: false }, {});
    mockStream.mockReturnValue(streamOf());
    renderView({ workingOn: { taskId: TASK_ID, title: "Build the report page" } });
    expect(await screen.findByTestId("chat-working-on")).toBeInTheDocument();
  });

  it("13: the composer says where a message goes while a task runs", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "Start")]) });
    mockStream.mockReturnValue(streamOf());
    renderView();
    expect(await screen.findByPlaceholderText("Goes to the chat, not the task…")).toBeInTheDocument();
  });

  it("10a/M1: sending from two screens up jumps to the end before the echo", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "Start")]) });
    const echoSent = vi.fn();
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")], echoSent }));
    const user = userEvent.setup();
    renderView();
    await screen.findByTestId("chat-task-segment");
    const { el } = scroller(3000, 600);
    scrollUp(el, 1200);
    expect(screen.getByRole("button", { name: /Jump to bottom/ })).toBeInTheDocument();
    let topAtEcho = -1;
    echoSent.mockImplementation(() => (topAtEcho = el.scrollTop));
    await user.type(screen.getByRole("textbox"), "Status?");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(topAtEcho).toBe(3000);
    expect(screen.queryByRole("button", { name: /Jump to bottom/ })).not.toBeInTheDocument();
  });

  it("10a/M1: a voice-forwarded text jumps to the end too", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "Start")]) });
    const echoSent = vi.fn();
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")], echoSent }));
    renderView();
    await screen.findByTestId("chat-task-segment");
    const { el } = scroller(3000, 600);
    scrollUp(el, 1200);
    let topAtEcho = -1;
    echoSent.mockImplementation(() => (topAtEcho = el.scrollTop));
    fireEvent.click(screen.getByTestId("voice-forward"));
    expect(topAtEcho).toBe(3000);
  });

  it("10b/M3: opening the last card while following keeps its head in place, also against a late resize callback", async () => {
    const done = stub({ status: "done", live: false, endedAt: at(30), outcome: "Report ready", deliverables: 3 });
    serve(
      { applies: true, tasks: [done], queued: 0, hasMore: false },
      { [TASK_ID]: steps([think(at(1), "A"), think(at(2), "B")], { live: false, status: "done" }) },
    );
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")] }));
    renderView();
    const card = await screen.findByTestId("chat-task-card");
    const { el, setHeight } = scroller(3000, 600);
    el.scrollTop = 2400; // following, at the end
    const head = card.firstElementChild as HTMLElement;
    head.getBoundingClientRect = () => ({ top: 2500 - el.scrollTop } as DOMRect);
    const before = head.getBoundingClientRect().top;
    setHeight(5000); // the opened card grows the content
    await act(async () => {
      fireEvent.click(within(card).getByRole("button", { name: "Show steps" }));
    });
    // A resize callback created while following arrives late (the browser
    // delivers it after layout) — it must not pin to the end any more.
    act(() => observers.forEach((cb) => cb([], {} as ResizeObserver)));
    expect(head.getBoundingClientRect().top).toBe(before);
    expect(screen.getByRole("button", { name: /Jump to bottom/ })).toBeInTheDocument();
    expect(await within(card).findAllByTestId("task-unit")).not.toHaveLength(0);
  });

  it("10d/M3: closing a stuck card head brings it back where it was on screen", async () => {
    const done = stub({ status: "done", live: false, endedAt: at(30) });
    serve(
      { applies: true, tasks: [done], queued: 0, hasMore: false },
      { [TASK_ID]: steps([think(at(1), "A"), think(at(2), "B")], { live: false, status: "done" }) },
    );
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")] }));
    renderView();
    const card = await screen.findByTestId("chat-task-card");
    const { el } = scroller(6000, 600);
    await act(async () => {
      fireEvent.click(within(card).getByRole("button", { name: "Show steps" }));
    });
    // Read far down inside the open card: its head sticks at the top (0).
    el.scrollTop = 3000;
    const head = card.firstElementChild as HTMLElement;
    let open = true;
    head.getBoundingClientRect = () => ({ top: open ? 0 : 1000 - el.scrollTop } as DOMRect);
    await act(async () => {
      fireEvent.click(within(card).getByRole("button", { name: "Hide steps" }));
      open = false;
    });
    expect(head.getBoundingClientRect().top).toBe(0);
    expect(el.scrollTop).toBe(1000);
  });

  it("10c/M3: while not following, a new chapter does not close the open one (no height change above)", async () => {
    const first = think(at(1), "Read the files first");
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([first]) });
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")] }));
    const { qc } = renderView();
    await screen.findByTestId("chat-task-segment");
    await waitFor(() => expect(screen.getByText("Read the files first").closest("[data-unit]")).toHaveAttribute("data-open", "true"));
    const { el } = scroller(3000, 600);
    scrollUp(el, 1200);
    const second = think(at(3), "Now build the page");
    vi.mocked(api.chat.taskSteps).mockResolvedValue({ data: steps([second], { cursor: "v1.9" }), etag: '"s2"' });
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["chat-task-steps", "agent-a", TASK_ID] });
    });
    await screen.findByText("Now build the page");
    expect(screen.getByText("Read the files first").closest("[data-unit]")).toHaveAttribute("data-open", "true");
    expect(screen.getByText("Now build the page").closest("[data-unit]")).toHaveAttribute("data-open", "false");
  });

  it("10e/S7: the counter counts newer units, not a length difference", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "A")]) });
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")] }));
    const { qc } = renderView();
    await screen.findByTestId("chat-task-segment");
    const { el } = scroller(3000, 600);
    scrollUp(el, 1200);
    // A late page of OLDER rows (reset) adds nothing new.
    vi.mocked(api.chat.taskSteps).mockResolvedValueOnce({
      data: steps([think(at(-5), "older 1"), think(at(-4), "older 2"), think(at(1), "A")], { reset: true }),
      etag: '"s2"',
    });
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["chat-task-steps", "agent-a", TASK_ID] });
    });
    await screen.findByText("older 1");
    expect(screen.getByRole("button", { name: /Jump to bottom/ })).not.toHaveTextContent(/new/);
    vi.mocked(api.chat.taskSteps).mockResolvedValueOnce({
      data: steps([think(at(5), "N1"), think(at(6), "N2"), think(at(7), "N3")], { cursor: "v1.20" }),
      etag: '"s3"',
    });
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["chat-task-steps", "agent-a", TASK_ID] });
    });
    await screen.findByText("N3");
    expect(screen.getByRole("button", { name: /Jump to bottom/ })).toHaveTextContent("3 new");
  });

  it("15: a poll with one new chapter renders that chapter only (≤ 3 chapter renders)", async () => {
    const rows = [think(at(1), "A"), think(at(2), "B"), think(at(3), "C"), think(at(4), "D"), think(at(5), "E")];
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps(rows) });
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")] }));
    const { qc } = renderView();
    await screen.findByText("E");
    await act(async () => {});
    const before = seam.summaries;
    vi.mocked(api.chat.taskSteps).mockResolvedValueOnce({ data: steps([think(at(6), "F")], { cursor: "v1.30" }), etag: '"s4"' });
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["chat-task-steps", "agent-a", TASK_ID] });
    });
    await screen.findByText("F");
    expect(seam.summaries - before).toBeLessThanOrEqual(3);
  });

  it("M4: a finished card that goes live again stays a card; its new work shows below", async () => {
    const done = stub({ status: "review", live: false, endedAt: at(30) });
    serve({ applies: true, tasks: [done], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "Old")], { live: false, status: "review" }) });
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go"), msg(at(40), "please rework")] }));
    const { qc } = renderView();
    await screen.findByTestId("chat-task-card");
    const relive = stub({ status: "in_progress", live: true, endedAt: at(30) });
    vi.mocked(api.chat.tasks).mockResolvedValue({ data: { applies: true, tasks: [relive], queued: 0, hasMore: false }, etag: '"t2"' });
    vi.mocked(api.chat.taskSteps).mockResolvedValue({ data: steps([think(at(1), "Old"), think(at(50), "Rework")]), etag: '"s9"' });
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["chat-tasks", "agent-a"] });
    });
    await screen.findByText("Rework");
    expect(screen.getByTestId("chat-task-card")).toBeInTheDocument();
    const rework = screen.getByText("Rework");
    expect(screen.getByText("please rework").compareDocumentPosition(rework)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it("5/D6: a task that finishes while the chat is open does not collapse — it ends with the end line", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "Working")]) });
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")] }));
    const { qc } = renderView();
    await screen.findByText("Working");
    const done = stub({ status: "done", live: false, endedAt: at(20), outcome: "Report ready", deliverables: 2 });
    vi.mocked(api.chat.tasks).mockResolvedValue({ data: { applies: true, tasks: [done], queued: 0, hasMore: false }, etag: '"t9"' });
    vi.mocked(api.chat.taskSteps).mockResolvedValue({ data: steps([], { live: false, status: "done" }), etag: '"s-end"' });
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["chat-tasks", "agent-a"] });
    });
    await screen.findByText(/Report ready/);
    expect(screen.queryByTestId("chat-task-card")).not.toBeInTheDocument();
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.getByText(/2 files/)).toBeInTheDocument();
  });

  it("live 22.10.: a task that finished before the operator's message ends above it — no 'continues' band below", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "Run the bench")]) });
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go"), msg(at(3), "Looks good?")] }));
    const { qc } = renderView();
    await screen.findByText("Run the bench");
    // The task had ended (review) at 1:22, before the message at 3:00.
    const review = stub({ status: "review", live: false, endedAt: at(1, 22) });
    vi.mocked(api.chat.tasks).mockResolvedValue({ data: { applies: true, tasks: [review], queued: 0, hasMore: false }, etag: '"t-r"' });
    vi.mocked(api.chat.taskSteps).mockResolvedValue({ data: steps([], { live: false, status: "review" }), etag: '"s-r"' });
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["chat-tasks", "agent-a"] });
    });
    await waitFor(() => expect(screen.getAllByTestId("task-unit").some((u) => u.dataset.unit === "end")).toBe(true));
    const segments = screen.getAllByTestId("chat-task-segment");
    expect(segments).toHaveLength(1);
    const end = within(segments[0]).getAllByTestId("task-unit").find((u) => u.dataset.unit === "end");
    expect(end).toHaveTextContent("Task awaits review · took 1 min");
    expect(end!.compareDocumentPosition(screen.getByText("Looks good?"))).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.queryByText(/· continues/)).not.toBeInTheDocument();
  });

  it("an end line that arrives after an interleaved message: title-only head below it, never 'continues'", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "Run the bench")]) });
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go"), msg(at(3), "Looks good?")] }));
    const { qc } = renderView();
    await screen.findByText("Run the bench");
    // Running at the message (the live row trails below it as "continues") …
    let segments = await screen.findAllByTestId("chat-task-segment");
    expect(within(segments[1]).getByTestId("chat-task-head")).toHaveTextContent("Build the report page · continues");
    // … then ends after it, with no new steps: the band below holds only the end line.
    const review = stub({ status: "review", live: false, endedAt: at(4) });
    vi.mocked(api.chat.tasks).mockResolvedValue({ data: { applies: true, tasks: [review], queued: 0, hasMore: false }, etag: '"t-r2"' });
    vi.mocked(api.chat.taskSteps).mockResolvedValue({ data: steps([], { live: false, status: "review" }), etag: '"s-r2"' });
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["chat-tasks", "agent-a"] });
    });
    await waitFor(() => expect(screen.getAllByTestId("task-unit").some((u) => u.dataset.unit === "end")).toBe(true));
    segments = screen.getAllByTestId("chat-task-segment");
    expect(segments).toHaveLength(2);
    const head = within(segments[1]).getByTestId("chat-task-head");
    expect(head).toHaveTextContent("Build the report page");
    expect(head).not.toHaveTextContent("continues");
    expect(head).toHaveTextContent("Review");
    expect(screen.getByText("Looks good?").compareDocumentPosition(segments[1])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    // The state is said once — in the end line, not again under the head (K3).
    expect(within(segments[1]).getAllByTestId("chat-task-state")).toHaveLength(1);
  });

  it("a blocked task below the operator's message: its head links 'Answer', not 'continues', and says the state once", async () => {
    serve(
      { applies: true, tasks: [stub({ status: "blocked" })], queued: 0, hasMore: false },
      { [TASK_ID]: steps([think(at(1), "Need a decision")], { status: "blocked" }) },
    );
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go"), msg(at(3), "What do you need?")] }));
    renderView();
    const segments = await screen.findAllByTestId("chat-task-segment");
    expect(segments).toHaveLength(2);
    const head = within(segments[1]).getByTestId("chat-task-head");
    expect(head).not.toHaveTextContent("continues");
    expect(head).toHaveTextContent("Answer");
    // The state line stands in the first band only (K3) — not again below.
    expect(within(segments[1]).queryByTestId("chat-task-state")).not.toBeInTheDocument();
    expect(within(segments[0]).getByTestId("chat-task-state")).toHaveTextContent("Task waits for you");
    expect(screen.getAllByText(/Task waits for you/)).toHaveLength(1);
  });

  it("M4: a stub that vanishes keeps its rows and says the task is no longer active here", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "Kept")]) });
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")] }));
    const { qc } = renderView();
    await screen.findByText("Kept");
    vi.mocked(api.chat.tasks).mockResolvedValue({ data: { applies: true, tasks: [], queued: 0, hasMore: false }, etag: '"t3"' });
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["chat-tasks", "agent-a"] });
    });
    expect(await screen.findByText("Task no longer active here")).toBeInTheDocument();
    expect(screen.getByText("Kept")).toBeInTheDocument();
  });

  it("D8: a finished task older than the chat's loaded window stays out", async () => {
    const old = stub({ taskId: TASK_ID_2, title: "Old task", status: "done", live: false, startKey: at(-120), endedAt: at(-100) });
    serve({ applies: true, tasks: [old], queued: 0, hasMore: false }, {});
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")] }));
    renderView();
    await screen.findByText("go");
    await act(async () => {});
    expect(screen.queryByText("Old task")).not.toBeInTheDocument();
  });

  it("SHOULD-1: an opened chapter shows its whole lead — no clamp while open", async () => {
    const long = "A".repeat(60) + " " + "B".repeat(60) + " end of the lead";
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), long)]) });
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")] }));
    renderView();
    const lead = await screen.findByText(long);
    await waitFor(() => expect(lead.closest("[data-unit]")).toHaveAttribute("data-open", "true"));
    expect(lead.className).not.toContain("line-clamp-2");
    fireEvent.click(lead.closest("button") as HTMLElement);
    expect(lead.closest("[data-unit]")).toHaveAttribute("data-open", "false");
    expect(lead.className).toContain("line-clamp-2");
  });

  it("SHOULD-2: a tool whose detail fetch fails says so and loads again on 'Load again'", async () => {
    serve(
      { applies: true, tasks: [stub()], queued: 0, hasMore: false },
      { [TASK_ID]: steps([think(at(1), "Run it"), tool(at(2), "npm test")]) },
    );
    vi.mocked(api.chat.taskStepDetail).mockRejectedValueOnce(new Error("502"));
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")] }));
    renderView();
    const title = await screen.findByText("npm test", { selector: "button *" });
    fireEvent.click(title.closest("button") as HTMLElement);
    expect(await screen.findByText("This step could not be loaded.")).toBeInTheDocument();
    expect(api.chat.taskStepDetail).toHaveBeenCalledTimes(1);
    vi.mocked(api.chat.taskStepDetail).mockResolvedValueOnce({
      data: { id: "1:2", ts: at(2), k: "tool", detail: { command: "npm test" }, result: "all green" },
      etag: '"d2"',
    });
    fireEvent.click(screen.getByRole("button", { name: "Load again" }));
    expect(await screen.findByText(/all green/)).toBeInTheDocument();
    expect(api.chat.taskStepDetail).toHaveBeenCalledTimes(2);
  });

  it("SHOULD-2: a live section whose steps fail to load says so under its head and retries", async () => {
    vi.mocked(api.chat.tasks).mockResolvedValue({ data: { applies: true, tasks: [stub()], queued: 0, hasMore: false }, etag: '"t1"' });
    vi.mocked(api.chat.taskSteps).mockRejectedValueOnce(new Error("502"));
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")] }));
    renderView();
    const failed = await screen.findByText("Steps could not be loaded.");
    expect(failed.closest("[data-testid=chat-task-segment]")).not.toBeNull();
    vi.mocked(api.chat.taskSteps).mockResolvedValueOnce({ data: steps([think(at(1), "Back again")]), etag: '"s1"' });
    fireEvent.click(screen.getByRole("button", { name: "Load again" }));
    expect(await screen.findByText("Back again")).toBeInTheDocument();
    expect(screen.queryByText("Steps could not be loaded.")).not.toBeInTheDocument();
  });

  it("SHOULD-3/M3: opening a chapter while scrolled up keeps the tapped row where it was", async () => {
    serve(
      { applies: true, tasks: [stub()], queued: 0, hasMore: false },
      { [TASK_ID]: steps([think(at(1), "Read the files first"), think(at(3), "Now build the page")]) },
    );
    mockStream.mockReturnValue(streamOf({ events: [msg(at(0), "go")] }));
    renderView();
    await screen.findByText("Now build the page");
    const { el } = scroller(3000, 600);
    scrollUp(el, 1200);
    const button = screen.getByText("Read the files first").closest("button") as HTMLElement;
    expect(button).toHaveAttribute("aria-expanded", "false");
    // Layout stand-in: once the chapter is open, the row sits 200 px lower
    // at the same scroll position (content above it grew).
    button.getBoundingClientRect = () =>
      ({ top: 300 + (button.getAttribute("aria-expanded") === "true" ? 200 : 0) - (el.scrollTop - 1200) }) as DOMRect;
    const before = button.getBoundingClientRect().top;
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(button.getBoundingClientRect().top).toBe(before);
    expect(el.scrollTop).toBe(1400);
  });

  it("SHOULD-3/10e: an echo sent after scrolling up counts as new on 'Jump to bottom'", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "A")]) });
    const events = [msg(at(0), "go")];
    mockStream.mockReturnValue(streamOf({ events }));
    const { rerenderView } = renderView();
    await screen.findByTestId("chat-task-segment");
    const { el } = scroller(3000, 600);
    scrollUp(el, 1200);
    expect(screen.getByRole("button", { name: /Jump to bottom/ })).not.toHaveTextContent(/new/);
    mockStream.mockReturnValue(
      streamOf({ events, pendingEchoes: [{ id: "e1", text: "from the phone", sentAt: Date.now() + 1000, status: "pending" }] }),
    );
    rerenderView();
    expect(screen.getByRole("button", { name: /Jump to bottom/ })).toHaveTextContent("1 new");
  });

  it("SHOULD-3/M1: a new echo while following is pinned to the end without a resize callback", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "A")]) });
    const events = [msg(at(0), "go")];
    const toolOutputs = {};
    mockStream.mockReturnValue(streamOf({ events, toolOutputs }));
    const { rerenderView } = renderView();
    await screen.findByTestId("chat-task-segment");
    await act(async () => {});
    const { el, setHeight } = scroller(3000, 600);
    el.scrollTop = 2400; // following, at the end
    fireEvent.scroll(el);
    setHeight(3300); // the echo bubble's height
    mockStream.mockReturnValue(
      streamOf({ events, toolOutputs, pendingEchoes: [{ id: "e1", text: "next", sentAt: Date.now(), status: "pending" }] }),
    );
    rerenderView();
    // The capturing ResizeObserver never fires here: only the pin effect can move it.
    expect(el.scrollTop).toBe(3300);
  });

  it("12: the status line names the live task when its head is not on screen", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "Start")]) });
    mockStream.mockReturnValue(streamOf({ state: { kind: "state", status: "task_working", prompt: null } }));
    renderView();
    await screen.findByTestId("chat-task-segment");
    // jsdom has no IntersectionObserver → no head counts as visible.
    expect(screen.getByTestId("status-label")).toHaveTextContent("Task in progress · Build the report page");
  });
});

// Round-3 user test t1 NICE-1/2/3 (R5 "honest states"): the chat's task band
// and status line lagged behind what the steps already showed.
describe("ChatView — task state from the freshest source (round 3)", () => {
  const delivered = (over = {}) => stub({ status: "inbox", live: true, startedAt: null, startKey: at(0), ...over });

  it("NICE-2: steps already say 'in progress' → the band does not say 'not started'", async () => {
    // The stub poll (10 s) still has inbox; the steps poll (2 s) is newer.
    serve(
      { applies: true, tasks: [delivered()], queued: 0, hasMore: false },
      { [TASK_ID]: steps([think(at(1), "ACK first"), tool(at(1, 10), "mc ack")], { status: "in_progress" }) },
    );
    mockStream.mockReturnValue(streamOf({ state: { kind: "state", status: "task_working", prompt: null } }));
    renderView();
    await screen.findByText("ACK first");
    const line = screen.getByTestId("chat-task-state");
    await waitFor(() => expect(line).toHaveTextContent("Task in progress"));
    expect(line).not.toHaveTextContent(/not started|waiting for the model/);
  });

  it("NICE-2: delivered and the agent already writing → 'in progress' even before the status flips", async () => {
    serve(
      { applies: true, tasks: [delivered()], queued: 0, hasMore: false },
      { [TASK_ID]: steps([think(at(1), "ACK first")], { status: "inbox" }) },
    );
    mockStream.mockReturnValue(streamOf({ state: { kind: "state", status: "task_working", prompt: null } }));
    renderView();
    await screen.findByText("ACK first");
    await waitFor(() => expect(screen.getByTestId("chat-task-state")).toHaveTextContent("Task in progress"));
  });

  it("NICE-2 counter-case: delivered, nothing written yet → still 'not started'", async () => {
    serve({ applies: true, tasks: [delivered()], queued: 0, hasMore: false }, { [TASK_ID]: steps([], { status: "inbox" }) });
    mockStream.mockReturnValue(streamOf({ state: { kind: "state", status: "waiting_model", prompt: null } }));
    renderView();
    const line = await screen.findByTestId("chat-task-state");
    await waitFor(() => expect(api.chat.taskSteps).toHaveBeenCalled());
    expect(line).toHaveTextContent("Task not started");
  });

  it("NICE-3: the steps say the task went to review → band and status line end at once", async () => {
    serve(
      { applies: true, tasks: [stub()], queued: 0, hasMore: false },
      { [TASK_ID]: steps([think(at(1), "Done, handing over")], { status: "review", live: false }) },
    );
    // The chat probe still reports the task turn (it lags up to ~15 s).
    mockStream.mockReturnValue(streamOf({ state: { kind: "state", status: "task_working", prompt: null } }));
    renderView();
    await screen.findByText("Done, handing over");
    await waitFor(() => expect(screen.getByTestId("chat-task-head")).toHaveTextContent("Review"));
    expect(screen.queryByText("Working on a task")).toBeNull();
    expect(screen.queryByText(/Task in progress · Build the report page/)).toBeNull();
    expect(screen.getByText("Ready")).toBeInTheDocument();
  });

  it("NICE-1: the new band under the operator's message says 'Thinking' at once, not empty", async () => {
    // Now-relative: a band quiet for minutes says "no new steps" instead.
    const ago = (s: number) => new Date(Date.now() - s * 1000).toISOString();
    serve(
      { applies: true, tasks: [stub({ startedAt: ago(120), startKey: ago(120), deliveredAt: ago(125) })], queued: 0, hasMore: false },
      { [TASK_ID]: steps([think(ago(60), "Build it")]) },
    );
    mockStream.mockReturnValue(
      streamOf({
        events: [msg(ago(5), "How far?")],
        state: { kind: "state", status: "task_working", prompt: null },
      }),
    );
    renderView();
    await screen.findByText("Build it");
    const segs = await screen.findAllByTestId("chat-task-segment");
    const last = segs[segs.length - 1];
    expect(within(last).getByText(/Thinking/)).toBeInTheDocument();
  });
});

describe("ChatView — a stalled task whose engine still works (31.10.2026)", () => {
  const ago = (sec: number) => new Date(Date.now() - sec * 1000).toISOString();
  function stalledTask() {
    serve(
      { applies: true, tasks: [stub({ startedAt: ago(1500), startKey: ago(1500), deliveredAt: ago(1505) })], queued: 0, hasMore: false },
      { [TASK_ID]: steps([think(ago(1400), "Write the bench file")]) },
    );
    mockStream.mockReturnValue(streamOf({ state: { kind: "state", status: "task_working", prompt: null } }));
  }

  it("engine busy: 'no new steps' says the model is still computing", async () => {
    stalledTask();
    vi.mocked(api.chat.engine).mockResolvedValue({ engine: { busy: true, running: 1, checkedAt: ago(0) } });
    renderView();
    await screen.findByText("Write the bench file");
    await waitFor(() => expect(screen.getByText(/No new steps for/)).toHaveTextContent("model computing, no output yet"));
    expect(api.chat.engine).toHaveBeenCalledWith("agent-a");
  });

  it("engine idle or unknown: only 'no new steps'", async () => {
    stalledTask();
    renderView();
    await screen.findByText("Write the bench file");
    await waitFor(() => expect(api.chat.engine).toHaveBeenCalled());
    expect(screen.getByText(/No new steps for/)).not.toHaveTextContent("model computing");
  });
});
