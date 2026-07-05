import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { E2ETab } from "../E2ETab";
import { TaskDetailBody } from "../TaskDetailBody";
import { api } from "@/lib/api";
import type { Task, TaskComment, TaskDeliverable } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

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

function mkComment(overrides: Partial<TaskComment> = {}): TaskComment {
  return {
    id: "comment-1",
    task_id: "task-1",
    author_type: "agent",
    author_agent_id: "tester-1",
    author_agent_name: "Tester",
    author_agent_emoji: "🧪",
    comment_type: "progress",
    content: "Working on it",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function mkDeliverable(overrides: Partial<TaskDeliverable> = {}): TaskDeliverable {
  return {
    id: "deliverable-1",
    task_id: "task-1",
    agent_id: "tester-1",
    deliverable_type: "screenshot",
    title: "test-desktop.png",
    path: "task-1/test-desktop.png",
    description: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  } as TaskDeliverable;
}

describe("E2ETab", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("shows a green PASS badge and renders the flow protocol for the newest TEST_PASS comment", async () => {
    vi.spyOn(api.tasks.comments, "list").mockResolvedValue([
      mkComment({ id: "c1", content: "**Result:** TEST_FAIL\n**Problem:** old failure", created_at: "2026-01-01T00:00:00Z" }),
      mkComment({ id: "c2", content: "**Result:** TEST_PASS\n**Summary:** all flows work", created_at: "2026-01-02T00:00:00Z" }),
    ]);
    vi.spyOn(api.tasks.deliverables, "list").mockResolvedValue([]);

    renderWithQuery(<E2ETab task={mkTask({ e2e_test_required: true })} boardId="board-1" />);

    expect(await screen.findByText("E2E passed")).toBeInTheDocument();
    expect(screen.getByText(/all flows work/)).toBeInTheDocument();
    // The stale FAIL comment must not win over the newer PASS.
    expect(screen.queryByText("E2E failed")).not.toBeInTheDocument();
  });

  it("shows a red FAIL badge for a TEST_FAIL comment", async () => {
    vi.spyOn(api.tasks.comments, "list").mockResolvedValue([
      mkComment({ content: "**Result:** TEST_FAIL\n**Problem:** button does nothing" }),
    ]);
    vi.spyOn(api.tasks.deliverables, "list").mockResolvedValue([]);

    renderWithQuery(<E2ETab task={mkTask({ e2e_test_required: true })} boardId="board-1" />);

    expect(await screen.findByText("E2E failed")).toBeInTheDocument();
    expect(screen.getByText(/button does nothing/)).toBeInTheDocument();
  });

  it("shows a neutral awaiting-test state with no result comment", async () => {
    vi.spyOn(api.tasks.comments, "list").mockResolvedValue([mkComment({ content: "just a status update" })]);
    vi.spyOn(api.tasks.deliverables, "list").mockResolvedValue([]);

    renderWithQuery(<E2ETab task={mkTask({ e2e_test_required: true, status: "in_progress" })} boardId="board-1" />);

    expect(await screen.findByText("Awaiting test")).toBeInTheDocument();
  });

  it("shows 'Test running' while the task is in user_test", async () => {
    vi.spyOn(api.tasks.comments, "list").mockResolvedValue([]);
    vi.spyOn(api.tasks.deliverables, "list").mockResolvedValue([]);

    renderWithQuery(<E2ETab task={mkTask({ e2e_test_required: true, status: "user_test" })} boardId="board-1" />);

    expect(await screen.findByText("Test running")).toBeInTheDocument();
  });

  it("renders a video element for a .webm deliverable and an empty state without one", async () => {
    // AuthVideo reads the auth token via localStorage — jsdom provides it,
    // but only once a window exists, so stub it defensively per-test.
    vi.stubGlobal("localStorage", {
      getItem: vi.fn().mockReturnValue("test-token"),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
    vi.spyOn(api.tasks.comments, "list").mockResolvedValue([]);
    vi.spyOn(api.tasks.deliverables, "list").mockResolvedValue([
      mkDeliverable({ id: "vid-1", deliverable_type: "file", title: "recording.webm", path: "task-1/recording.webm" }),
    ]);
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(["fake"], { type: "video/webm" })),
    }) as unknown as typeof fetch;

    const { container } = renderWithQuery(<E2ETab task={mkTask({ e2e_test_required: true })} boardId="board-1" />);

    expect(await screen.findByText("Recording")).toBeInTheDocument();
    await vi.waitFor(() => expect(container.querySelector("video")).toBeInTheDocument());
    expect(screen.queryByText("No recording yet")).not.toBeInTheDocument();
  });

  it("shows the empty recording state when there is no video deliverable", async () => {
    vi.spyOn(api.tasks.comments, "list").mockResolvedValue([]);
    vi.spyOn(api.tasks.deliverables, "list").mockResolvedValue([
      mkDeliverable({ id: "shot-1" }), // screenshot only, no video
    ]);

    renderWithQuery(<E2ETab task={mkTask({ e2e_test_required: true })} boardId="board-1" />);

    expect(await screen.findByText("No recording yet")).toBeInTheDocument();
  });
});

describe("TaskDetailBody — E2E tab visibility", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.tasks.checklist, "list").mockResolvedValue([]);
  });

  it("shows the E2E tab when task.e2e_test_required is set", async () => {
    vi.spyOn(api.tasks.comments, "list").mockResolvedValue([]);
    vi.spyOn(api.tasks.deliverables, "list").mockResolvedValue([]);
    vi.spyOn(api.tasks, "hierarchy").mockResolvedValue({
      parent: null,
      children: [],
      report_back: null,
      has_credentials: false,
      requester: null,
    });
    vi.spyOn(api.tasks, "dependencies").mockResolvedValue([]);
    vi.spyOn(api.projects, "list").mockResolvedValue([]);

    renderWithQuery(
      <TaskDetailBody
        task={mkTask({ e2e_test_required: true })}
        agents={[]}
        boardId="board-1"
        onClose={() => {}}
      />
    );

    expect(await screen.findByRole("tab", { name: "E2E" })).toBeInTheDocument();
  });

  it("hides the E2E tab when e2e is not required and no result comment exists", async () => {
    vi.spyOn(api.tasks.comments, "list").mockResolvedValue([mkComment({ content: "no marker here" })]);
    vi.spyOn(api.tasks.deliverables, "list").mockResolvedValue([]);
    vi.spyOn(api.tasks, "hierarchy").mockResolvedValue({
      parent: null,
      children: [],
      report_back: null,
      has_credentials: false,
      requester: null,
    });
    vi.spyOn(api.tasks, "dependencies").mockResolvedValue([]);
    vi.spyOn(api.projects, "list").mockResolvedValue([]);

    renderWithQuery(
      <TaskDetailBody
        task={mkTask({ e2e_test_required: false })}
        agents={[]}
        boardId="board-1"
        onClose={() => {}}
      />
    );

    await screen.findByRole("tab", { name: "Comments" });
    expect(screen.queryByRole("tab", { name: "E2E" })).not.toBeInTheDocument();
  });
});
