/**
 * Tasks list, status view — the Done group lists the newest completions first.
 *
 * The API returns tasks by sort_order/created_at, so the Done group opened
 * with months-old tasks and today's completions sat behind a dozen
 * "Show more" clicks.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import TaskListColumn from "../TaskListColumn";
import type { Task } from "@/lib/types";

vi.mock("@/lib/api", () => ({ api: { tasks: {} } }));

function mkTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    board_id: "board-1",
    project_id: null,
    phase_id: null,
    parent_task_id: null,
    title: "Ship the thing",
    description: null,
    status: "review",
    priority: "medium",
    task_type: "story",
    assigned_agent_id: null,
    started_at: null,
    completed_at: null,
    due_at: null,
    sort_order: 0,
    is_auto_created: false,
    auto_reason: null,
    pipeline_id: null,
    pipeline_stage: null,
    owner_agent_id: null,
    delegation_type: null,
    branch_name: null,
    triggered_by_deliverable_id: null,
    target_url: null,
    acceptance_criteria: null,
    requires_auth: false,
    source_task_id: null,
    report_back_required: false,
    report_back_status: null,
    review_decision: null,
    review_decided_at: null,
    dispatch_phase: null,
    intake_mode: null,
    request_kind: null,
    desired_output: null,
    scope_out: null,
    risk_notes: null,
    reference_urls: null,
    reference_notes: null,
    approval_policy: null,
    autonomy_level: null,
    publish_allowed: null,
    needs_browser: null,
    e2e_test_required: false,
    use_separate_repo: false,
    repo_id: null,
    credential_consent: null,
    credential_id: null,
    planner_mode: "auto",
    run_control: null,
    dispatch_intent: "root",
    dispatch_attempt_id: null,
    spawn_session_key: null,
    spawn_run_id: null,
    workspace_port: null,
    workspace_path: null,
    checklist_total: 0,
    checklist_done: 0,
    dispatched_at: null,
    ack_at: null,
    last_activity_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    created_by_user_id: null,
    ...overrides,
  };
}

describe("TaskListColumn Done ordering", () => {
  beforeEach(() => {
    const store: Record<string, string> = {
      "mc:tasks:view": JSON.stringify({ mode: "status", toggled: ["status:done"] }),
    };
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("sorts the Done group by completed_at, newest first", async () => {
    const tasks = [
      mkTask({ id: "a", title: "April task", status: "done", completed_at: "2026-04-21T10:00:00Z", sort_order: 0 }),
      mkTask({ id: "b", title: "Today task", status: "done", completed_at: "2026-09-22T10:00:00Z", sort_order: 1 }),
      mkTask({ id: "c", title: "June task", status: "done", completed_at: "2026-06-01T10:00:00Z", sort_order: 2 }),
    ];
    const qc = new QueryClient();
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
    await screen.findByText("Today task");
    const titles = ["Today task", "June task", "April task"].map((s) => screen.getByText(s));
    // DOCUMENT_POSITION_FOLLOWING (4): each title comes after the previous one.
    expect(titles[0].compareDocumentPosition(titles[1]) & 4).toBeTruthy();
    expect(titles[1].compareDocumentPosition(titles[2]) & 4).toBeTruthy();
  });
});
