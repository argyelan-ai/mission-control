import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axe from "axe-core";
import { ReviewTaskRow } from "../ReviewTaskRow";
import { mkRun } from "@/lib/__tests__/headFixtures";
import type { HeadRun } from "@/lib/heads";
import type { Task } from "@/lib/types";

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
    human_review_required: false,
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

function renderRow(task: Task, headRun: HeadRun | null = null, agent?: Parameters<typeof ReviewTaskRow>[0]["agent"]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ReviewTaskRow
        task={task}
        boardId="board-1"
        agent={agent}
        agentMap={{}}
        headRun={headRun}
        onDecision={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

describe("ReviewTaskRow — Human review badge", () => {
  it('shows the "Your review" badge when human_review_required is true', () => {
    renderRow(mkTask({ human_review_required: true }));
    expect(screen.getByText("Your review")).toBeInTheDocument();
  });

  it('hides the badge when human_review_required is false or unset', () => {
    renderRow(mkTask({ human_review_required: false }));
    expect(screen.queryByText("Your review")).not.toBeInTheDocument();
  });
});

describe("ReviewTaskRow — head chip (heads-sichtbar PR 3, bauplan §4)", () => {
  it("a head-finished card shows 'Head · <pair> · passed ›', linking to the head chat", () => {
    const run = mkRun({ run_id: "run-9", harness: "omp", model: "GLM-5.3-Flash-EXL3", state: "passed", pr_url: "https://github.com/o/r/pull/9" });
    renderRow(mkTask(), run);
    const chip = screen.getByTestId("review-row-head-chip");
    expect(chip).toHaveTextContent("Head · omp × GLM-5.3 · passed");
    expect(chip).toHaveAttribute("href", "/sessions?head=run-9");
  });

  it("without a head run, an assigned agent's own chip shows instead — unchanged default", () => {
    renderRow(mkTask({ assigned_agent_id: "a1" }), null, { id: "a1", name: "Beta", emoji: "🔧" } as never);
    expect(screen.getByText("Beta")).toBeInTheDocument();
    expect(screen.queryByTestId("review-row-head-chip")).not.toBeInTheDocument();
  });

  it("clicking the head chip does not also toggle the row's own expand/collapse", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    const run = mkRun({ state: "passed" });
    renderRow(mkTask(), run);
    const header = screen.getByTestId("review-row-header");
    // `aria-expanded` is synchronous and does not depend on the (unmocked)
    // comments query ever resolving — review fix round 5: the previous
    // version of this test asserted on "No comments yet.", text that only
    // renders once `api.tasks.comments.list` resolves; since that call was
    // never mocked here, the query never settled in jsdom and the assertion
    // passed vacuously whether or not the card actually expanded.
    expect(header).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(screen.getByTestId("review-row-head-chip"));
    // still collapsed — the chip's own stopPropagation held (sabotage: removing
    // it would expand the card on every chip click, a confusing side effect
    // of what reads as "open the head chat").
    expect(header).toHaveAttribute("aria-expanded", "false");
  });

  it("an ended head + a real assigned agent shows the AGENT's chip, not a stale head chip (review fix round 5)", () => {
    const run = mkRun({ state: "failed", reason: "no_progress", exited_at: "2026-09-23T12:00:00Z" });
    renderRow(mkTask({ assigned_agent_id: "a1" }), run, { id: "a1", name: "Beta", emoji: "🔧" } as never);
    expect(screen.getByText("Beta")).toBeInTheDocument();
    expect(screen.queryByTestId("review-row-head-chip")).not.toBeInTheDocument();
  });

  // Review fix round 6, finding 8: no screenshot script set `hasTouch` on
  // its Playwright context, so `pointer-coarse:` never matched and the
  // phone-only 44px chip layout showed up in no screenshot — removing the
  // class entirely left every test (this file included) green. A plain
  // class-presence check is cheap insurance against that regression class.
  it("carries the 44px coarse-pointer touch target (DESIGN.md K11)", () => {
    const run = mkRun({ state: "passed" });
    renderRow(mkTask(), run);
    expect(screen.getByTestId("review-row-head-chip")).toHaveClass("pointer-coarse:min-h-[44px]");
  });

  it("an ACTIVE head still wins over an assigned agent — the head owns the work right now", () => {
    const run = mkRun({ state: "running" });
    renderRow(mkTask({ assigned_agent_id: "a1" }), run, { id: "a1", name: "Beta", emoji: "🔧" } as never);
    expect(screen.getByTestId("review-row-head-chip")).toBeInTheDocument();
    expect(screen.queryByText("Beta")).not.toBeInTheDocument();
  });

  // Review fix round 6, finding 1 made the Enter/Space keydown stop being
  // swallowed, but round 7's independent review found the underlying
  // structure was still wrong: `review-row-header` was a `role="button"`
  // div wrapping the chip `<Link>`, which is a link nested inside a button
  // (axe rule `nested-interactive`, serious) — an event-only test can be
  // green while the DOM shape itself is still invalid. Round 7 replaced
  // the div with TaskRow.tsx's own pattern (a real `<button>` stretched via
  // `after:absolute after:inset-0`, the chip raised `relative z-[1]` above
  // it) and this asserts the actual DOM shape via axe-core instead of one
  // event. Confirmed red by hand against the pre-fix (`role="button"` div)
  // header: axe reported a `nested-interactive` violation on this node.
  it("the header has no nested-interactive accessibility violation (axe-core)", async () => {
    const run = mkRun({ state: "passed" });
    const { container } = renderRow(mkTask(), run);
    const results = await axe.run(container, {
      runOnly: { type: "rule", values: ["nested-interactive"] },
    });
    expect(results.violations).toEqual([]);
  });

  it("the head chip is still independently focusable and clickable above the stretched title button", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    const run = mkRun({ state: "passed" });
    renderRow(mkTask(), run);
    const header = screen.getByTestId("review-row-header");
    const chip = screen.getByTestId("review-row-head-chip");
    expect(header.tagName).toBe("BUTTON");
    expect(header.contains(chip)).toBe(false);
    await userEvent.click(chip);
    // still collapsed — the chip's own stopPropagation held, and it is a
    // sibling of the title button now, not nested inside it.
    expect(header).toHaveAttribute("aria-expanded", "false");
  });
});
