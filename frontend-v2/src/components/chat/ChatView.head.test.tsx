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
import type { MessageEvent, TimelineChatEvent } from "@/lib/chatTypes";
import type { UseChatStreamResult } from "@/hooks/useChatStream";
import historyClaude from "@/__fixtures__/heads/history-claude.json";
import historyOmp from "@/__fixtures__/heads/history-omp.json";

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

  it("re-rendering the SAME instance from no-head to a head never throws (Rules of Hooks — review finding on PR #756)", () => {
    // Reproduces the real crash: `sessions/page.tsx` mounts `ChatView` with
    // `head={selectedHeadRun}` while that run is still resolving (the
    // Archive sheet's "tap a row" path — an archived run is never in the
    // recent list — and every `?head=` deep link, since the recent list
    // has not loaded on page load), so the SAME component instance
    // re-renders from `head: null` to a real `head` a tick later. With the
    // early-return guard positioned BEFORE the hooks below it (the
    // previous shape of this component), that second render called more
    // hooks than the first and React threw "Rendered more hooks than
    // during the previous render" — caught here with the real component,
    // no stub.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <ChatView
          agent={null}
          head={null}
          hasTranscript={false}
          detailLevel="normal"
          onDetailLevelChange={vi.fn()}
          centerView="chat"
          onCenterViewChange={vi.fn()}
          onBack={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(screen.getByText("Pick a chat in the sidebar.")).toBeInTheDocument();

    expect(() =>
      rerender(
        <QueryClientProvider client={qc}>
          <ChatView
            agent={null}
            head={mkRun({ state: "running" })}
            hasTranscript
            detailLevel="normal"
            onDetailLevelChange={vi.fn()}
            centerView="chat"
            onCenterViewChange={vi.fn()}
            onBack={vi.fn()}
          />
        </QueryClientProvider>,
      ),
    ).not.toThrow();
    expect(screen.getByTestId("chat-header")).toHaveAttribute("data-kind", "head");
  });

  // Harness-neutral guard (bauplan §3.3, review finding on PR #756): both
  // fixtures are the REAL PR-1 `transcript.read()` output over
  // `backend/tests/fixtures/heads/{claude-run,omp-run}` (regenerated with
  // `app.services.heads.transcript.read`, already scrubbed/masked — see
  // those fixtures' own docstring). Only the backend had two-harness
  // coverage before this; `ChatView.head.test.tsx` itself rendered a
  // single synthetic `MessageEvent` for both harnesses alike.
  it.each([
    ["claude", historyClaude],
    ["omp", historyOmp],
  ])("renders the real %s reader output — tool groups, thinking and messages all present", async (_harness, history) => {
    mockUseHeadTranscript.mockReturnValue(
      mkHeadStream({ events: history.events as unknown as TimelineChatEvent[], session: history.session as never }),
    );
    const { container } = renderHead(mkRun({ state: "passed", harness: _harness, model: "GLM-5.3-Flash-EXL3" }));

    // A message rendered (markdown splits the text across several DOM
    // nodes — `container.textContent` is the robust check, not a single
    // `getByText`). The component mounts only the last `INITIAL_RENDER_
    // WINDOW` timeline items first and joins the rest one animation frame
    // later ("Tail first" — real transcripts, unlike the single synthetic
    // `MSG` every other test here uses, are long enough to hit that
    // window), so this waits for the full, deferred render.
    const firstMessage = (history.events as { kind: string; role?: string; text?: string }[]).find(
      (ev) => ev.kind === "message" && ev.role === "user",
    );
    expect(firstMessage?.text).toBeTruthy();
    const needle = firstMessage!.text!.split("\n")[0].replace(/^#\s*/, "").trim();
    await waitFor(() => expect(container.textContent).toContain(needle));

    // A tool event rendered — either as its own `ToolRow` with its real
    // title text visible (Claude's real transcript stamps a `usage` event
    // after EVERY tool call, which closes the activity run at length 1
    // every time, so nothing ever groups there) or folded into a
    // `ToolGroup` chip, collapsed by default, which shows "N Tool(s)
    // verwendet" instead of the individual titles (omp's real transcript:
    // long uninterrupted tool/thinking runs). A real, harness-specific
    // rendering difference this test deliberately does not paper over.
    const firstTool = (history.events as { kind: string; title?: string }[]).find((ev) => ev.kind === "tool");
    expect(firstTool?.title).toBeTruthy();
    const toolGroups = screen.queryAllByTestId("tool-group");
    if (toolGroups.length > 0) {
      // Case-insensitive: the chip's own EN label is lowercase ("tool
      // used" — review finding on PR #756 round 3, ToolGroup's summary
      // line used to be hardcoded German, now i18n'd and no longer
      // capitalised the way the old "Tool" check assumed).
      expect(toolGroups.some((g) => /tool/i.test(g.textContent ?? ""))).toBe(true);
    } else {
      expect(container.textContent).toContain(firstTool!.title);
    }

    // A thinking event rendered — grouped ("N× thought"/"N× nachgedacht") or
    // standalone ("Denkt nach…", `ThinkingRow`'s own collapsed label) — same
    // either/or reasoning as the tool check above.
    expect(/nachgedacht|Denkt nach|thought/i.test(container.textContent ?? "")).toBe(true);
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
