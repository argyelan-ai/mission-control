import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { WorkspaceTab } from "../WorkspaceTab";
import { TaskDetailBody } from "../TaskDetailBody";
import { api } from "@/lib/api";
import type { Task, FsEntry } from "@/lib/types";

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

function mkEntry(overrides: Partial<FsEntry> = {}): FsEntry {
  return {
    name: "file.txt",
    type: "file",
    size: 100,
    mime: "text/plain",
    mtime: 1700000000,
    is_directory: false,
    ...overrides,
  };
}

describe("WorkspaceTab", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // FilePreview fetches content with a Bearer token — mock the storage it
    // reads from and the network call, mirroring FilePreview.test.tsx.
    const storage = {
      getItem: () => "tok",
      setItem: () => undefined,
      removeItem: () => undefined,
      clear: () => undefined,
    };
    Object.defineProperty(globalThis, "localStorage", {
      value: storage, configurable: true, writable: true,
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("file contents", { status: 200 }));
  });

  it("renders directory entries from the workspace list response", async () => {
    vi.spyOn(api.tasks.workspace, "list").mockResolvedValue({
      exists: true,
      subpath: "",
      entries: [
        mkEntry({ name: "src", type: "directory", is_directory: true, size: 0, mime: null }),
        mkEntry({ name: "README.md" }),
      ],
    });

    renderWithQuery(<WorkspaceTab task={mkTask({ workspace_path: "/mc/agents/foo/workspace" })} boardId="board-1" />);

    expect(await screen.findByText("src")).toBeInTheDocument();
    expect(screen.getByText("README.md")).toBeInTheDocument();
  });

  it("navigates into a subfolder via breadcrumb click", async () => {
    const listSpy = vi.spyOn(api.tasks.workspace, "list").mockImplementation((_boardId, _taskId, subpath) => {
      if (!subpath) {
        return Promise.resolve({
          exists: true,
          subpath: "",
          entries: [mkEntry({ name: "src", type: "directory", is_directory: true, size: 0, mime: null })],
        });
      }
      return Promise.resolve({
        exists: true,
        subpath,
        entries: [mkEntry({ name: "index.ts" })],
      });
    });

    renderWithQuery(<WorkspaceTab task={mkTask({ workspace_path: "/mc/agents/foo/workspace" })} boardId="board-1" />);

    fireEvent.click(await screen.findByText("src"));

    expect(await screen.findByText("index.ts")).toBeInTheDocument();
    expect(listSpy).toHaveBeenCalledWith("board-1", "task-1", "src");

    // Breadcrumb root takes us back
    fireEvent.click(screen.getByText("workspace"));
    expect(await screen.findByText("src")).toBeInTheDocument();
  });

  it("shows a friendly empty state when the workspace folder no longer exists", async () => {
    vi.spyOn(api.tasks.workspace, "list").mockResolvedValue({
      exists: false,
      subpath: "",
      entries: [],
    });

    renderWithQuery(<WorkspaceTab task={mkTask({ workspace_path: "/mc/agents/foo/workspace" })} boardId="board-1" />);

    expect(await screen.findByText(/no longer exists/)).toBeInTheDocument();
    expect(screen.getByText("Deliverables")).toBeInTheDocument();
  });

  it("resets subpath and selected file when the task changes", async () => {
    vi.spyOn(api.tasks.workspace, "list").mockImplementation((_boardId, taskId, subpath) => {
      if (taskId === "task-1" && subpath === "src") {
        return Promise.resolve({
          exists: true,
          subpath: "src",
          entries: [mkEntry({ name: "index.ts" })],
        });
      }
      return Promise.resolve({
        exists: true,
        subpath: "",
        entries: [mkEntry({ name: "src", type: "directory", is_directory: true, size: 0, mime: null })],
      });
    });

    const { rerender } = renderWithQuery(
      <WorkspaceTab task={mkTask({ id: "task-1", workspace_path: "/mc/agents/foo/workspace" })} boardId="board-1" />,
    );

    fireEvent.click(await screen.findByText("src"));
    expect(await screen.findByText("index.ts")).toBeInTheDocument();

    // Same component instance, new task — must not keep showing task-1's subpath.
    rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <WorkspaceTab task={mkTask({ id: "task-2", workspace_path: "/mc/agents/bar/workspace" })} boardId="board-1" />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("src")).toBeInTheDocument();
    expect(screen.queryByText("index.ts")).not.toBeInTheDocument();
  });

  it("keeps the breadcrumb visible when a subfolder 404s, so the user can navigate back up", async () => {
    vi.spyOn(api.tasks.workspace, "list").mockImplementation((_boardId, _taskId, subpath) => {
      if (!subpath) {
        return Promise.resolve({
          exists: true,
          subpath: "",
          entries: [mkEntry({ name: "src", type: "directory", is_directory: true, size: 0, mime: null })],
        });
      }
      return Promise.reject(new Error("404"));
    });

    renderWithQuery(<WorkspaceTab task={mkTask({ workspace_path: "/mc/agents/foo/workspace" })} boardId="board-1" />);

    fireEvent.click(await screen.findByText("src"));

    expect(await screen.findByText("Failed to load workspace")).toBeInTheDocument();
    // Root breadcrumb entry stays clickable to escape the dead-end.
    expect(screen.getByText("workspace")).toBeInTheDocument();
  });

  it("gates auto-preview behind a button for files above the size threshold", async () => {
    vi.spyOn(api.tasks.workspace, "list").mockResolvedValue({
      exists: true,
      subpath: "",
      entries: [mkEntry({ name: "huge.log", size: 6 * 1024 * 1024 })],
    });

    renderWithQuery(<WorkspaceTab task={mkTask({ workspace_path: "/mc/agents/foo/workspace" })} boardId="board-1" />);

    fireEvent.click(await screen.findByText("huge.log"));

    expect(await screen.findByText(/too large to preview/)).toBeInTheDocument();
    expect(screen.getByText("Load preview anyway")).toBeInTheDocument();
  });

  it("loads the preview after clicking through the large-file gate", async () => {
    vi.spyOn(api.tasks.workspace, "list").mockResolvedValue({
      exists: true,
      subpath: "",
      entries: [mkEntry({ name: "huge.txt", size: 6 * 1024 * 1024 })],
    });

    renderWithQuery(<WorkspaceTab task={mkTask({ workspace_path: "/mc/agents/foo/workspace" })} boardId="board-1" />);

    fireEvent.click(await screen.findByText("huge.txt"));
    fireEvent.click(await screen.findByText("Load preview anyway"));

    expect(screen.queryByText(/too large to preview/)).not.toBeInTheDocument();
  });

  it("does not gate small files behind the size button", async () => {
    vi.spyOn(api.tasks.workspace, "list").mockResolvedValue({
      exists: true,
      subpath: "",
      entries: [mkEntry({ name: "small.txt", size: 100 })],
    });

    renderWithQuery(<WorkspaceTab task={mkTask({ workspace_path: "/mc/agents/foo/workspace" })} boardId="board-1" />);

    fireEvent.click(await screen.findByText("small.txt"));

    expect(screen.queryByText(/too large to preview/)).not.toBeInTheDocument();
  });
});

describe("TaskDetailBody — Workspace tab visibility", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.tasks.checklist, "list").mockResolvedValue([]);
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
  });

  it("shows the Workspace tab when task.workspace_path is set", async () => {
    renderWithQuery(
      <TaskDetailBody
        task={mkTask({ workspace_path: "/mc/agents/foo/workspace" })}
        agents={[]}
        boardId="board-1"
        onClose={() => {}}
      />
    );

    expect(await screen.findByRole("tab", { name: "Workspace" })).toBeInTheDocument();
  });

  it("hides the Workspace tab when task.workspace_path is null", async () => {
    renderWithQuery(
      <TaskDetailBody
        task={mkTask({ workspace_path: null })}
        agents={[]}
        boardId="board-1"
        onClose={() => {}}
      />
    );

    await screen.findByRole("tab", { name: "Comments" });
    expect(screen.queryByRole("tab", { name: "Workspace" })).not.toBeInTheDocument();
  });
});
