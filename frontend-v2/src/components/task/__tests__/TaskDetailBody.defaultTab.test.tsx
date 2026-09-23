/**
 * TaskDetailBody — wave 3a "task detail lite": state card per status, facts
 * row, Summary from the run record, default tab per status, URL-controlled
 * tab, ⋯ menu (thread moved there, delete last), 409 as a sentence, confirm
 * on end states. Fixtures: lib/taskDetail/__tests__/fixtures.ts.
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
  // References / thread / transcript etc. hit fetch directly — answer empty.
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }),
  );
  return vi.spyOn(api.tasks, "update").mockImplementation(async () => {
    const p = routes.patch;
    if (p && p.status >= 400) throw new Error(`API ${p.status}: ${JSON.stringify(p.body)}`);
    return taskFixture();
  });
}

function renderBody(task: Task, opts: { agents?: Agent[]; tab?: string | null; onTabChange?: (t: string) => void } = {}) {
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

describe("default tab per status", () => {
  it("opens Summary for a task that is not running", async () => {
    mockApi();
    renderBody(taskFixture({ status: "blocked" }));
    expect(await screen.findByRole("tab", { name: "Summary" })).toHaveAttribute("aria-selected", "true");
  });

  it("opens Comments while the task is running", async () => {
    mockApi();
    renderBody(taskFixture({ status: "in_progress" }));
    expect(await screen.findByRole("tab", { name: "Comments" })).toHaveAttribute("aria-selected", "true");
  });

  it("has neither a Thread nor an E2E tab — even with e2e_test_required", async () => {
    mockApi();
    renderBody(taskFixture({ e2e_test_required: true }));
    await screen.findByRole("tab", { name: "Summary" });
    expect(screen.queryByRole("tab", { name: /Thread/ })).toBeNull();
    expect(screen.queryByRole("tab", { name: /E2E/ })).toBeNull();
  });
});

describe("tab from the URL", () => {
  it("shows the requested tab and reports switches", async () => {
    mockApi();
    const onTabChange = vi.fn();
    renderBody(taskFixture({ status: "done" }), { tab: "timeline", onTabChange });
    expect(await screen.findByRole("tab", { name: "Timeline" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("tab", { name: "History" }));
    expect(onTabChange).toHaveBeenCalledWith("history");
  });

  it("falls back to the status default for an unknown tab (old links with tab=e2e)", async () => {
    mockApi();
    renderBody(taskFixture({ status: "done" }), { tab: "e2e" });
    expect(await screen.findByRole("tab", { name: "Summary" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("state card", () => {
  it("NEEDS YOU with an open approval embeds the inbox ApprovalCard (Unblock / Cancel task)", async () => {
    const task = taskFixture({ status: "blocked", assigned_agent_id: "agent-1" });
    mockApi({ approvals: [approvalFixture({ description: "Card needs a flip to review" })] });
    renderBody(task);
    const card = await screen.findByTestId("task-state-card");
    expect(card).toHaveAttribute("data-kind", "needs_you");
    expect(card).toHaveTextContent("Needs you");
    expect(card).toHaveTextContent("alpha asks:");
    const embedded = await within(card).findByTestId("state-card-approval");
    expect(within(embedded).getByRole("button", { name: /Unblock/ })).toBeInTheDocument();
    expect(within(embedded).getByRole("button", { name: /Cancel task/ })).toBeInTheDocument();
    // No Reply button when the approval is the way to act.
    expect(within(card).queryByRole("button", { name: "Reply" })).toBeNull();
  });

  it("NEEDS YOU without an approval quotes the blocker and Reply jumps to Comments", async () => {
    mockApi({ comments: [commentFixture({ comment_type: "blocker", content: "Which branch should I use?" })] });
    renderBody(taskFixture({ status: "waiting" }));
    const card = await screen.findByTestId("task-state-card");
    await within(card).findByText("Which branch should I use?");
    fireEvent.click(within(card).getByRole("button", { name: "Reply" }));
    expect(screen.getByRole("tab", { name: "Comments" })).toHaveAttribute("aria-selected", "true");
  });

  it("RUNNING shows the last step", async () => {
    mockApi({ comments: [commentFixture({ comment_type: "progress", content: "Running the test suite" })] });
    renderBody(taskFixture({ status: "in_progress", dispatched_at: "2026-09-22T08:00:00Z" }));
    const card = await screen.findByTestId("task-state-card");
    expect(card).toHaveAttribute("data-kind", "running");
    await within(card).findByText("Running the test suite");
  });

  it("RESULT shows the resolution, PR chip and evidence count", async () => {
    mockApi({ comments: [commentFixture({ comment_type: "resolution", content: "Fixed the sideways scroll." })] });
    renderBody(taskFixture({ status: "done", pr_url: "https://example.test/pr/638", pr_number: 638 }));
    const card = await screen.findByTestId("task-state-card");
    expect(card).toHaveAttribute("data-kind", "result");
    await within(card).findByText("Fixed the sideways scroll.");
    expect(within(card).getByRole("link", { name: /PR #638/ })).toHaveAttribute("href", "https://example.test/pr/638");
    await within(card).findByText("Evidence: 3");
  });

  it("FAILED shows the error and Open log switches to the Timeline", async () => {
    mockApi({ comments: [commentFixture({ comment_type: "blocker", content: "Build failed: missing module" })] });
    renderBody(taskFixture({ status: "failed" }));
    const card = await screen.findByTestId("task-state-card");
    expect(card).toHaveAttribute("data-kind", "failed");
    await within(card).findByText("Build failed: missing module");
    fireEvent.click(within(card).getByRole("button", { name: "Open log" }));
    expect(screen.getByRole("tab", { name: "Timeline" })).toHaveAttribute("aria-selected", "true");
  });

  it("inbox has no card and the facts row says Not started", async () => {
    mockApi();
    renderBody(taskFixture({ status: "inbox" }));
    await screen.findByTestId("task-fact-row");
    expect(screen.queryByTestId("task-state-card")).toBeNull();
    expect(screen.getByTestId("fact-time")).toHaveTextContent("Not started");
  });
});

describe("facts row", () => {
  it("shows agent monogram + name, plan, cost and 'Claude: not tracked'", async () => {
    mockApi();
    renderBody(taskFixture({ status: "blocked", assigned_agent_id: "agent-1" }));
    const agent = await screen.findByTestId("fact-agent");
    expect(agent).toHaveTextContent("AL");
    expect(agent).toHaveTextContent("alpha");
    await waitFor(() => expect(screen.getByTestId("fact-cost")).toHaveTextContent("$0.05"));
    expect(screen.getByTestId("fact-cost")).toHaveTextContent("Claude: not tracked");
  });
});

describe("Summary tab (run record)", () => {
  it("renders the boxes with English labels and the content untranslated", async () => {
    mockApi({ runRecord: runRecordFixture() });
    renderBody(taskFixture({ status: "done", description: "Fix the sideways scroll in chat." }));
    const summary = await screen.findByTestId("run-record-summary");
    for (const label of ["Brief", "Times", "Plan", "Steps", "Evidence", "Decisions", "Friction"]) {
      expect(within(summary).getByText(label)).toBeInTheDocument();
    }
    expect(within(summary).getByText("Fix the sideways scroll in chat.")).toBeInTheDocument();
    expect(screen.getByTestId("summary-evidence")).toHaveTextContent("Files: 2 · Screenshots: 1");
    expect(screen.getByTestId("summary-decisions")).toHaveTextContent("Open: 1 (Blocker decision");
    expect(screen.getByTestId("summary-decisions")).toHaveTextContent("Approved: 2");
    expect(screen.getByTestId("summary-friction")).toHaveTextContent("Escalated to operator ×2 · Review stuck ×1");
  });

  it("says so when the run record cannot be loaded, and offers a retry", async () => {
    mockApi({ runRecord: null });
    renderBody(taskFixture({ status: "done" }));
    expect(await screen.findByText("The summary could not be loaded.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("⋯ menu", () => {
  it("lists Copy link, Copy as Markdown, Save to Vault, the thread — and Delete last after a divider", async () => {
    mockApi();
    renderBody(taskFixture({ status: "done", assigned_agent_id: "agent-1" }));
    fireEvent.click(await screen.findByRole("button", { name: "More actions" }));
    const menu = await screen.findByRole("menu");
    const items = within(menu).getAllByRole("menuitem").map((el) => el.textContent);
    expect(items[0]).toContain("Copy link");
    expect(items[1]).toContain("Copy as Markdown");
    expect(items[2]).toContain("Save to Vault");
    expect(items[3]).toContain("Open alpha thread");
    expect(items[items.length - 1]).toContain("Delete");
    expect(within(menu).getByRole("separator")).toBeInTheDocument();
  });

  it("greys out Save to Vault with the reason when the viewer lacks the operator role", async () => {
    storeState.currentUser = { id: "u1", name: "viewer", role: "viewer" };
    mockApi();
    renderBody(taskFixture({ status: "done" }));
    fireEvent.click(await screen.findByRole("button", { name: "More actions" }));
    const save = within(await screen.findByRole("menu")).getByRole("menuitem", { name: /Save to Vault/ });
    expect(save).toBeDisabled();
    expect(save).toHaveTextContent("Needs the operator role");
  });

  it("enables Save to Vault for an operator", async () => {
    storeState.currentUser = { id: "u1", name: "op", role: "operator" };
    mockApi();
    renderBody(taskFixture({ status: "done" }));
    fireEvent.click(await screen.findByRole("button", { name: "More actions" }));
    expect(within(await screen.findByRole("menu")).getByRole("menuitem", { name: /Save to Vault/ })).toBeEnabled();
  });

  it("opens the thread view from the menu", async () => {
    mockApi();
    const onTabChange = vi.fn();
    renderBody(taskFixture({ status: "done" }), { onTabChange });
    fireEvent.click(await screen.findByRole("button", { name: "More actions" }));
    fireEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: /thread/i }));
    expect(onTabChange).toHaveBeenCalledWith("thread");
    expect(screen.getByRole("button", { name: "Back to summary" })).toBeInTheDocument();
  });
});

describe("status changes", () => {
  it("shows a refused transition (409) as one sentence", async () => {
    const error = vi.spyOn(notify, "error").mockImplementation(() => undefined as never);
    mockApi({
      patch: {
        status: 409,
        body: {
          detail: {
            error: "invalid_transition",
            current_status: "blocked",
            expected: "review",
            allowed: ["inbox"],
            message: "Ungültiger Status-Übergang",
          },
        },
      },
    });
    renderBody(taskFixture({ status: "blocked" }));
    fireEvent.click(await screen.findByRole("button", { name: /Status: Blocked/ }));
    fireEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: "Review" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("This status change isn't allowed from Blocked."));
  });

  it("asks before Done and only then sends it", async () => {
    const updateSpy = mockApi();
    renderBody(taskFixture({ status: "review" }));
    fireEvent.click(await screen.findByRole("button", { name: /Status: Review/ }));
    fireEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: "Done" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Mark this task as done?");
    expect(updateSpy).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Mark done" }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith("board-1", "task-1", { status: "done" }));
  });
});
