import { describe, it, expect, vi, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/lib/api", () => ({ api: { chat: { taskActivity: vi.fn() } } }));

import { api } from "@/lib/api";
import { TASK_ACTIVITY_IDLE_POLL_MS, TASK_ACTIVITY_POLL_MS, useTaskActivity } from "./useTaskActivity";

const taskActivity = vi.mocked(api.chat.taskActivity);

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

async function callsIn(ms: number): Promise<number> {
  renderHook(() => useTaskActivity("agent-1"), { wrapper });
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  return taskActivity.mock.calls.length;
}

describe("useTaskActivity — how often it asks", () => {
  afterEach(() => {
    vi.useRealTimers();
    taskActivity.mockReset();
  });

  it("a running task block asks every 2 s", async () => {
    vi.useFakeTimers();
    taskActivity.mockResolvedValue({
      applies: true, taskId: "t", hasSession: true, deliveredAt: null, events: [], preview: null, queued: 0,
    });
    const n = await callsIn(30_000);
    expect(n).toBeGreaterThanOrEqual(30_000 / TASK_ACTIVITY_POLL_MS - 1);
  });

  // Review R2: Claude, Kimi, Grok, native omp … answer applies:false for good
  // — every open chat with a bound task asked every 2 s for nothing.
  it("a harness whose chat shows the task is asked rarely, not every 2 s", async () => {
    vi.useFakeTimers();
    taskActivity.mockResolvedValue({ applies: false });
    const n = await callsIn(30_000);
    expect(TASK_ACTIVITY_IDLE_POLL_MS).toBeGreaterThanOrEqual(30_000);
    expect(n).toBeLessThanOrEqual(2);
  });
});
