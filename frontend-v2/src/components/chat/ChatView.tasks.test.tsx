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
vi.mock("@/hooks/useTaskActivity", () => ({ useTaskActivity: vi.fn(() => null) }));
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

  it("12: the status line names the live task when its head is not on screen", async () => {
    serve({ applies: true, tasks: [stub()], queued: 0, hasMore: false }, { [TASK_ID]: steps([think(at(1), "Start")]) });
    mockStream.mockReturnValue(streamOf({ state: { kind: "state", status: "task_working", prompt: null } }));
    renderView();
    await screen.findByTestId("chat-task-segment");
    // jsdom has no IntersectionObserver → no head counts as visible.
    expect(screen.getByTestId("status-label")).toHaveTextContent("Task in progress · Build the report page");
  });
});
