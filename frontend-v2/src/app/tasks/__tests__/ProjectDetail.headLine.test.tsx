/**
 * ProjectDetail's second line for a head-owned task (heads-sichtbar PR 3,
 * bauplan §4) — the desktop project view's OWN `headByTask.get(task.id)`
 * lookups (review fix round 5). `TaskRow.headLine.test.tsx` already covers
 * what the extracted row component does with a `headRun` prop; it never
 * exercises the lookup lines in `app/tasks/page.tsx` itself — one inside
 * `PhaseSection` (a phase's subtasks) and one inside `ProjectDetail` (the
 * standalone list below the phases). Both are real, independent copies of
 * the same `headByTask.get(task.id) ?? null` line and both get their own
 * case here.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ProjectDetail } from "../page";
import { mkRun } from "@/lib/__tests__/headFixtures";
import type { HeadRun } from "@/lib/heads";
import type { Project, Task } from "@/lib/types";

const mocks = vi.hoisted(() => ({ useHeadRuns: vi.fn() }));
vi.mock("@/components/heads/useHeadRuns", () => ({ useHeadRuns: mocks.useHeadRuns }));

function mockHeadRuns(byTask: Map<string, HeadRun>) {
  mocks.useHeadRuns.mockReturnValue({ runs: [...byTask.values()], byTask, archivedCount: 0, isLoading: false, error: null });
}

function mkTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1", board_id: "board-1", project_id: "proj-1", phase_id: null, parent_task_id: null,
    title: "Untitled", description: null, status: "in_progress",
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

function mkProject(overrides: Partial<Project> = {}): Project {
  return {
    id: "proj-1", board_id: "board-1", name: "Project X", description: null,
    project_type: "feature", status: "active", priority: "medium", plan_summary: null,
    progress_pct: 0, github_repo_url: null, github_repo_name: null, workspace_path: null,
    project_config: null, created_by: "user-1", started_at: null, completed_at: null,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  } as Project;
}

function renderDetail(tasks: Task[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ProjectDetail
        project={mkProject()}
        tasks={tasks}
        agents={[]}
        boardId="board-1"
        tags={[]}
        onTaskClick={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

describe("ProjectDetail — head second line (standalone task list)", () => {
  it("a standalone task with a head run gets a second line; an unrelated standalone task does not", async () => {
    const run = mkRun({ run_id: "r1", task_id: "task-standalone", harness: "omp", model: "GLM-5.3-Flash-EXL3", state: "running", started_at: "2026-09-23T10:00:00Z" });
    mockHeadRuns(new Map([["task-standalone", run]]));
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-09-23T10:12:00Z"));
    renderDetail([
      mkTask({ id: "task-standalone", title: "Standalone with a head" }),
      mkTask({ id: "task-other", title: "Unrelated standalone task" }),
    ]);

    await screen.findByText("Standalone with a head");
    const lines = screen.getAllByTestId("task-row-head-line");
    expect(lines).toHaveLength(1);
    expect(lines[0]).toHaveTextContent("Head · omp × GLM-5.3 · running for 12 min");
    const otherRow = screen.getByText("Unrelated standalone task").closest(".group");
    expect(otherRow?.querySelector('[data-testid="task-row-head-line"]')).toBeNull();
    vi.restoreAllMocks();
  });

  // Sabotage: a `headByTask.get(task.id)` in `ProjectDetail`'s standalone
  // loop keyed on the WRONG id (e.g. a hardcoded id, or the loop variable's
  // index) would show the line on every row or none — this is exactly the
  // regression the finding named ("the desktop lookup… has no test at all").
  // Confirmed red by hand: swapping `task.id` for `"task-other"` in the
  // standalone `<TaskRow headRun={headByTask.get(task.id) ?? null} …/>` call
  // makes BOTH rows below render the line (the run's own task_id and the
  // hardcoded lookup key collide), failing the second assertion here.
  it("a run for one standalone task never leaks onto another standalone task's row", async () => {
    mockHeadRuns(new Map([["task-standalone", mkRun({ task_id: "task-standalone", state: "running" })]]));
    renderDetail([mkTask({ id: "task-other", title: "Unrelated standalone task" })]);
    await screen.findByText("Unrelated standalone task");
    expect(screen.queryByTestId("task-row-head-line")).not.toBeInTheDocument();
  });
});

describe("ProjectDetail — head second line (a phase's subtasks)", () => {
  it("a phase subtask with a head run gets a second line; its sibling subtask does not", async () => {
    mockHeadRuns(new Map([["task-sub-1", mkRun({ task_id: "task-sub-1", state: "needs_you", model: "GLM-5.3-Flash-EXL3" })]]));
    renderDetail([
      mkTask({ id: "phase-1", title: "Phase One", status: "in_progress" }),
      mkTask({ id: "task-sub-1", title: "Subtask with a head", parent_task_id: "phase-1" }),
      mkTask({ id: "task-sub-2", title: "Subtask without a head", parent_task_id: "phase-1" }),
    ]);

    await screen.findByText("Subtask with a head");
    expect(await screen.findByTestId("task-row-head-line")).toHaveTextContent("Head · omp × GLM-5.3 · needs you");
    const sibling = screen.getByText("Subtask without a head").closest(".group");
    expect(sibling?.querySelector('[data-testid="task-row-head-line"]')).toBeNull();
  });
});
