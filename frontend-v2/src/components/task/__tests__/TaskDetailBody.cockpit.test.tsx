import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TaskDetailBody } from "../TaskDetailBody";
import type { Task, Agent } from "@/lib/types";

// Cockpit layout (09/2026): four tabs — Conversation · Changes · Results ·
// Technical — with a glance block (summary, needs-you, checkpoints) on top.
// Store mock follows TaskDetailBody.defaultTab.test.tsx.
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

function renderBody(task: Task, agents: Agent[] = [], wide = false) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TaskDetailBody task={task} agents={agents} boardId="board-1" onClose={() => {}} wide={wide} />
    </QueryClientProvider>
  );
}

describe("TaskDetailBody — cockpit", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
  });

  it("offers exactly four tabs and opens on Conversation", async () => {
    renderBody(mkTask());
    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((t) => t.textContent?.replace(/\d+$/, "").trim())).toEqual([
      "Conversation",
      "Changes",
      "Results",
      "Technical",
    ]);
    expect(screen.getByRole("tab", { name: /Conversation/ })).toHaveAttribute("aria-selected", "true");
  });

  it("shows the plain summary from the description's first paragraph", async () => {
    renderBody(mkTask({ description: "## Goal\nStop double dispatch.\n\n## Details\nLots of tech." }));
    expect(await screen.findByText("Stop double dispatch.")).toBeInTheDocument();
    expect(screen.queryByText("Lots of tech.")).not.toBeInTheDocument();
  });

  it("puts a 'Needs you' block on top for a waiting task", async () => {
    renderBody(mkTask({ status: "waiting" }));
    expect(await screen.findByText("Needs you")).toBeInTheDocument();
    expect(screen.getByText("Waiting for your answer")).toBeInTheDocument();
  });

  it("has no 'Needs you' block while the agent is working", async () => {
    renderBody(mkTask({ status: "in_progress" }));
    await screen.findAllByRole("tab");
    expect(screen.queryByText("Needs you")).not.toBeInTheDocument();
    expect(screen.getByText("Agent is working")).toBeInTheDocument();
  });

  it("keeps the technical sections collapsed inside the Technical tab", async () => {
    renderBody(mkTask({ intake_mode: "structured", request_kind: "code_change", description: "x" }));
    fireEvent.click(await screen.findByRole("tab", { name: /Technical/ }));
    const briefing = screen.getByRole("button", { name: /Briefing/ });
    expect(briefing).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("code_change")).not.toBeInTheDocument();
    fireEvent.click(briefing);
    expect(briefing).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("code_change")).toBeInTheDocument();
  });

  it("renders Changes and Results side by side in wide mode", async () => {
    renderBody(mkTask({ workspace_path: "/tmp/ws" }), [], true);
    const work = await screen.findByTestId("cockpit-work");
    expect(within(work).getByText(/Changes/)).toBeInTheDocument();
    // In wide mode the work column replaces the Changes/Results tabs.
    const tabs = screen.getAllByRole("tab").map((t) => t.textContent?.replace(/\d+$/, "").trim());
    expect(tabs).toEqual(["Conversation", "Technical"]);
  });
});
