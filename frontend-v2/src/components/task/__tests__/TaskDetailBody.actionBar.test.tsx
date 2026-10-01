/**
 * TaskDetailBody on the phone — mobile nav V2 action bar at the bottom:
 * Back · main action · Reply · More. The main action mirrors the next step's
 * one primary button. Setup copied from TaskDetailBody.defaultTab.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TaskDetailBody } from "../TaskDetailBody";
import { notify } from "@/lib/notify";
import { api } from "@/lib/api";
import type { Agent, Approval, RunRecord, Task, TaskComment } from "@/lib/types";
import {
  agentFixture,
  approvalFixture,
  commentFixture,
  runRecordFixture,
  taskFixture,
} from "@/lib/taskDetail/__tests__/fixtures";

const storeState: { currentUser: { id: string; name: string; role: string } | null } = { currentUser: null };
vi.mock("@/lib/store", () => ({
  useNotificationStore: Object.assign(() => ({ notifications: [] }), {
    getState: () => ({ addNotification: () => {} }),
  }),
  useAppStore: (selector?: (s: typeof storeState) => unknown) => (selector ? selector(storeState) : storeState),
}));

type Routes = {
  runRecord?: RunRecord | null;
  approvals?: Approval[];
  comments?: TaskComment[];
  patch?: { status: number; body: unknown };
  subtasks?: { id: string; title: string; status: string }[];
};

function mockApi(routes: Routes = {}) {
  vi.spyOn(api.tasks, "runRecord").mockImplementation(async () => {
    if (routes.runRecord === null) throw new Error("API 500: boom");
    return routes.runRecord ?? runRecordFixture();
  });
  vi.spyOn(api.approvals, "list").mockResolvedValue(routes.approvals ?? []);
  vi.spyOn(api.tasks.comments, "list").mockResolvedValue(routes.comments ?? []);
  vi.spyOn(api.tasks, "hierarchy").mockResolvedValue({
    parent: null,
    children: (routes.subtasks ?? []).map((c) => ({ ...c, assigned_agent_id: null, priority: "medium" })),
    report_back: null,
    has_credentials: false,
  } as unknown as Awaited<ReturnType<typeof api.tasks.hierarchy>>);
  vi.spyOn(api.tasks, "dependencies").mockResolvedValue([]);
  vi.spyOn(api.tasks.checklist, "list").mockResolvedValue([]);
  vi.spyOn(api.projects, "list").mockResolvedValue([]);
  vi.spyOn(api.tasks, "timeline").mockResolvedValue({ task: {} as never, entries: [], total: 0, truncated: false });
  vi.spyOn(api.tasks, "events").mockResolvedValue([]);
  vi.spyOn(api.tasks.deliverables, "list").mockResolvedValue([]);
  // The thread panel reads `messages` off the response — a bare `[]` from the
  // generic fetch stub made ThreadPanel crash (`undefined.find`) as an
  // unhandled error on slower CI runs.
  vi.spyOn(api.tasks.thread, "list").mockResolvedValue({
    task_id: "task-1", recipient: null, messages: [], has_more_before: false, latest_seq: 0, my_read_seq: 0,
  });
  // References / transcript etc. hit fetch directly — answer empty.
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }),
  );
  return vi.spyOn(api.tasks, "update").mockImplementation(async () => {
    const p = routes.patch;
    if (p && p.status >= 400) throw new Error(`API ${p.status}: ${JSON.stringify(p.body)}`);
    return taskFixture();
  });
}

function renderBody(
  task: Task,
  opts: { agents?: Agent[]; tab?: string | null; onTabChange?: (t: string) => void; onBack?: () => void } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TaskDetailBody
        task={task}
        agents={opts.agents ?? [agentFixture()]}
        boardId="board-1"
        onClose={() => {}}
        tab={opts.tab}
        onTabChange={opts.onTabChange}
        onBack={opts.onBack}
        backLabel={opts.onBack ? "Tasks" : undefined}
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  storeState.currentUser = null;
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("phone action bar", () => {
  it("is there only on the phone chrome (with onBack)", async () => {
    mockApi();
    renderBody(taskFixture({ status: "done" }));
    await screen.findByRole("tab", { name: "Summary" });
    expect(screen.queryByRole("toolbar", { name: "Actions" })).toBeNull();
  });

  it("Back, Reply and More work from the bottom", async () => {
    mockApi();
    const onBack = vi.fn();
    renderBody(taskFixture({ status: "done" }), { onBack });
    const bar = await screen.findByRole("toolbar", { name: "Actions" });
    fireEvent.click(within(bar).getByRole("button", { name: "Back" }));
    expect(onBack).toHaveBeenCalledTimes(1);

    fireEvent.click(within(bar).getByRole("button", { name: "Reply" }));
    expect(screen.getByRole("tab", { name: "Comments" })).toHaveAttribute("aria-selected", "true");

    fireEvent.click(within(bar).getByRole("button", { name: "More actions" }));
    expect(await screen.findByRole("menu")).toBeInTheDocument();
  });

  it("mirrors the next step's primary button (failed → Open log)", async () => {
    mockApi();
    renderBody(taskFixture({ status: "failed" }), { onBack: () => {} });
    const bar = await screen.findByRole("toolbar", { name: "Actions" });
    const head = document.querySelector('[data-region="task-head"]') as HTMLElement;
    const primary = head.querySelector("button.main-action") as HTMLButtonElement;
    expect(primary).not.toBeNull();
    const main = await within(bar).findByTestId("detail-main-action");
    expect(main).toHaveTextContent(primary.textContent!.trim());

    fireEvent.click(main);
    // the same effect as tapping the button in place
    await waitFor(() => expect(screen.getByRole("tab", { name: "Timeline" })).toHaveAttribute("aria-selected", "true"));
  });
});
