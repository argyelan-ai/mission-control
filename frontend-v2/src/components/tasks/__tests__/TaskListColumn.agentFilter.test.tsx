/**
 * Round-2 Q5 (t4 S-neu-4): the agent filter kept only `assigned_agent_id`,
 * so a card the agent had delivered into the operator's review (assignment
 * cleared by the hand-off) vanished from its list — "Beta" showed 1 of 3.
 * Setup copied from TaskListColumn.headLine.test.tsx:
 *
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
import type { Agent, Task } from "@/lib/types";
import { fireEvent } from "@testing-library/react";

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

function renderList(tasks: Task[], agents: Agent[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TaskListColumn
        tasks={tasks}
        projects={[]}
        agents={agents}
        boardId="board-1"
        selectedTaskId={null}
        onSelectTask={vi.fn()}
        onOpenProject={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

describe("TaskListColumn — agent filter keeps delivered cards", () => {
  it("lists the agent's card in the operator's review next to its running one", () => {
    mockHeadRuns(new Map());
    const agents = [
      { id: "beta", name: "Beta", emoji: null } as unknown as Agent,
      { id: "other", name: "Other", emoji: null } as unknown as Agent,
    ];
    renderList([
      mkTask({ id: "t-run", title: "Still building", status: "in_progress", assigned_agent_id: "beta" }),
      mkTask({ id: "t-review", title: "Delivered page", status: "review", assigned_agent_id: null, delivered_by_agent_id: "beta" }),
      mkTask({ id: "t-foreign", title: "Someone else's", status: "review", assigned_agent_id: null, delivered_by_agent_id: "other" }),
    ], agents);

    fireEvent.change(screen.getByRole("combobox", { name: "Filter by agent" }), { target: { value: "beta" } });

    expect(screen.getByText("Still building")).toBeInTheDocument();
    expect(screen.getByText("Delivered page")).toBeInTheDocument();
    expect(screen.queryByText("Someone else's")).toBeNull();
  });
});
