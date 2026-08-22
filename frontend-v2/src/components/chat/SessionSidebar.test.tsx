/**
 * SessionSidebar — Task B5 vitest.
 *
 * Coverage: grouping agents by their current task's project, the "Ad-hoc"
 * fallback group for agents with no project-bound task, the "Terminal" chip
 * for agents the caller reports as having no transcript, selection state,
 * and the rail ↔ sheet variant split (sheet collapses behind a toggle).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SessionSidebar } from "./SessionSidebar";
import type { Agent, Task, Project } from "@/lib/types";

function mkAgent(overrides: Partial<Agent> = {}): Agent {
  return {
    id: "agent-1",
    board_id: null,
    name: "Agent One",
    role: null,
    emoji: null,
    status: "idle",
    model: null,
    secret_id: null,
    is_board_lead: false,
    heartbeat_config: { interval: "5m", target: "boss" },
    skills: [],
    skill_filter: null,
    cli_plugins: null,
    cli_skills: null,
    mcp_servers: null,
    scopes: [],
    identity_md: null,
    soul_md: null,
    tools_md: null,
    heartbeat_md: null,
    rules_md: null,
    memory_md: null,
    last_seen_at: null,
    last_task_activity_at: null,
    current_task_id: null,
    context_tokens: 0,
    context_max: 200000,
    session_message_count: 0,
    total_tasks_completed: 0,
    total_compactions: 0,
    template_id: null,
    workspace_path: null,
    provision_status: "local",
    provisioned_at: null,
    archived_at: null,
    discord_channel_id: null,
    discord_channel_name: null,
    last_trigger_at: null,
    last_dispatch_error: null,
    run_state: "idle",
    operational_mode: "active",
    agent_runtime: "cli-bridge",
    runtime_id: null,
    pending_runtime_sync: false,
    harness: null,
    runtime_switchable: false,
    runtime_switch_blocked_reason: null,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
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
    status: "in_progress",
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

function mkProject(overrides: Partial<Project> = {}): Project {
  return {
    id: "project-1",
    board_id: "board-1",
    name: "Acme Project",
    description: null,
    project_type: "feature",
    status: "active",
    priority: "medium",
    plan_summary: null,
    progress_pct: 0,
    github_repo_url: null,
    github_repo_name: null,
    workspace_path: null,
    project_config: null,
    created_by: "user-1",
    started_at: null,
    completed_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("SessionSidebar", () => {
  it("groups an agent under its current task's project", () => {
    const project = mkProject({ id: "proj-1", name: "Sessions Chat View" });
    const task = mkTask({ id: "task-1", project_id: "proj-1", title: "Wire the sidebar" });
    const agent = mkAgent({ id: "agent-1", name: "Rex", current_task_id: "task-1" });

    render(
      <SessionSidebar
        agents={[agent]}
        tasks={[task]}
        projects={[project]}
        selectedId={null}
        onSelect={() => {}}
      />
    );

    expect(screen.getByText("Sessions Chat View")).toBeInTheDocument();
    expect(screen.getByText("Rex")).toBeInTheDocument();
    expect(screen.getByText("Wire the sidebar")).toBeInTheDocument();
    expect(screen.queryByText("Ad-hoc")).not.toBeInTheDocument();
  });

  it("falls back to the Ad-hoc group for an agent with no project-bound task", () => {
    const withTask = mkAgent({ id: "agent-1", name: "Rex", current_task_id: "task-1" });
    const noTask = mkAgent({ id: "agent-2", name: "Cody", current_task_id: null });
    const adhocTask = mkAgent({ id: "agent-3", name: "Sparky", current_task_id: "task-2" });
    const project = mkProject({ id: "proj-1", name: "Sessions Chat View" });
    const boundTask = mkTask({ id: "task-1", project_id: "proj-1", title: "Wire the sidebar" });
    const unboundTask = mkTask({ id: "task-2", project_id: null, title: "Quick fix" });

    render(
      <SessionSidebar
        agents={[withTask, noTask, adhocTask]}
        tasks={[boundTask, unboundTask]}
        projects={[project]}
        selectedId={null}
        onSelect={() => {}}
      />
    );

    const adhocHeading = screen.getByText("Ad-hoc");
    const adhocGroup = adhocHeading.parentElement as HTMLElement;
    expect(within(adhocGroup).getByText("Cody")).toBeInTheDocument();
    expect(within(adhocGroup).getByText("Sparky")).toBeInTheDocument();
    expect(within(adhocGroup).queryByText("Rex")).not.toBeInTheDocument();
  });

  it('shows a "Terminal" chip for agents the caller reports as having no transcript', () => {
    const hermes = mkAgent({ id: "hermes-1", name: "Hermes", agent_runtime: "host" });
    const cody = mkAgent({ id: "agent-2", name: "Cody" });

    render(
      <SessionSidebar
        agents={[hermes, cody]}
        tasks={[]}
        projects={[]}
        selectedId={null}
        onSelect={() => {}}
        hasTranscript={(id) => id !== "hermes-1"}
      />
    );

    const hermesRow = screen.getByText("Hermes").closest('[role="option"]') as HTMLElement;
    const codyRow = screen.getByText("Cody").closest('[role="option"]') as HTMLElement;
    expect(within(hermesRow).getByText("Terminal")).toBeInTheDocument();
    expect(within(codyRow).queryByText("Terminal")).not.toBeInTheDocument();
  });

  it("does not show the Terminal chip for anyone when hasTranscript is omitted", () => {
    const agent = mkAgent({ id: "agent-1", name: "Rex" });
    render(
      <SessionSidebar agents={[agent]} tasks={[]} projects={[]} selectedId={null} onSelect={() => {}} />
    );
    expect(screen.queryByText("Terminal")).not.toBeInTheDocument();
  });

  it("marks the selected agent's row and fires onSelect with its id on click", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const a1 = mkAgent({ id: "agent-1", name: "Rex" });
    const a2 = mkAgent({ id: "agent-2", name: "Cody" });

    render(
      <SessionSidebar
        agents={[a1, a2]}
        tasks={[]}
        projects={[]}
        selectedId="agent-1"
        onSelect={onSelect}
      />
    );

    const rexRow = screen.getByText("Rex").closest('[role="option"]') as HTMLElement;
    expect(rexRow).toHaveAttribute("aria-selected", "true");

    await user.click(screen.getByText("Cody"));
    expect(onSelect).toHaveBeenCalledWith("agent-2");
  });

  it("renders the full list inline for the rail variant", () => {
    const agent = mkAgent({ id: "agent-1", name: "Rex" });
    render(
      <SessionSidebar
        agents={[agent]}
        tasks={[]}
        projects={[]}
        selectedId={null}
        onSelect={() => {}}
        variant="rail"
      />
    );
    expect(screen.getByText("Rex")).toBeInTheDocument();
  });

  // ── Mobile stack screen (variant="list") ──────────────────────────────────
  describe("list variant (mobile stack screen 1)", () => {
    function renderList(props: Partial<React.ComponentProps<typeof SessionSidebar>> = {}) {
      const a1 = mkAgent({ id: "agent-1", name: "Rex", current_task_id: "task-1" });
      const a2 = mkAgent({ id: "agent-2", name: "Cody" });
      return render(
        <SessionSidebar
          agents={[a1, a2]}
          tasks={[mkTask({ id: "task-1", title: "Login reparieren" })]}
          projects={[]}
          selectedId={null}
          onSelect={() => {}}
          variant="list"
          {...props}
        />
      );
    }

    it("shows every session immediately — no dropdown to open first", () => {
      renderList();
      expect(screen.getByText("Rex")).toBeInTheDocument();
      expect(screen.getByText("Cody")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Session wählen/i })).not.toBeInTheDocument();
    });

    it("keeps the task subtitle so a row says what the session is about", () => {
      renderList();
      expect(screen.getByText("Login reparieren")).toBeInTheDocument();
    });

    it("gives rows a touch-sized height (they open a screen, not a dropdown)", () => {
      renderList();
      const row = screen.getByText("Rex").closest('[role="option"]') as HTMLElement;
      expect(row.className).toContain("min-h-[52px]");
    });

    it("selecting a row reports the agent id", async () => {
      const onSelect = vi.fn();
      const user = userEvent.setup();
      renderList({ onSelect });
      await user.click(screen.getByText("Cody"));
      expect(onSelect).toHaveBeenCalledWith("agent-2");
    });
  });

  it("collapses the sheet variant behind a toggle and opens it on click", async () => {
    const user = userEvent.setup();
    const agent = mkAgent({ id: "agent-1", name: "Rex" });
    render(
      <SessionSidebar
        agents={[agent]}
        tasks={[]}
        projects={[]}
        selectedId={null}
        onSelect={() => {}}
        variant="sheet"
      />
    );

    expect(screen.queryByText("Rex")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Session wählen/i }));
    expect(screen.getByText("Rex")).toBeInTheDocument();
  });

  it("closes the sheet again after selecting a row", async () => {
    const user = userEvent.setup();
    const a1 = mkAgent({ id: "agent-1", name: "Rex" });
    const a2 = mkAgent({ id: "agent-2", name: "Cody" });
    render(
      <SessionSidebar
        agents={[a1, a2]}
        tasks={[]}
        projects={[]}
        selectedId={null}
        onSelect={() => {}}
        variant="sheet"
      />
    );

    await user.click(screen.getByRole("button", { name: /Session wählen/i }));
    await user.click(screen.getByText("Cody"));
    expect(screen.queryByText("Rex")).not.toBeInTheDocument();
  });

  // ── Rail collapse (operator addition on top of B5) ────────────────────────
  describe("rail collapse", () => {
    it("shows no collapse chevron when onToggleCollapse is omitted (backward compatible)", () => {
      const agent = mkAgent({ id: "agent-1", name: "Rex" });
      render(
        <SessionSidebar
          agents={[agent]}
          tasks={[]}
          projects={[]}
          selectedId={null}
          onSelect={() => {}}
          variant="rail"
        />
      );
      expect(screen.queryByRole("button", { name: /einklappen|ausklappen/i })).not.toBeInTheDocument();
      expect(screen.getByText("Rex")).toBeInTheDocument();
    });

    it("collapsed=true renders a slim icon-only strip, no group labels or task titles", () => {
      const agent = mkAgent({ id: "agent-1", name: "Rex", current_task_id: "task-1" });
      render(
        <SessionSidebar
          agents={[agent]}
          tasks={[{ id: "task-1", title: "Fix the bug", project_id: null } as unknown as Task]}
          projects={[]}
          selectedId="agent-1"
          onSelect={() => {}}
          variant="rail"
          collapsed
          onToggleCollapse={() => {}}
        />
      );
      // Icon-only: the agent's name/task title text is gone, but the row is
      // still selectable (title attribute carries the name for a11y/hover).
      expect(screen.queryByText("Rex")).not.toBeInTheDocument();
      expect(screen.queryByText("Fix the bug")).not.toBeInTheDocument();
      expect(screen.queryByText("Ad-hoc")).not.toBeInTheDocument();
      const row = screen.getByRole("option", { name: "Rex" });
      expect(row).toHaveAttribute("aria-selected", "true");
    });

    it("never renders the open-rail's Terminal chip when collapsed, even for a no-transcript agent — the info folds into the title instead", () => {
      const hermes = mkAgent({ id: "hermes-1", name: "Hermes" });
      const cody = mkAgent({ id: "cody-1", name: "Cody" });
      render(
        <SessionSidebar
          agents={[hermes, cody]}
          tasks={[]}
          projects={[]}
          selectedId={null}
          onSelect={() => {}}
          variant="rail"
          hasTranscript={(id) => id !== "hermes-1"}
          collapsed
          onToggleCollapse={() => {}}
        />
      );
      // No visible "Terminal" text anywhere — icon-only, full stop.
      expect(screen.queryByText("Terminal")).not.toBeInTheDocument();
      // The no-transcript info still reaches the user, via the title
      // attribute (hover/a11y name) rather than a chip.
      expect(screen.getByRole("option", { name: "Hermes — nur Terminal" })).toBeInTheDocument();
      expect(screen.getByRole("option", { name: "Cody" })).toBeInTheDocument();
    });

    it("clicking the collapse chevron (open rail) calls onToggleCollapse", async () => {
      const onToggleCollapse = vi.fn();
      const user = userEvent.setup();
      render(
        <SessionSidebar
          agents={[mkAgent({ id: "agent-1", name: "Rex" })]}
          tasks={[]}
          projects={[]}
          selectedId={null}
          onSelect={() => {}}
          variant="rail"
          collapsed={false}
          onToggleCollapse={onToggleCollapse}
        />
      );
      await user.click(screen.getByRole("button", { name: "Seitenleiste einklappen" }));
      expect(onToggleCollapse).toHaveBeenCalledTimes(1);
    });

    it("clicking the expand chevron (collapsed rail) calls onToggleCollapse", async () => {
      const onToggleCollapse = vi.fn();
      const user = userEvent.setup();
      render(
        <SessionSidebar
          agents={[mkAgent({ id: "agent-1", name: "Rex" })]}
          tasks={[]}
          projects={[]}
          selectedId={null}
          onSelect={() => {}}
          variant="rail"
          collapsed
          onToggleCollapse={onToggleCollapse}
        />
      );
      await user.click(screen.getByRole("button", { name: "Seitenleiste ausklappen" }));
      expect(onToggleCollapse).toHaveBeenCalledTimes(1);
    });

    it("selecting an agent from the collapsed strip still calls onSelect", async () => {
      const onSelect = vi.fn();
      const user = userEvent.setup();
      render(
        <SessionSidebar
          agents={[mkAgent({ id: "agent-1", name: "Rex" })]}
          tasks={[]}
          projects={[]}
          selectedId={null}
          onSelect={onSelect}
          variant="rail"
          collapsed
          onToggleCollapse={() => {}}
        />
      );
      await user.click(screen.getByRole("option", { name: "Rex" }));
      expect(onSelect).toHaveBeenCalledWith("agent-1");
    });

    it("collapsed has no effect on the sheet variant", () => {
      const agent = mkAgent({ id: "agent-1", name: "Rex" });
      render(
        <SessionSidebar
          agents={[agent]}
          tasks={[]}
          projects={[]}
          selectedId={null}
          onSelect={() => {}}
          variant="sheet"
          collapsed
          onToggleCollapse={() => {}}
        />
      );
      // Sheet still renders its own collapsed-by-default toggle, unaffected
      // by the rail-only `collapsed` prop.
      expect(screen.getByRole("button", { name: /Session wählen/i })).toBeInTheDocument();
    });
  });
});

// ── Gruppen-Sektion (ADR-075) ───────────────────────────────────────────────
// Die vier Gruppen-Props sind optional: ohne sie muss die Sidebar exakt so
// rendern wie vorher — genau das prüft der erste Test hier.
function mkGroup(overrides: Partial<import("@/lib/groupTypes").GroupSummary> = {}) {
  return {
    id: "grp-1",
    thread_id: "thr-1",
    name: "Spark-Runde",
    goal: "DFlash2 bewerten",
    status: "idle" as const,
    lifecycle: "one_shot" as const,
    member_count: 2,
    rounds_completed: 0,
    current_round_no: 0,
    max_rounds: 3,
    created_at: "2026-08-20T10:00:00Z",
    member_avatars: [
      { id: "a1", emoji: null, name: "Alpha" },
      { id: "a2", emoji: null, name: "Beta" },
    ],
    last_message: null,
    ...overrides,
  };
}

describe("SessionSidebar — Gruppen-Sektion", () => {
  it("renders no group section at all when the caller passes no group props", () => {
    render(
      <SessionSidebar agents={[mkAgent()]} tasks={[]} projects={[]} selectedId={null} onSelect={() => {}} />
    );
    expect(screen.queryByText("Groups")).not.toBeInTheDocument();
  });

  it("puts the group section above the agent list", () => {
    render(
      <SessionSidebar
        agents={[mkAgent({ name: "Agent One" })]}
        tasks={[]}
        projects={[]}
        selectedId={null}
        onSelect={() => {}}
        groups={[mkGroup()]}
        onSelectGroup={() => {}}
      />
    );
    const listbox = screen.getByRole("listbox", { name: "Sessions" });
    const text = listbox.textContent ?? "";
    expect(text.indexOf("Groups")).toBeGreaterThanOrEqual(0);
    expect(text.indexOf("Groups")).toBeLessThan(text.indexOf("Agent One"));
  });

  it("sorts a waiting group above a running one", () => {
    render(
      <SessionSidebar
        agents={[]}
        tasks={[]}
        projects={[]}
        selectedId={null}
        onSelect={() => {}}
        groups={[
          mkGroup({ id: "running", name: "Läuft", status: "running", current_round_no: 2 }),
          mkGroup({ id: "waiting", name: "Wartet", status: "waiting_gate" }),
        ]}
        onSelectGroup={() => {}}
      />
    );
    const text = screen.getByRole("listbox", { name: "Sessions" }).textContent ?? "";
    expect(text.indexOf("Wartet")).toBeLessThan(text.indexOf("Läuft"));
  });

  it("selects a group by click and offers the create button", async () => {
    const onSelectGroup = vi.fn();
    const onCreateGroup = vi.fn();
    render(
      <SessionSidebar
        agents={[]}
        tasks={[]}
        projects={[]}
        selectedId={null}
        onSelect={() => {}}
        groups={[mkGroup()]}
        onSelectGroup={onSelectGroup}
        onCreateGroup={onCreateGroup}
      />
    );
    await userEvent.click(screen.getByRole("option", { name: /Spark-Runde/ }));
    expect(onSelectGroup).toHaveBeenCalledWith("grp-1");

    await userEvent.click(screen.getByRole("button", { name: "New group" }));
    expect(onCreateGroup).toHaveBeenCalled();
  });

  it("explains the empty state instead of showing a bare header", () => {
    render(
      <SessionSidebar
        agents={[]}
        tasks={[]}
        projects={[]}
        selectedId={null}
        onSelect={() => {}}
        groups={[]}
        onSelectGroup={() => {}}
      />
    );
    expect(screen.getByText("No groups yet — several agents, one goal.")).toBeInTheDocument();
  });

  it("keeps a waiting group reachable in the collapsed rail", () => {
    render(
      <SessionSidebar
        agents={[]}
        tasks={[]}
        projects={[]}
        selectedId={null}
        onSelect={() => {}}
        groups={[mkGroup({ status: "waiting_gate" })]}
        onSelectGroup={() => {}}
        collapsed
        onToggleCollapse={() => {}}
      />
    );
    expect(screen.getByRole("button", { name: "Spark-Runde" })).toBeInTheDocument();
  });
});
