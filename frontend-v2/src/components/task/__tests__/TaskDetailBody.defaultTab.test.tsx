import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TaskDetailBody } from "../TaskDetailBody";
import type { Task, Agent } from "@/lib/types";

// Task-Detail öffnet auf dem Gespräch, und darin auf den Kommentaren (Task
// #12: Comments vor Thread). Seit dem Cockpit (09/2026) ist Thread kein
// eigener Tab mehr, sondern ein Umschalter innerhalb von „Conversation".
// Store-Mock folgt demselben Muster wie TasksPage.test.tsx.
vi.mock("@/lib/store", () => ({
  useAppStore: (selector?: (s: { currentUser: null }) => unknown) =>
    selector ? selector({ currentUser: null }) : { currentUser: null },
}));

function mkTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    board_id: "board-1",
    project_id: null,
    phase_id: null,
    parent_task_id: null,
    title: "Sample task",
    description: null,
    status: "inbox",
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
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    created_by_user_id: null,
    ...overrides,
  };
}

function renderBody(task: Task, agents: Agent[] = []) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TaskDetailBody task={task} agents={agents} boardId="board-1" onClose={() => {}} />
    </QueryClientProvider>
  );
}

describe("TaskDetailBody — default tab", () => {
  beforeEach(() => {
    // Generic fetch stub — every query (checklist/events/git-info/hierarchy/
    // dependencies/comments/users/…) resolves to an empty list.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
  });

  it("opens on Conversation with Comments selected, not Thread", async () => {
    renderBody(mkTask());

    const conversationTab = await screen.findByRole("tab", { name: /Conversation/ });
    expect(conversationTab).toHaveAttribute("aria-selected", "true");

    expect(screen.getByRole("button", { name: "Comments" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Thread" })).toHaveAttribute("aria-pressed", "false");
  });

  it("still switches to Thread on click (switch behavior untouched)", async () => {
    renderBody(mkTask());

    const threadBtn = await screen.findByRole("button", { name: "Thread" });
    fireEvent.click(threadBtn);

    expect(threadBtn).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Comments" })).toHaveAttribute("aria-pressed", "false");
  });
});
