import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Task } from "@/lib/types";
import { notify } from "@/lib/notify";
import { waitFor } from "@testing-library/react";
import { TaskRow } from "../TaskRow";

// A done row carried an orange paper-plane ("dispatch again?") as the
// loudest thing on the row, right next to "open task" — on phones always
// visible. Re-dispatching a finished task is rare and restarts an agent, so
// it moves behind the row's ⋯ menu and asks through the shared dialog.

function mkTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    title: "Ship the changelog",
    status: "done",
    priority: "medium",
    assigned_agent_id: "agent-1",
    checklist_total: 0,
    checklist_done: 0,
    last_activity_at: null,
    ...overrides,
  } as Task;
}

function renderRow(task: Task) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TaskRow task={task} agents={[]} boardId="board-1" onClick={vi.fn()} />
    </QueryClientProvider>
  );
}

describe("TaskRow — Dispatch again lives in the ⋯ menu", () => {
  let update: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    vi.restoreAllMocks();
    update = vi.spyOn(api.tasks, "update").mockResolvedValue({} as never);
  });

  it("a done row shows no direct dispatch button", () => {
    renderRow(mkTask());
    expect(screen.queryByTitle("Task already done — dispatch again?")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "More actions" })).toBeInTheDocument();
  });

  it("⋯ → Dispatch again asks first; confirming dispatches", async () => {
    renderRow(mkTask());
    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: /Dispatch again/ }));
    expect(update).not.toHaveBeenCalled();

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Ship the changelog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Yes, dispatch" }));
    expect(update).toHaveBeenCalledWith("board-1", "task-1", { status: "in_progress" });
  });

  it("an inbox row with an agent keeps its one-click dispatch", async () => {
    renderRow(mkTask({ status: "inbox" }));
    expect(screen.queryByRole("button", { name: "More actions" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByTitle("Dispatch task"));
    expect(update).toHaveBeenCalledWith("board-1", "task-1", { status: "in_progress" });
  });

  it("a failed re-dispatch says so and closes the dialog", async () => {
    update.mockRejectedValue(new Error("board locked"));
    const err = vi.spyOn(notify, "error").mockImplementation(() => undefined as never);
    renderRow(mkTask());
    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: /Dispatch again/ }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Yes, dispatch" }));
    await waitFor(() => expect(err).toHaveBeenCalledWith(expect.stringContaining("board locked")));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
