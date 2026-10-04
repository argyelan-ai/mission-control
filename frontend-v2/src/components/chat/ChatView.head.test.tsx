/**
 * ChatView — the `head` branch (bauplan `heads-sichtbar` PR 2 §3.2): a
 * read-only chat view fed by `useHeadTranscript` instead of
 * `useChatStream`, with its own small header and footer, and none of the
 * agent-only chrome (composer, StatusLine, Chat/Terminal toggle, live/
 * ended badge, options sheet, microphone).
 *
 * `useChatStream` is mocked to a quiet no-op (it still runs — rules of
 * hooks — with `agentId: null`, so asserting it was called with `null`
 * doubles as the regression guard for "an agent's stream must not keep
 * running once a head is selected"). `useHeadTranscript` is mocked the
 * other way: it is what feeds the timeline.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ChatView } from "./ChatView";
import { useChatStream } from "@/hooks/useChatStream";
import { useHeadTranscript, useHeadTranscriptMeta } from "@/hooks/useHeadTranscript";
import { api } from "@/lib/api";
import { mkRun } from "@/lib/__tests__/headFixtures";
import type { MessageEvent } from "@/lib/chatTypes";
import type { UseChatStreamResult } from "@/hooks/useChatStream";

vi.mock("@/hooks/useChatStream", () => ({ useChatStream: vi.fn() }));
vi.mock("@/hooks/useHeadTranscript", () => ({ useHeadTranscript: vi.fn(), useHeadTranscriptMeta: vi.fn() }));
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => true }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock("@/components/voice/VoiceWidget", () => ({ VoiceButton: () => <button type="button">mic</button> }));
vi.mock("./TerminalPanel", async () => {
  const actual = await vi.importActual<typeof import("./TerminalPanel")>("./TerminalPanel");
  return { ...actual, TerminalPanel: () => <div data-testid="terminal-panel-stub" /> };
});
vi.mock("@/lib/api", () => ({
  api: {
    heads: {
      summary: vi.fn().mockRejectedValue(new Error("API 404: {}")),
      runRecord: vi.fn(),
      restart: vi.fn(),
      stop: vi.fn(),
      log: vi.fn().mockResolvedValue(""),
    },
  },
}));

const mockUseChatStream = vi.mocked(useChatStream);
const mockUseHeadTranscript = vi.mocked(useHeadTranscript);
const mockUseHeadTranscriptMeta = vi.mocked(useHeadTranscriptMeta);

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  class MockResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
});

function mkHeadStream(overrides: Partial<UseChatStreamResult> = {}): UseChatStreamResult {
  return {
    events: [],
    subagentRuns: [],
    state: null,
    usage: null,
    session: { sessionId: "s1", live: true, startedAt: "2026-10-04T09:00:00Z" },
    hasMore: false,
    connected: true,
    loading: false,
    error: null,
    capabilities: null,
    pendingEchoes: [],
    echoSent: vi.fn(),
    echoFailed: vi.fn(),
    echoAgentStarting: vi.fn(),
    withdrawQueued: vi.fn(() => []),
    awaitingResponse: false,
    preview: null,
    ...overrides,
  };
}

const MSG: MessageEvent = {
  kind: "message",
  uuid: "m1",
  ts: "2026-10-04T09:05:00Z",
  role: "assistant",
  text: "Working on it.",
  model: null,
  sidechain: false,
};

function renderHead(run = mkRun({ state: "running" })) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ChatView
        agent={null}
        head={run}
        hasTranscript={true}
        detailLevel="normal"
        onDetailLevelChange={vi.fn()}
        centerView="chat"
        onCenterViewChange={vi.fn()}
        onBack={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseChatStream.mockReturnValue(mkHeadStream({ events: [], session: null }));
  mockUseHeadTranscript.mockReturnValue(mkHeadStream({ events: [MSG] }));
  mockUseHeadTranscriptMeta.mockReturnValue({ source: "transcript", reader: "omp", reason: null });
});

describe("ChatView — head branch", () => {
  it("renders agent=null + head without crashing (the whole point of the agent?. guards)", () => {
    renderHead();
    expect(screen.getByTestId("chat-header")).toBeInTheDocument();
  });

  it("the agent stream hook is called with a null id — it must not keep an agent subscription alive", () => {
    renderHead();
    expect(mockUseChatStream).toHaveBeenCalledWith(null, false);
  });

  it("feeds the timeline from useHeadTranscript, not useChatStream", () => {
    renderHead();
    expect(screen.getByText("Working on it.")).toBeInTheDocument();
  });

  it("shows the head header (title + context line), not the agent header name/badge", () => {
    renderHead(mkRun({ title: "Fix flaky retry test", state: "running", harness: "omp", model: "GLM-5.3-Flash-EXL3" }));
    const header = screen.getByTestId("chat-header");
    expect(header).toHaveAttribute("data-kind", "head");
    expect(header).toHaveTextContent("Fix flaky retry test");
    expect(header).toHaveTextContent("omp × GLM-5.3");
    expect(screen.queryByTestId("session-badge")).not.toBeInTheDocument();
  });

  it("shows HeadChatFooter instead of StatusLine/Composer", () => {
    renderHead(mkRun({ state: "running", step: "4/7 sabotage probe · waiting for: nothing" }));
    expect(screen.getByTestId("head-footer-stop")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByTestId("admin-only-notice")).not.toBeInTheDocument();
  });

  it("has no Chat/Terminal toggle and no options ('…') button", () => {
    renderHead();
    expect(screen.queryByText("Terminal")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Chat options")).not.toBeInTheDocument();
  });

  it("has no microphone button — nothing to dictate into", () => {
    renderHead();
    expect(screen.queryByText("mic")).not.toBeInTheDocument();
  });

  it("shows the run-record card once the head has ended and wrote one", async () => {
    vi.mocked(api.heads.summary).mockResolvedValueOnce({
      run_id: "r1", status: "passed", result_line: "Added the thing.",
      tests: { failed_before: null, passed_after: null }, sabotage: null, kz_ok: null,
      review: null, bypass: null, operator_minutes: null, helpers: null, branch: null, pr_url: null,
    });
    renderHead(mkRun({ state: "passed", run_record: true }));
    await waitFor(() => expect(screen.getByTestId("head-run-record-card")).toBeInTheDocument());
    expect(screen.getByText("Added the thing.")).toBeInTheDocument();
  });

  it("no run-record card for an active run, even if run_record is already true", () => {
    renderHead(mkRun({ state: "running", run_record: true }));
    expect(screen.queryByTestId("head-run-record-card")).not.toBeInTheDocument();
  });

  it("no run-record card when the run never wrote one", () => {
    renderHead(mkRun({ state: "passed", run_record: false }));
    expect(screen.queryByTestId("head-run-record-card")).not.toBeInTheDocument();
  });

  it("falls back to the log tail (not the agent empty-state copy) when there is nothing to show", () => {
    mockUseHeadTranscript.mockReturnValue(mkHeadStream({ events: [] }));
    mockUseHeadTranscriptMeta.mockReturnValue({ source: "none", reader: null, reason: "no_reader" });
    renderHead(mkRun({ state: "passed" }));
    expect(screen.getByTestId("head-no-transcript")).toBeInTheDocument();
    expect(screen.queryByText("noMessagesYet")).not.toBeInTheDocument();
  });

  it("the back chevron calls onBack", async () => {
    const onBack = vi.fn();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ChatView
          agent={null}
          head={mkRun()}
          hasTranscript
          detailLevel="normal"
          onDetailLevelChange={vi.fn()}
          centerView="chat"
          onCenterViewChange={vi.fn()}
          onBack={onBack}
        />
      </QueryClientProvider>,
    );
    screen.getByTestId("head-chat-back").click();
    expect(onBack).toHaveBeenCalled();
  });
});
