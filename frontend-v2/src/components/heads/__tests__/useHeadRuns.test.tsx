/**
 * `useHeadRuns` — the one shared "/heads?recent_days=" fetch (bauplan
 * `heads-sichtbar` PR 2 §3.2). Proves: it waits for `useHeadsEnabled`, it
 * asks for `recentDays: 7`, and `byTask` picks the newest run per task
 * (sabotage: without `sortRunsNewestFirst` the OLDER run would win when a
 * task has two).
 */
import { describe, it, expect, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useHeadRuns } from "../useHeadRuns";
import { mkRun } from "@/lib/__tests__/headFixtures";

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  occupancy: vi.fn(async () => ({ boxes: {} })),
}));

vi.mock("@/lib/api", () => ({
  api: { heads: { list: mocks.list, occupancy: mocks.occupancy } },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useHeadRuns", () => {
  it("does nothing until heads are known to be enabled, then asks for recent_days=7", async () => {
    mocks.list.mockResolvedValue({ runs: [], archived_count: 0 });
    const { result } = renderHook(() => useHeadRuns(), { wrapper });
    await waitFor(() => expect(mocks.occupancy).toHaveBeenCalled());
    await waitFor(() => expect(mocks.list).toHaveBeenCalledWith({ recentDays: 7 }));
    expect(result.current.runs).toEqual([]);
  });

  it("byTask picks the NEWEST run per task", async () => {
    const older = mkRun({ run_id: "old", task_id: "t1", created_at: "2026-09-20T10:00:00Z" });
    const newer = mkRun({ run_id: "new", task_id: "t1", created_at: "2026-09-23T10:00:00Z" });
    const otherTask = mkRun({ run_id: "other", task_id: "t2", created_at: "2026-09-22T10:00:00Z" });
    mocks.list.mockResolvedValue({ runs: [older, newer, otherTask], archived_count: 2 });

    const { result } = renderHook(() => useHeadRuns(), { wrapper });
    await waitFor(() => expect(result.current.byTask.size).toBe(2));
    expect(result.current.byTask.get("t1")?.run_id).toBe("new");
    expect(result.current.byTask.get("t2")?.run_id).toBe("other");
    expect(result.current.archivedCount).toBe(2);
  });

  it("a task with no head run at all is simply absent from byTask", async () => {
    mocks.list.mockResolvedValue({ runs: [mkRun({ run_id: "a", task_id: "t1" })], archived_count: 0 });
    const { result } = renderHook(() => useHeadRuns(), { wrapper });
    await waitFor(() => expect(result.current.byTask.size).toBe(1));
    expect(result.current.byTask.get("unrelated-task")).toBeUndefined();
  });
});
