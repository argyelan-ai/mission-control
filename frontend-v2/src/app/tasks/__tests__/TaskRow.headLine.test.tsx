/**
 * TaskRow's second line for a head-owned task (heads-sichtbar PR 3, bauplan
 * §4) — the desktop twin of `TaskListColumn.headLine.test.tsx`. `headRun` is
 * a plain prop here (the ONE shared `useHeadRuns().byTask` lookup lives in
 * `ProjectDetail`/`PhaseSection`, `app/tasks/page.tsx`) — this test is only
 * about what the ROW does with it.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClientProvider, QueryClient } from "@tanstack/react-query";
import { TaskRow } from "../TaskRow";
import { mkRun } from "@/lib/__tests__/headFixtures";
import type { Task } from "@/lib/types";

function mkTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    title: "Ship the changelog",
    status: "in_progress",
    priority: "medium",
    assigned_agent_id: null,
    checklist_total: 0,
    checklist_done: 0,
    last_activity_at: null,
    ...overrides,
  } as Task;
}

function renderRow(task: Task, headRun: ReturnType<typeof mkRun> | null = null) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TaskRow task={task} agents={[]} boardId="board-1" headRun={headRun} onClick={vi.fn()} />
    </QueryClientProvider>,
  );
}

describe("TaskRow — head second line", () => {
  it("without a head run, the row has no second line (unchanged default)", () => {
    renderRow(mkTask());
    expect(screen.getByText("Ship the changelog")).toBeInTheDocument();
    expect(screen.queryByTestId("task-row-head-line")).not.toBeInTheDocument();
  });

  it("with a head run, a second line names the pair and state", () => {
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-09-23T10:12:00Z"));
    const run = mkRun({ harness: "omp", model: "GLM-5.3-Flash-EXL3", state: "running", started_at: "2026-09-23T10:00:00Z" });
    renderRow(mkTask(), run);
    expect(screen.getByTestId("task-row-head-line")).toHaveTextContent("Head · omp × GLM-5.3 · running for 12 min");
    vi.restoreAllMocks();
  });

  it("a failed run reads 'Head · <pair> · failed', no end-of-run age (K4 density)", () => {
    const run = mkRun({ harness: "omp", model: "GLM-5.3-Flash-EXL3", state: "failed", reason: "no_progress", exited_at: "2020-01-01T00:00:00Z" });
    renderRow(mkTask(), run);
    const line = screen.getByTestId("task-row-head-line");
    expect(line).toHaveTextContent("Head · omp × GLM-5.3 · failed");
    expect(line).not.toHaveTextContent("ago");
  });
});
