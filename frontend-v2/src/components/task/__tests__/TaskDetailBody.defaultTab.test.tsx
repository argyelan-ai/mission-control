/**
 * TaskDetailBody — wave 3a "task detail lite" + header variant A: state line
 * and next step per status, properties list, Summary from the run record, default tab per status, URL-controlled
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
    // Who asks and since when is the state sentence, once (K3/K10).
    await waitFor(() => expect(screen.getByTestId("task-state-line")).toHaveTextContent(/Blocked · alpha asked .+ ago/));
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
    // Evidence counts live in the Summary (EVIDENCE row), not in the header.
    expect(within(card).queryByText(/Evidence/)).toBeNull();
    // The PR shows once: in the header, not again as a property.
    await screen.findByTestId("task-properties");
    expect(screen.queryByTestId("fact-pr")).toBeNull();
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

  it("inbox has no next step and the state line says not started", async () => {
    mockApi();
    renderBody(taskFixture({ status: "inbox" }));
    await screen.findByTestId("task-properties");
    expect(screen.queryByTestId("task-state-card")).toBeNull();
    expect(screen.getByTestId("task-state-line")).toHaveTextContent("Inbox · not started");
  });
});

describe("properties", () => {
  it("lists status, agent, project and the billed cost — without the 'not tracked' filler", async () => {
    mockApi();
    renderBody(taskFixture({ status: "blocked", assigned_agent_id: "agent-1" }));
    const props = await screen.findByTestId("task-properties");
    expect(within(props).getByRole("button", { name: /Status: Blocked/ })).toBeInTheDocument();
    expect(screen.getByTestId("fact-agent")).toHaveTextContent("alpha");
    await waitFor(() => expect(screen.getByTestId("fact-cost")).toHaveTextContent("$0.05"));
    expect(props).not.toHaveTextContent("not tracked");
    expect(screen.getByTestId("fact-id")).toHaveTextContent("task-1".slice(0, 8));
  });

  it("shows a PR as a property when the header is not about it", async () => {
    mockApi();
    renderBody(taskFixture({ status: "blocked", pr_url: "https://example.test/pr/9", pr_number: 9 }));
    const pr = await screen.findByTestId("fact-pr");
    expect(within(pr).getByRole("link", { name: /PR #9/ })).toHaveAttribute("href", "https://example.test/pr/9");
  });

  it("a card in review shows its PR once, right under the state line", async () => {
    mockApi();
    renderBody(taskFixture({ status: "review", pr_url: "https://example.test/pr/10", pr_number: 10 }));
    expect(await screen.findByTestId("task-review-pr")).toHaveTextContent("PR #10");
    await screen.findByTestId("task-properties");
    expect(screen.queryByTestId("fact-pr")).toBeNull();
  });
});

describe("Summary tab (run record)", () => {
  it("renders the boxes with English labels and the content untranslated", async () => {
    mockApi({ runRecord: runRecordFixture() });
    renderBody(taskFixture({ status: "done", description: "Fix the sideways scroll in chat." }));
    await screen.findByTestId("run-record-summary");
    const summary = screen.getByRole("tabpanel");
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
  it("lists Change status, Copy link, Copy as Markdown, Save to Vault, the thread — and Delete last after a divider", async () => {
    mockApi();
    renderBody(taskFixture({ status: "done", assigned_agent_id: "agent-1" }));
    fireEvent.click(await screen.findByRole("button", { name: "More actions" }));
    const menu = await screen.findByRole("menu");
    const items = within(menu).getAllByRole("menuitem").map((el) => el.textContent);
    expect(items[0]).toContain("Change status");
    expect(items[1]).toContain("Copy link");
    expect(items[2]).toContain("Copy as Markdown");
    expect(items[3]).toContain("Save to Vault");
    expect(items[4]).toContain("Open alpha thread");
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

// ── Review follow-ups (wave 3a review) ───────────────────────────────────────

const LONG_REPORT = "## What was done\nA very long blocker report that must not push the buttons below the fold.";

describe("NEEDS YOU stays compact with an embedded approval", () => {
  const approval = approvalFixture({
    description: "alpha is blocked at Sample task",
    payload: {
      blocked_agent_name: "alpha",
      blocker_type: "technical_problem",
      description: "",
      question: "Card needs a flip to review — PATCH 403",
      blocker_comment: LONG_REPORT,
    },
  });

  it("shows the question once, clamped, and hides the long report until Show full", async () => {
    mockApi({ approvals: [approval] });
    renderBody(taskFixture({ status: "blocked", assigned_agent_id: "agent-1" }));
    const card = await screen.findByTestId("task-state-card");
    const embedded = await within(card).findByTestId("state-card-approval");
    // One reason, not three: no own quote above the approval, no repeated title line.
    expect(within(card).getAllByText(/Card needs a flip to review/)).toHaveLength(1);
    expect(within(card).queryByText(/alpha is blocked at/)).toBeNull();
    expect(within(embedded).getByTestId("approval-lead")).toHaveAttribute("data-clamped", "true");
    expect(within(card).queryByText(/very long blocker report/)).toBeNull();
    // Buttons directly under the reason.
    expect(within(embedded).getByRole("button", { name: /Unblock/ })).toBeInTheDocument();
    fireEvent.click(within(embedded).getByRole("button", { name: "Show full" }));
    expect(await within(card).findByText(/very long blocker report/)).toBeInTheDocument();
    expect(within(card).getAllByText(/alpha is blocked at/)).toHaveLength(1);
  });
});

describe("Reply focuses the comment field", () => {
  it("jumps to Comments and focuses the input", async () => {
    mockApi({ comments: [commentFixture({ comment_type: "blocker", content: "Which branch?" })] });
    renderBody(taskFixture({ status: "waiting" }));
    const card = await screen.findByTestId("task-state-card");
    fireEvent.click(await within(card).findByRole("button", { name: "Reply" }));
    await waitFor(() => expect(document.activeElement).toHaveAttribute("data-comment-input"));
  });

  it("also focuses when Comments is already the open tab", async () => {
    mockApi({ comments: [commentFixture({ comment_type: "blocker", content: "Which branch?" })] });
    renderBody(taskFixture({ status: "waiting" }), { tab: "comments" });
    const card = await screen.findByTestId("task-state-card");
    await screen.findByRole("textbox", { name: /comment/i });
    fireEvent.click(await within(card).findByRole("button", { name: "Reply" }));
    await waitFor(() => expect(document.activeElement).toHaveAttribute("data-comment-input"));
  });
});

describe("Delete asks first", () => {
  it("needs a second click before anything is deleted", async () => {
    mockApi();
    const del = vi.spyOn(api.tasks, "delete").mockResolvedValue(undefined as never);
    renderBody(taskFixture({ status: "done" }));
    fireEvent.click(await screen.findByRole("button", { name: "More actions" }));
    fireEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: /Delete/ }));
    expect(del).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole("menu")).getByRole("button", { name: "Delete task" }));
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
  });
});

describe("STEPS lists subtasks in pages", () => {
  it("shows the first 10 links, then all 25 after Show all", async () => {
    const subtasks = Array.from({ length: 25 }, (_, i) => ({ id: `sub-${i}`, title: `Subtask ${i}`, status: "done" }));
    mockApi({ subtasks });
    renderBody(taskFixture({ status: "done" }));
    const steps = await screen.findByTestId("summary-steps");
    await within(steps).findByText(/Subtasks: 25/);
    fireEvent.click(within(steps).getByRole("button", { name: /Show first 10/ }));
    expect(within(screen.getByTestId("summary-subtasks")).getAllByRole("link")).toHaveLength(10);
    fireEvent.click(within(steps).getByRole("button", { name: /Show all \(25\)/ }));
    expect(within(screen.getByTestId("summary-subtasks")).getAllByRole("link")).toHaveLength(25);
  });

  it("without subtasks the first toggle promises the last 10, not all", async () => {
    const schritte = Array.from({ length: 12 }, (_, i) => ({
      ts: `2026-09-18T08:${String(i).padStart(2, "0")}:00`,
      quelle: "status" as const,
      actor_label: "alpha",
      changed_by: "agent",
      text: `step ${i}`,
    }));
    mockApi({ runRecord: runRecordFixture({ schritte }) });
    renderBody(taskFixture({ status: "done" }));
    const steps = await screen.findByTestId("summary-steps");
    fireEvent.click(within(steps).getByRole("button", { name: /Show last 10/ }));
    expect(within(steps).getAllByText(/^step \d+$/)).toHaveLength(10);
    fireEvent.click(within(steps).getByRole("button", { name: /Show all \(12\)/ }));
    expect(within(steps).getAllByText(/^step \d+$/)).toHaveLength(12);
  });
});

describe("properties details", () => {
  it("leaves the cost out when nothing was billed (no $0.00, no 'not tracked')", async () => {
    const rr = runRecordFixture();
    mockApi({ runRecord: { ...rr, kosten: { ...rr.kosten, gesamt_usd: 0, je_anbieter: {} } } });
    renderBody(taskFixture({ status: "done" }));
    await screen.findByTestId("run-record-summary");
    expect(screen.queryByTestId("fact-cost")).toBeNull();
    expect(screen.getByTestId("task-properties")).not.toHaveTextContent("$");
  });

  it("an inbox task says not started even when it was dispatched before", async () => {
    mockApi();
    renderBody(taskFixture({ status: "inbox", dispatched_at: "2026-09-22T08:00:00Z" }));
    expect(await screen.findByTestId("task-state-line")).toHaveTextContent("not started");
  });

  it("translates the priority label", async () => {
    mockApi();
    renderBody(taskFixture({ status: "done", priority: "critical" }));
    expect(await screen.findByTestId("fact-priority")).toHaveTextContent("Critical");
  });
});

describe("thread view", () => {
  it("Back to summary goes to Summary even for a running task", async () => {
    mockApi();
    renderBody(taskFixture({ status: "in_progress" }));
    fireEvent.click(await screen.findByRole("button", { name: "More actions" }));
    fireEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: /thread/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Back to summary" }));
    // Wait for the tab strip to re-render — a synchronous query raced it on CI.
    expect(await screen.findByRole("tab", { name: "Summary" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("tab strip keyboard", () => {
  it("arrow keys move between tabs and only the active tab is in the tab order", async () => {
    mockApi();
    const onTabChange = vi.fn();
    renderBody(taskFixture({ status: "done" }), { onTabChange });
    const summary = await screen.findByRole("tab", { name: "Summary" });
    expect(summary).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tab", { name: "Comments" })).toHaveAttribute("tabindex", "-1");
    fireEvent.keyDown(summary, { key: "ArrowRight" });
    expect(onTabChange).toHaveBeenLastCalledWith("comments");
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("aria-labelledby", screen.getByRole("tab", { name: "Comments" }).id);
  });
});

describe("Copy as Markdown on Safari", () => {
  it("hands the clipboard a pending item inside the click, before the markdown has arrived", async () => {
    mockApi();
    let resolveMd: (s: string) => void = () => {};
    vi.spyOn(api.tasks, "runRecordMarkdown").mockImplementation(() => new Promise<string>((r) => { resolveMd = r; }));
    const write = vi.fn().mockResolvedValue(undefined);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { write, writeText }, configurable: true });
    class FakeClipboardItem { constructor(public items: Record<string, Promise<Blob>>) {} }
    vi.stubGlobal("ClipboardItem", FakeClipboardItem);
    try {
      renderBody(taskFixture({ status: "done" }));
      fireEvent.click(await screen.findByRole("button", { name: "More actions" }));
      fireEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: /Copy as Markdown/ }));
      // Synchronously inside the click — the fetch has not resolved yet.
      expect(write).toHaveBeenCalledTimes(1);
      expect(writeText).not.toHaveBeenCalled();
      resolveMd("# Run record");
      const item = write.mock.calls[0][0][0] as FakeClipboardItem;
      const blob = await item.items["text/plain"];
      expect(await blob.text()).toBe("# Run record");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("head-owned task (head launcher §8.2)", () => {
  it("a head run owns the card; the fleet run controls (Requeue) are hidden", async () => {
    mockApi();
    const { mkRun } = await import("@/lib/__tests__/headFixtures");
    vi.spyOn(api.heads, "list").mockResolvedValue({ runs: [mkRun({ state: "running" })] });
    vi.spyOn(api.heads, "pairs").mockRejectedValue(new Error("API 404: {}"));
    renderBody(taskFixture({ status: "in_progress", run_control: "manual_hold" }));
    await waitFor(() => expect(screen.getByTestId("task-state-card")).toHaveAttribute("data-kind", "head"));
    // The pair is a property (Summary tab — a running task opens on Comments).
    fireEvent.click(screen.getByRole("tab", { name: "Summary" }));
    expect(await screen.findByTestId("fact-head")).toHaveTextContent("omp · glm-local");
    expect(screen.queryByText("Requeue")).not.toBeInTheDocument();
  });

  it("a passed head (card in review, still held) shows no Requeue and no 'review blocked'", async () => {
    mockApi();
    const { mkRun } = await import("@/lib/__tests__/headFixtures");
    vi.spyOn(api.heads, "list").mockResolvedValue({
      runs: [mkRun({ state: "passed", pr_url: "https://github.com/o/r/pull/712", exited_at: "2026-09-23T12:00:00Z" })],
    });
    vi.spyOn(api.heads, "pairs").mockRejectedValue(new Error("API 404: {}"));
    renderBody(taskFixture({ status: "review", run_control: "manual_hold" }));
    await waitFor(() => expect(screen.getByTestId("task-state-card")).toHaveAttribute("data-head-state", "passed"));
    expect(screen.queryByText("Requeue")).not.toBeInTheDocument();
    expect(screen.queryByText(/Review blockiert/)).not.toBeInTheDocument();
  });

  it("without a head run the fleet controls stay", async () => {
    mockApi();
    vi.spyOn(api.heads, "list").mockRejectedValue(new Error('API 404: {"detail":{"code":"heads_disabled"}}'));
    renderBody(taskFixture({ status: "in_progress", run_control: "manual_hold" }));
    expect(await screen.findByText("Requeue")).toBeInTheDocument();
    expect(screen.queryByTestId("fact-head")).not.toBeInTheDocument();
  });
});

describe("header variant A (DESIGN.md K12)", () => {
  it("the context bar carries '‹ Tasks' and ⋯; the compact title only appears after scrolling", async () => {
    mockApi();
    const onBack = vi.fn();
    renderBody(taskFixture({ status: "done", title: "Only once please" }), { onBack });
    fireEvent.click(await screen.findByRole("button", { name: "Tasks" }));
    expect(onBack).toHaveBeenCalledTimes(1);
    // One copy of the title until it scrolls away — the bar does not repeat it.
    expect(screen.getAllByText("Only once please")).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Only once please" })).toBeInTheDocument();
  });

  it("⋯ Change status opens the Summary tab with the status menu open", async () => {
    mockApi();
    const onTabChange = vi.fn();
    renderBody(taskFixture({ status: "in_progress" }), { onTabChange });
    // A running task opens on Comments — the status row is not in view.
    expect(await screen.findByRole("tab", { name: "Comments" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    fireEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: /Change status/ }));
    expect(onTabChange).toHaveBeenCalledWith("summary");
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: "Done" })).toBeInTheDocument();
  });

  it("the Comments tab shows how many comments there are", async () => {
    mockApi({ comments: [commentFixture({ id: "c1" }), commentFixture({ id: "c2" })] });
    renderBody(taskFixture({ status: "done" }));
    const tab = await screen.findByRole("tab", { name: "Comments" });
    await waitFor(() => expect(tab).toHaveTextContent("Comments2"));
  });

  it("no tab select any more — one tab strip at every width", async () => {
    mockApi();
    renderBody(taskFixture({ status: "done" }));
    await screen.findByRole("tab", { name: "Summary" });
    expect(screen.queryByRole("combobox")).toBeNull();
  });
});

describe("markdown in the brief and the header", () => {
  it("the collapsed brief renders markdown — a heading element, no literal '##'", async () => {
    mockApi();
    renderBody(taskFixture({ status: "done", description: "## Goal\n\nFix the **sideways** scroll.\n\n- first step" }));
    const preview = await screen.findByTestId("summary-brief-preview");
    expect(within(preview).getByRole("heading", { name: "Goal" })).toBeInTheDocument();
    expect(within(preview).getByText("sideways").tagName).toBe("STRONG");
    expect(within(preview).getByRole("listitem")).toHaveTextContent("first step");
    expect(preview).not.toHaveTextContent("##");
    expect(preview).not.toHaveTextContent("**");
    // Still collapsed with the toggle to the full brief.
    expect(screen.getByRole("button", { name: /Show full brief/ })).toBeInTheDocument();
  });

  it("the result preview in the header shows no markdown syntax", async () => {
    mockApi({ comments: [commentFixture({ comment_type: "resolution", content: "## Result\n\n- [x] **Fixed** the scroll" })] });
    renderBody(taskFixture({ status: "done" }));
    const card = await screen.findByTestId("task-state-card");
    await within(card).findByText("Result Fixed the scroll");
    expect(card).not.toHaveTextContent("##");
    expect(card).not.toHaveTextContent("**");
  });
});
