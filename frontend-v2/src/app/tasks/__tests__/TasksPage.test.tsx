import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Task } from "@/lib/types";

// /tasks?taskId=<uuid> deep link (produced by LoopDetailPanel's "View task"
// and VoicePreviewSheet) must be consumed on load: select + open the task,
// expand whatever collapsed group hides it, then strip the param from the URL.
const nav = vi.hoisted(() => ({
  replace: vi.fn(),
  push: vi.fn(),
  searchParamsString: "",
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: nav.push }),
  usePathname: () => "/tasks",
  useSearchParams: () => new URLSearchParams(nav.searchParamsString),
}));

// Same convention as LoopsPage.test.tsx / HostsSection.test.tsx: a plain
// selector-compatible store mock dodges the real zustand persist middleware.
const mockAppState = vi.hoisted(() => ({
  state: {
    activeBoardId: "board-1" as string | null,
    sidebarCollapsed: false,
    commandPaletteOpen: false,
    boards: [] as unknown[],
    boardGroups: [] as unknown[],
    currentUser: { id: "user-1", email: "mark@example.com", name: "Mark", role: "admin" } as {
      id: string;
      email: string;
      name: string;
      role: string;
    } | null,
    setActiveBoardId: (id: string | null) => {
      mockAppState.state.activeBoardId = id;
    },
    toggleSidebar: () => {},
    setCommandPaletteOpen: (open: boolean) => {
      mockAppState.state.commandPaletteOpen = open;
    },
    setBoards: (boards: unknown[]) => {
      mockAppState.state.boards = boards;
    },
    setBoardGroups: (boardGroups: unknown[]) => {
      mockAppState.state.boardGroups = boardGroups;
    },
    setCurrentUser: (user: typeof mockAppState.state.currentUser) => {
      mockAppState.state.currentUser = user;
    },
  },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: Object.assign(
    (selector?: (s: typeof mockAppState.state) => unknown) =>
      selector ? selector(mockAppState.state) : mockAppState.state,
    { setState: (partial: Partial<typeof mockAppState.state>) => Object.assign(mockAppState.state, partial) }
  ),
}));

import TasksPage from "../page";

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TasksPage />
    </QueryClientProvider>
  );
}

function mkTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    board_id: "board-1",
    project_id: null,
    phase_id: null,
    parent_task_id: null,
    title: "Fix the deep link",
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

describe("TasksPage — /tasks?taskId=<uuid> deep link", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    nav.replace.mockClear();
    nav.push.mockClear();
    nav.searchParamsString = "";
    mockAppState.state.activeBoardId = "board-1";

    const store: Record<string, string> = { mc_auth_token: "tok" };
    Object.defineProperty(globalThis, "localStorage", {
      value: {
        getItem: (k: string) => store[k] ?? null,
        setItem: (k: string, v: string) => {
          store[k] = v;
        },
        removeItem: (k: string) => {
          delete store[k];
        },
        clear: () => undefined,
      },
      configurable: true,
      writable: true,
    });

    // Generic fetch stub for any unmocked api call (TaskDetailBody's checklist/
    // events/git-info/hierarchy/dependencies/users queries, Sidebar badges, etc.)
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    vi.spyOn(api.agents, "list").mockResolvedValue([]);
    vi.spyOn(api.projects, "list").mockResolvedValue([]);
  });

  it("(a) opens the task detail for a task referenced by ?taskId=", async () => {
    const target = mkTask({ id: "task-target", title: "Consume the deep link" });
    vi.spyOn(api.tasks, "list").mockResolvedValue([
      target,
      mkTask({ id: "task-other", title: "Unrelated task" }),
    ]);
    nav.searchParamsString = "taskId=task-target";

    renderPage();

    // Detail pane opens with the target task's title as the header.
    expect(await screen.findByRole("heading", { name: "Consume the deep link" })).toBeInTheDocument();

    // The row is visible in the list too — proves the (default-collapsed)
    // Ad-hoc group was expanded, not just that selectedTaskId got set blindly.
    expect(
      await screen.findByRole("button", { name: "Open task: Consume the deep link" })
    ).toBeInTheDocument();
  });

  it("(b) ignores an unknown taskId — no crash, no detail pane", async () => {
    vi.spyOn(api.tasks, "list").mockResolvedValue([mkTask({ id: "task-1", title: "Some task" })]);
    nav.searchParamsString = "taskId=does-not-exist";

    renderPage();

    // Empty state stays put; nothing crashed trying to open a missing task.
    expect(await screen.findByText("Select a task from the list")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /./ })).not.toHaveTextContent("does-not-exist");
  });

  it("(c) strips the taskId param from the URL via router.replace", async () => {
    vi.spyOn(api.tasks, "list").mockResolvedValue([mkTask({ id: "task-1", title: "Some task" })]);
    nav.searchParamsString = "taskId=task-1";

    renderPage();

    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/tasks", { scroll: false }));
  });
});
