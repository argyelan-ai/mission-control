/**
 * Wave 2 of the task detail (DESIGN.md K8/K11): run control and the review
 * decision in the calm header language — Stop is quiet and asks inline,
 * Requeue / Release are the one primary unless the header already has one,
 * the review decision is one surface with one primary. Strings in EN + DE.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Task } from "@/lib/types";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";

const apiMock = vi.hoisted(() => ({
  agentsList: vi.fn(),
  tasksReview: vi.fn(),
  tasksPromote: vi.fn(),
  tasksStop: vi.fn(),
  tasksResume: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    agents: { list: (...a: unknown[]) => apiMock.agentsList(...a) },
    tasks: {
      review: (...a: unknown[]) => apiMock.tasksReview(...a),
      promote: (...a: unknown[]) => apiMock.tasksPromote(...a),
      stop: (...a: unknown[]) => apiMock.tasksStop(...a),
      resume: (...a: unknown[]) => apiMock.tasksResume(...a),
    },
  },
}));

import { TaskActions } from "../TaskActions";

function mkTask(o: Partial<Task> = {}): Task {
  return {
    id: "t1", board_id: "board-1", title: "Ship it", status: "in_progress",
    assigned_agent_id: null, human_review_required: false,
    review_decision: null, run_control: null, dispatch_phase: null,
    parent_task_id: null, dispatched_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
    priority: "medium",
    ...o,
  } as unknown as Task;
}

function renderActions(task: Task, primaryTaken = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TaskActions task={task} boardId="board-1" primaryTaken={primaryTaken} />
    </QueryClientProvider>,
  );
}

const isPrimary = (el: HTMLElement) => el.style.background.includes("--color-accent");

beforeEach(() => {
  Object.values(apiMock).forEach((m) => m.mockReset());
  apiMock.agentsList.mockResolvedValue([]);
  apiMock.tasksStop.mockResolvedValue({});
  apiMock.tasksResume.mockResolvedValue({});
  apiMock.tasksReview.mockResolvedValue({});
});

describe("run control", () => {
  it("Stop run is a quiet button that asks inline before anything is stopped", async () => {
    renderActions(mkTask());
    const stop = screen.getByRole("button", { name: "Stop run" });
    expect(isPrimary(stop)).toBe(false);
    fireEvent.click(stop);
    expect(apiMock.tasksStop).not.toHaveBeenCalled();
    expect(screen.getByText("Stop the run?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Stop run" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop run" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(apiMock.tasksStop).toHaveBeenCalledWith("board-1", "t1", "Manual stop"));
  });

  it("a held run explains itself in one line; Requeue is the primary action", () => {
    renderActions(mkTask({ run_control: "manual_hold" }));
    expect(screen.getByTestId("run-held")).toHaveTextContent("The run is on hold.");
    expect(isPrimary(screen.getByRole("button", { name: "Requeue" }))).toBe(true);
    expect(screen.queryByRole("button", { name: "Stop run" })).toBeNull();
  });

  it("Requeue steps down to quiet when the header already has a primary button", () => {
    renderActions(mkTask({ run_control: "stopped" }), true);
    expect(screen.getByTestId("run-held")).toHaveTextContent("The run is stopped.");
    expect(isPrimary(screen.getByRole("button", { name: "Requeue" }))).toBe(false);
  });
});

describe("the operator's review decision", () => {
  it("one surface, quick reasons fill the reason, Approve is the one primary", async () => {
    renderActions(mkTask({ status: "review", human_review_required: true, dispatched_at: null }));
    const approve = screen.getByRole("button", { name: "Approve" });
    expect(isPrimary(approve)).toBe(true);
    expect(isPrimary(screen.getByRole("button", { name: "Request changes" }))).toBe(false);
    expect(approve).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Tests passed" }));
    expect(screen.getByLabelText("Reason for your review decision")).toHaveValue("Tests passed");
    fireEvent.click(approve);
    await waitFor(() =>
      expect(apiMock.tasksReview).toHaveBeenCalledWith("board-1", "t1", { decision: "approve", comment: "Tests passed" }),
    );
  });

  it("a locked review says why in plain words, not a red box", () => {
    renderActions(mkTask({ status: "review", human_review_required: true, run_control: "stopped", dispatched_at: null }));
    expect(screen.getByText("Review is locked — the run is stopped.")).toBeInTheDocument();
  });
});

describe("strings in both languages", () => {
  it("every run-control and review string exists in EN and DE, and DE is German", () => {
    const enA = en.tasks.actions as Record<string, string>;
    const deA = de.tasks.actions as Record<string, string>;
    expect(Object.keys(deA).sort()).toEqual(Object.keys(enA).sort());
    expect(deA.stopRun).toBe("Lauf stoppen");
    expect(deA.requeue).toBe("Wieder einreihen");
    expect(deA.approve).toBe("Annehmen");
  });
});
