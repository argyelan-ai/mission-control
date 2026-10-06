/**
 * TaskListColumn's second line for a head-owned task (heads-sichtbar PR 3,
 * bauplan §4): "Head · <pair> · <state/time>" only while a task has a head
 * run; a plain-agent task stays exactly as it was, one line. The list reads
 * `useHeadRuns().byTask` ONCE for the whole column — this test mocks that
 * hook directly rather than the network underneath it, so it stays about
 * the ROW, not about `useHeadRuns`'s own fetch (that hook has its own unit
 * test, `useHeadRuns.test.tsx`).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import TaskListColumn from "../TaskListColumn";
import { mkRun } from "@/lib/__tests__/headFixtures";
import type { HeadRun } from "@/lib/heads";
import type { Task } from "@/lib/types";

vi.mock("@/lib/api", () => ({ api: { tasks: {} } }));

// "Status" grouping, nothing pre-toggled: the "in_progress" lane every
// fixture task below lives in starts expanded (only done/aborted collapse
// by default) — same stub `TaskListColumn.doneOrder.test.tsx` uses to reach
// into its own group.
beforeEach(() => {
  const store: Record<string, string> = { "mc:tasks:view": JSON.stringify({ mode: "status", toggled: [] }) };
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store[k] ?? null,
    setItem: (k: string, v: string) => { store[k] = v; },
    removeItem: (k: string) => { delete store[k]; },
  });
});
afterEach(() => vi.unstubAllGlobals());

const mocks = vi.hoisted(() => ({ useHeadRuns: vi.fn() }));
vi.mock("@/components/heads/useHeadRuns", () => ({ useHeadRuns: mocks.useHeadRuns }));

function mockHeadRuns(byTask: Map<string, HeadRun>) {
  mocks.useHeadRuns.mockReturnValue({ runs: [...byTask.values()], byTask, archivedCount: 0, isLoading: false, error: null });
}

function mkTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1", board_id: "board-1", project_id: null, phase_id: null, parent_task_id: null,
    title: "kz: map drift must ignore test-only changes", description: null, status: "in_progress",
    priority: "medium", task_type: "story", assigned_agent_id: null, started_at: null, completed_at: null,
    due_at: null, sort_order: 0, is_auto_created: false, auto_reason: null, pipeline_id: null,
    pipeline_stage: null, owner_agent_id: null, delegation_type: null, branch_name: null,
    triggered_by_deliverable_id: null, target_url: null, acceptance_criteria: null, requires_auth: false,
    source_task_id: null, report_back_required: false, report_back_status: null, review_decision: null,
    review_decided_at: null, dispatch_phase: null, intake_mode: null, request_kind: null, desired_output: null,
    scope_out: null, risk_notes: null, reference_urls: null, reference_notes: null, approval_policy: null,
    autonomy_level: null, publish_allowed: null, needs_browser: null, e2e_test_required: false,
    use_separate_repo: false, repo_id: null, credential_consent: null, credential_id: null,
    planner_mode: "auto", run_control: null, dispatch_intent: "root", dispatch_attempt_id: null,
    spawn_session_key: null, spawn_run_id: null, workspace_port: null, workspace_path: null,
    checklist_total: 0, checklist_done: 0, dispatched_at: null, ack_at: null, last_activity_at: null,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", created_by_user_id: null,
    ...overrides,
  };
}

function renderList(tasks: Task[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TaskListColumn
        tasks={tasks}
        projects={[]}
        agents={[]}
        boardId="board-1"
        selectedTaskId={null}
        onSelectTask={vi.fn()}
        onOpenProject={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

describe("TaskListColumn — head second line", () => {
  it("a task with a running head gets a second line; a plain task does not", async () => {
    const run = mkRun({ run_id: "r1", task_id: "task-1", harness: "omp", model: "GLM-5.3-Flash-EXL3", state: "running", started_at: "2026-09-23T10:00:00Z" });
    mockHeadRuns(new Map([["task-1", run]]));
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-09-23T10:12:00Z"));
    renderList([mkTask({ id: "task-1" }), mkTask({ id: "task-2", title: "A plain task" })]);

    await screen.findByText("kz: map drift must ignore test-only changes");
    const lines = screen.getAllByTestId("task-row-head-line");
    expect(lines).toHaveLength(1);
    expect(lines[0]).toHaveTextContent("Head · omp × GLM-5.3 · running for 12 min");
    // the plain task's own row has no such line at all
    const plainRow = screen.getByText("A plain task").closest(".group");
    expect(plainRow?.querySelector('[data-testid="task-row-head-line"]')).toBeNull();
    vi.restoreAllMocks();
  });

  it("needs_you / passed / failed render their own state word on the second line", async () => {
    mockHeadRuns(
      new Map([
        ["task-1", mkRun({ task_id: "task-1", state: "needs_you", model: "GLM-5.3-Flash-EXL3" })],
      ]),
    );
    renderList([mkTask({ id: "task-1" })]);
    expect(await screen.findByTestId("task-row-head-line")).toHaveTextContent("Head · omp × GLM-5.3 · needs you");
  });

  // Sabotage: a `byTask` lookup keyed on the RUN's own task_id instead of the
  // ROW's task.id would silently show every row's line (or none), since every
  // run in this fixture carries the same task_id as the row under test would
  // otherwise read. Keying the lookup on the wrong id here (`get("task-2")`
  // instead of `get(task.id)`) is exactly the bug this guards against: the
  // first test above already fails red the moment that swap is made, because
  // "task-2"'s row would then also pick up "task-1"'s run.
  it("a task absent from byTask renders no second line, even once a head run exists for another task", async () => {
    mockHeadRuns(new Map([["task-1", mkRun({ task_id: "task-1", state: "running" })]]));
    renderList([mkTask({ id: "task-2", title: "Unrelated task" })]);
    await screen.findByText("Unrelated task");
    expect(screen.queryByTestId("task-row-head-line")).not.toBeInTheDocument();
  });

  it("20 rows cost a handful of column-level renders, never one hook call per row", async () => {
    // `useHeadRuns` runs once per render of the COLUMN itself (React's own
    // mount/effect settling calls it a few times — normal, and not what
    // this guards against). Were `ListRow` calling the hook itself instead
    // of reading the `headRun` prop, 20 rows would push this well past 20
    // calls; a column-level hook stays in the single digits regardless of
    // row count (sabotage: moving the `useHeadRuns()` call from
    // `TaskListColumn` into `ListRow` fails this red).
    mockHeadRuns(new Map());
    const twenty = Array.from({ length: 20 }, (_, i) => mkTask({ id: `task-${i}`, title: `Task ${i}` }));
    renderList(twenty);
    await screen.findByText("Task 19");
    expect(mocks.useHeadRuns.mock.calls.length).toBeLessThan(10);
  });
});
