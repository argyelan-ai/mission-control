/**
 * TaskReferences — task detail "References" section (ADR-053). Renders the
 * task's own uploads plus files inherited from its project, badging the
 * inherited ones and hiding their delete action (deletes belong to the
 * project dialog, not the task view).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TaskReferences } from "../TaskReferences";
import { api } from "@/lib/api";
import type { ReferenceFile } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function mkRef(overrides: Partial<ReferenceFile> = {}): ReferenceFile {
  return {
    id: "ref-1",
    board_id: "board-1",
    task_id: "task-1",
    project_id: null,
    rel_path: "task-1/example.png",
    original_name: "example.png",
    mime: "image/png",
    size: 2048,
    note: null,
    created_at: "2026-01-01T00:00:00Z",
    abs_path: "/abs/example.png",
    ...overrides,
  };
}

describe("TaskReferences", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("(c) renders own and inherited references, badging inherited ones", async () => {
    vi.spyOn(api.references, "list").mockResolvedValue([
      mkRef({ id: "own-1", original_name: "own.png" }),
      mkRef({
        id: "inherited-1",
        original_name: "shared.pdf",
        project_id: "project-1",
        inherited: true,
      }),
    ]);

    renderWithQuery(<TaskReferences taskId="task-1" />);

    expect(await screen.findByText("own.png")).toBeInTheDocument();
    expect(await screen.findByText("shared.pdf")).toBeInTheDocument();
    expect(screen.getByText("from project")).toBeInTheDocument();

    // Only the own reference gets a delete action — inherited files are
    // read-only here (deleting them belongs to the project dialog).
    expect(screen.getAllByLabelText(/^Delete /)).toHaveLength(1);
    expect(screen.getByLabelText("Delete own.png")).toBeInTheDocument();
  });

  it("shows an upload affordance even with references already present", async () => {
    vi.spyOn(api.references, "list").mockResolvedValue([mkRef()]);

    renderWithQuery(<TaskReferences taskId="task-1" />);

    expect(await screen.findByText("example.png")).toBeInTheDocument();
    expect(screen.getByLabelText(/Add more/i)).toBeInTheDocument();
  });
});
