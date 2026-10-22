/**
 * Spec A hooks (PR-3, test 9): ETag / 304, the cursor delta with `open`,
 * `hasMore` followed at once, and no requests while the chat is not on screen.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, requestWithEtag } from "@/lib/api";
import { useChatTasks, chatTasksInterval, isTaskFlip, CHAT_TASKS_IDLE_POLL_MS, CHAT_TASKS_LIVE_POLL_MS } from "../useChatTasks";
import { useTaskSteps, stepsInterval, TASK_STEPS_POLL_MS, TASK_STEPS_WAITING_POLL_MS } from "../useTaskSteps";
import type { TaskStepsResponse } from "@/lib/chatTypes";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { chat: { tasks: vi.fn(), taskSteps: vi.fn(), taskStepDetail: vi.fn() } } };
});

const TASK = "00000000-0000-4000-8000-000000000001";

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const W = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  return { qc, W };
}

function page(over: Partial<Extract<TaskStepsResponse, { applies: true }>> = {}): TaskStepsResponse {
  return {
    applies: true, taskId: TASK, status: "in_progress", live: true, hasSession: true, attempts: [],
    rows: [], updated: [], cursor: "v1.0", hasMore: false, reset: false, omitted: 0, liveRow: null, ...over,
  };
}

beforeEach(() => vi.clearAllMocks());

describe("requestWithEtag", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it("sends If-None-Match and answers null on 304", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 304 }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    expect(await requestWithEtag("/x", '"abc"')).toBeNull();
    expect((fetchMock.mock.calls[0][1] as RequestInit).headers).toMatchObject({ "If-None-Match": '"abc"' });
  });

  it("returns the body and the ETag on 200", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: 1 }), { status: 200, headers: { ETag: '"e2"' } }),
    ) as unknown as typeof fetch;
    expect(await requestWithEtag("/x")).toEqual({ data: { ok: 1 }, etag: '"e2"' });
  });
});

describe("useChatTasks", () => {
  it("sends the last ETag and keeps the data on 304", async () => {
    const { qc, W } = wrapper();
    vi.mocked(api.chat.tasks)
      .mockResolvedValueOnce({ data: { applies: true, tasks: [], queued: 2, hasMore: false }, etag: '"t1"' })
      .mockResolvedValueOnce(null);
    const { result } = renderHook(() => useChatTasks("agent-a", true), { wrapper: W });
    await waitFor(() => expect(result.current.data).toMatchObject({ queued: 2 }));
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["chat-tasks", "agent-a"] });
    });
    expect(vi.mocked(api.chat.tasks).mock.calls[1][1]).toMatchObject({ etag: '"t1"' });
    expect(result.current.data).toMatchObject({ queued: 2 });
  });

  it("asks nothing while the chat is not on screen (N7)", async () => {
    const { W } = wrapper();
    renderHook(() => useChatTasks("agent-a", false), { wrapper: W });
    await act(async () => {});
    expect(api.chat.tasks).not.toHaveBeenCalled();
  });

  it("polls 10 s with a live task, 60 s without", () => {
    const live = { applies: true as const, tasks: [{ live: true }] as never, queued: 0, hasMore: false };
    expect(chatTasksInterval(live)).toBe(CHAT_TASKS_LIVE_POLL_MS);
    expect(chatTasksInterval({ applies: true, tasks: [], queued: 0, hasMore: false })).toBe(CHAT_TASKS_IDLE_POLL_MS);
  });
});

describe("useTaskSteps", () => {
  const spec = { taskId: TASK, live: true, status: "in_progress", load: true };

  it("first page without a cursor, then the delta with cursor, open ids and ETag; 304 keeps the state", async () => {
    const { qc, W } = wrapper();
    vi.mocked(api.chat.taskSteps)
      .mockResolvedValueOnce({
        data: page({ rows: [{ id: "1:3", ts: "2026-10-21T23:00:00Z", k: "tool", name: "bash", title: "npm test", st: "running" }], cursor: "v1.4" }),
        etag: '"a"',
      })
      .mockResolvedValueOnce(null);
    const specs = [spec];
    const { result } = renderHook(() => useTaskSteps("agent-a", specs, true), { wrapper: W });
    await waitFor(() => expect(result.current.get(TASK)?.state?.rows).toHaveLength(1));
    expect(vi.mocked(api.chat.taskSteps).mock.calls[0][2]).toMatchObject({ limit: 1000, after: null });
    const first = result.current.get(TASK)?.state;
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["chat-task-steps", "agent-a", TASK] });
    });
    expect(vi.mocked(api.chat.taskSteps).mock.calls[1][2]).toMatchObject({ after: "v1.4", open: ["1:3"], etag: '"a"' });
    expect(result.current.get(TASK)?.state).toBe(first);
  });

  it("follows `hasMore` at once (S11)", async () => {
    const { W } = wrapper();
    vi.mocked(api.chat.taskSteps)
      .mockResolvedValueOnce({ data: page({ rows: [{ id: "1:1", ts: null, k: "text", text: "a" }], cursor: "v1.1", hasMore: true }), etag: null })
      .mockResolvedValueOnce({ data: page({ rows: [{ id: "1:2", ts: null, k: "text", text: "b" }], cursor: "v1.2" }), etag: null });
    const specs = [spec];
    const { result } = renderHook(() => useTaskSteps("agent-a", specs, true), { wrapper: W });
    await waitFor(() => expect(result.current.get(TASK)?.state?.rows).toHaveLength(2));
    expect(vi.mocked(api.chat.taskSteps).mock.calls[1][2]).toMatchObject({ after: "v1.1" });
  });

  it("asks nothing off screen, nor for a card that was not opened", async () => {
    const { W } = wrapper();
    const offScreen = [spec];
    renderHook(() => useTaskSteps("agent-a", offScreen, false), { wrapper: W });
    const closedCard = [{ ...spec, live: false, load: false }];
    renderHook(() => useTaskSteps("agent-a", closedCard, true), { wrapper: W });
    await act(async () => {});
    expect(api.chat.taskSteps).not.toHaveBeenCalled();
  });

  it("polls 2 s while live, 10 s while waiting on the operator, not at all when finished", () => {
    expect(stepsInterval(spec)).toBe(TASK_STEPS_POLL_MS);
    expect(stepsInterval({ ...spec, status: "waiting" })).toBe(TASK_STEPS_WAITING_POLL_MS);
    expect(stepsInterval({ ...spec, live: false })).toBe(false);
  });
});

describe("isTaskFlip (review MUST-2 on #937)", () => {
  it("a crossing to or from task_working / waiting_model / idle asks; anything else does not", () => {
    expect(isTaskFlip("idle", "task_working")).toBe(true);
    expect(isTaskFlip("task_working", "idle")).toBe(true);
    expect(isTaskFlip("waiting_model", "task_working")).toBe(true);
    expect(isTaskFlip("working", "idle")).toBe(true);
    expect(isTaskFlip("working", "waiting_input")).toBe(false);
    expect(isTaskFlip("idle", "idle")).toBe(false);
    // The first state of a mount is not a flip (the first fetch runs anyway).
    expect(isTaskFlip(null, "task_working")).toBe(false);
  });
});
