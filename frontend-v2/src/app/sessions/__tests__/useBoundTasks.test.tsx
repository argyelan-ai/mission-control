import { describe, it, expect, vi, afterEach } from "vitest";
import type { ReactNode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useBoundTasks } from "../useBoundTasks";

type TaskRow = Awaited<ReturnType<typeof api.tasks.get>>;

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  const get = vi
    .spyOn(api.tasks, "get")
    .mockImplementation(async (_board, id) => ({ id, title: `Task ${id}` }) as unknown as TaskRow);
  const agents = [{ current_task_id: "t-1" }];
  const hook = renderHook(() => useBoundTasks("board-1", agents), { wrapper });
  return { qc, get, hook };
}

afterEach(() => vi.restoreAllMocks());

// Task mutations all over the app (create, move, state change, head restart)
// refresh the task cache by the `["tasks"]` / `["tasks", boardId]` prefix.
// The chat's bound tasks must answer to that prefix too, or the picker keeps
// showing an old task title after a change.
describe("useBoundTasks — follows the app's task refreshes", () => {
  it("asks again when the whole task cache is refreshed (['tasks'])", async () => {
    const { qc, get, hook } = setup();
    await waitFor(() => expect(hook.result.current.isSuccess).toBe(true));
    expect(get).toHaveBeenCalledTimes(1);
    await qc.invalidateQueries({ queryKey: ["tasks"] });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });

  it("asks again when the board's task cache is refreshed (['tasks', boardId])", async () => {
    const { qc, get, hook } = setup();
    await waitFor(() => expect(hook.result.current.isSuccess).toBe(true));
    await qc.invalidateQueries({ queryKey: ["tasks", "board-1"] });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });

  it("leaves another board's refresh alone", async () => {
    const { qc, get, hook } = setup();
    await waitFor(() => expect(hook.result.current.isSuccess).toBe(true));
    await qc.invalidateQueries({ queryKey: ["tasks", "board-2"] });
    expect(get).toHaveBeenCalledTimes(1);
  });
});
