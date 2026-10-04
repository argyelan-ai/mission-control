/**
 * `useHeadTranscript` — shape parity with `useChatStream` (bauplan
 * `heads-sichtbar` PR 2 §3.2), the 304-keeps-the-page contract, and the
 * poll-only-while-active rule (`isHeadTranscriptActive`, tested directly —
 * actually waiting out the 5s interval would make this suite slow and the
 * scheduling itself is TanStack Query's own, already-proven machinery).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { isHeadTranscriptActive, useHeadTranscript } from "../useHeadTranscript";

const mocks = vi.hoisted(() => ({ history: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: { heads: { history: mocks.history } } }));

function mkPage(overrides: Record<string, unknown> = {}) {
  return {
    events: [{ kind: "message", uuid: "m1", ts: "2026-10-04T09:00:00Z", role: "assistant", text: "hi", model: null, sidechain: false }],
    session: { sessionId: "sess-1", live: true, startedAt: "2026-10-04T09:00:00Z", aliveness: "active" },
    hasMore: false,
    subagentRuns: [],
    source: "transcript",
    reader: "claude",
    reason: null,
    ...overrides,
  };
}

beforeEach(() => mocks.history.mockReset());

describe("isHeadTranscriptActive", () => {
  it("polls while running/starting, stops once ended", () => {
    expect(isHeadTranscriptActive("r1", "running")).toBe(true);
    expect(isHeadTranscriptActive("r1", "starting")).toBe(true);
    for (const state of ["needs_you", "passed", "failed", "stopped"] as const) {
      expect(isHeadTranscriptActive("r1", state)).toBe(false);
    }
  });

  it("no run id at all never polls; unknown state defaults to active", () => {
    expect(isHeadTranscriptActive(null, "running")).toBe(false);
    expect(isHeadTranscriptActive("r1", null)).toBe(true);
  });
});

describe("useHeadTranscript", () => {
  function renderWithClient(runId: string | null, headState: Parameters<typeof useHeadTranscript>[1] = "running") {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    function wrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
    }
    const view = renderHook(({ id, state }: { id: string | null; state: typeof headState }) => useHeadTranscript(id, state), {
      wrapper,
      initialProps: { id: runId, state: headState },
    });
    return { qc, ...view };
  }

  it("seeds events from the first page and exposes the same shape useChatStream does", async () => {
    mocks.history.mockResolvedValueOnce({ data: mkPage(), etag: '"v1"' });
    const { result } = renderWithClient("run-1");
    await waitFor(() => expect(result.current.events).toHaveLength(1));
    expect(result.current.session?.sessionId).toBe("sess-1");
    // The no-op composer surface a head has none of — present so `ChatView`
    // can treat `stream` as one type regardless of source.
    expect(result.current.pendingEchoes).toEqual([]);
    expect(result.current.capabilities).toBeNull();
    expect(typeof result.current.echoSent).toBe("function");
  });

  it("a 304 (null) keeps the page on screen instead of blanking it out", async () => {
    mocks.history.mockResolvedValueOnce({ data: mkPage(), etag: '"v1"' });
    const { result, qc } = renderWithClient("run-1");
    await waitFor(() => expect(result.current.events).toHaveLength(1));

    mocks.history.mockResolvedValueOnce(null);
    await qc.refetchQueries({ queryKey: ["heads", "run-1", "chat", "history"] });
    await waitFor(() => expect(mocks.history).toHaveBeenCalledTimes(2));
    // Sabotage check (manual): returning the raw `null` here instead of the
    // remembered last page is exactly the bug this guards — `events` would
    // drop to `[]` on every 304.
    expect(result.current.events).toHaveLength(1);
    expect(result.current.session?.sessionId).toBe("sess-1");
  });

  it("sends the previous page's etag back as If-None-Match on the next call", async () => {
    mocks.history.mockResolvedValueOnce({ data: mkPage(), etag: '"v1"' });
    const { qc } = renderWithClient("run-1");
    await waitFor(() => expect(mocks.history).toHaveBeenCalledTimes(1));

    mocks.history.mockResolvedValueOnce(null);
    await qc.refetchQueries({ queryKey: ["heads", "run-1", "chat", "history"] });
    await waitFor(() => expect(mocks.history).toHaveBeenCalledTimes(2));
    expect(mocks.history).toHaveBeenLastCalledWith("run-1", { limit: 1000, etag: '"v1"' });
  });

  it("switching to a new run id starts that run's own page, not the old run's", async () => {
    mocks.history.mockResolvedValueOnce({ data: mkPage(), etag: '"v1"' });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    function wrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
    }
    const { result, rerender } = renderHook(({ id }: { id: string }) => useHeadTranscript(id, "running"), {
      wrapper,
      initialProps: { id: "run-1" },
    });
    await waitFor(() => expect(result.current.events).toHaveLength(1));

    mocks.history.mockResolvedValueOnce({
      data: mkPage({
        events: [{ kind: "message", uuid: "m2", ts: "2026-10-04T09:05:00Z", role: "assistant", text: "run 2", model: null, sidechain: false }],
        session: { sessionId: "sess-2", live: true, startedAt: null, aliveness: "active" },
      }),
      etag: '"v9"',
    });
    rerender({ id: "run-2" });
    await waitFor(() => expect(result.current.session?.sessionId).toBe("sess-2"));
    expect(result.current.events.map((e) => (e as { uuid: string }).uuid)).toEqual(["m2"]);
    // A stale etag from run-1 must never be sent for run-2's first request.
    expect(mocks.history).toHaveBeenLastCalledWith("run-2", { limit: 1000, etag: null });
  });

  it("an ended run still fetches its own final page once (the invalidate-on-end effect)", async () => {
    mocks.history.mockResolvedValue({
      data: mkPage({ session: { sessionId: "sess-1", live: false, startedAt: null, aliveness: "ended" } }),
      etag: '"v2"',
    });
    const { result } = renderWithClient("run-1", "passed");
    await waitFor(() => expect(result.current.session?.aliveness).toBe("ended"));
    expect(mocks.history).toHaveBeenCalled();
  });
});
