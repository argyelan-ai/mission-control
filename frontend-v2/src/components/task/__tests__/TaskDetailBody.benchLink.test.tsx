/**
 * A bench agent run is a fleet task. Its result belongs to the benchmark:
 * the task detail (head and Files tab) leads there — the operator must not
 * end at the task's deliverables (27.10.).
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TaskDetailBody } from "../TaskDetailBody";
import { api } from "@/lib/api";
import type { Task } from "@/lib/types";
import { agentFixture, runRecordFixture, taskFixture } from "@/lib/taskDetail/__tests__/fixtures";

vi.mock("@/lib/store", () => ({
  useNotificationStore: Object.assign(() => ({ notifications: [] }), {
    getState: () => ({ addNotification: () => {} }),
  }),
  useAppStore: (selector?: (s: { currentUser: null }) => unknown) =>
    selector ? selector({ currentUser: null }) : { currentUser: null },
}));

const TEST_ID = "bb467c57-8213-4438-a405-08b1d4cc7de2";

function mockApi() {
  vi.spyOn(api.tasks, "runRecord").mockResolvedValue(runRecordFixture());
  vi.spyOn(api.approvals, "list").mockResolvedValue([]);
  vi.spyOn(api.tasks.comments, "list").mockResolvedValue([]);
  vi.spyOn(api.tasks, "hierarchy").mockResolvedValue({
    parent: null,
    children: [],
    report_back: null,
    has_credentials: false,
  } as unknown as Awaited<ReturnType<typeof api.tasks.hierarchy>>);
  vi.spyOn(api.tasks, "dependencies").mockResolvedValue([]);
  vi.spyOn(api.tasks.checklist, "list").mockResolvedValue([]);
  vi.spyOn(api.projects, "list").mockResolvedValue([]);
  vi.spyOn(api.tasks, "timeline").mockResolvedValue({ task: {} as never, entries: [], total: 0, truncated: false });
  vi.spyOn(api.tasks, "events").mockResolvedValue([]);
  vi.spyOn(api.tasks.deliverables, "list").mockResolvedValue([]);
  vi.spyOn(api.tasks.thread, "list").mockResolvedValue({
    task_id: "task-1", recipient: null, messages: [], has_more_before: false, latest_seq: 0, my_read_seq: 0,
  });
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }),
  );
}

function renderBody(task: Task, tab?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TaskDetailBody task={task} agents={[agentFixture()]} boardId="board-1" onClose={() => {}} tab={tab} />
    </QueryClientProvider>,
  );
}

const benchTask = (over: Partial<Task> = {}) =>
  taskFixture({
    status: "done",
    title: "[Bench] Pagoda Garden — Example-Model",
    is_auto_created: true,
    auto_reason: `bench_studio challenge ${TEST_ID}`,
    ...over,
  });

afterEach(() => vi.restoreAllMocks());

describe("task detail of a bench run", () => {
  it("the head leads to the result in the benchmark", async () => {
    mockApi();
    renderBody(benchTask());
    const link = await screen.findByTestId("task-bench-result");
    expect(link).toHaveAttribute("href", `/bench/r/${TEST_ID}`);
    expect(link).toHaveTextContent("View result in Benchmark");
  });

  it("also while the agent still builds: the live run is in the benchmark too", async () => {
    mockApi();
    renderBody(benchTask({ status: "in_progress" }));
    expect(await screen.findByTestId("task-bench-result")).toHaveAttribute("href", `/bench/r/${TEST_ID}`);
  });

  it("the Files tab says where the result is judged", async () => {
    mockApi();
    renderBody(benchTask(), "deliverables");
    const panel = await screen.findByRole("tabpanel");
    const link = await within(panel).findByTestId("deliverables-bench-result");
    expect(link).toHaveAttribute("href", `/bench/r/${TEST_ID}`);
  });

  it("a plain task has no benchmark link", async () => {
    mockApi();
    renderBody(taskFixture({ status: "done" }), "deliverables");
    await screen.findByRole("tabpanel");
    expect(screen.queryByTestId("task-bench-result")).toBeNull();
    expect(screen.queryByTestId("deliverables-bench-result")).toBeNull();
  });
});
