/**
 * ProjectReferencesDialog — reference files shared with every task in a
 * project (ADR-053). Opened from the project group header's paperclip icon.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ProjectReferencesDialog } from "../ProjectReferencesDialog";
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
    task_id: null,
    project_id: "project-1",
    rel_path: "project-1/spec.docx",
    original_name: "spec.docx",
    mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    size: 4096,
    note: null,
    created_at: "2026-01-01T00:00:00Z",
    abs_path: "/abs/spec.docx",
    ...overrides,
  };
}

describe("ProjectReferencesDialog", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("(d) loads and renders the project's reference files when opened", async () => {
    const listSpy = vi.spyOn(api.references, "list").mockResolvedValue([mkRef()]);

    renderWithQuery(
      <ProjectReferencesDialog
        open
        onClose={() => {}}
        projectId="project-1"
        projectName="Acme Project"
      />
    );

    expect(await screen.findByText("spec.docx")).toBeInTheDocument();
    expect(listSpy).toHaveBeenCalledWith({ projectId: "project-1" });
    expect(screen.getByText("Acme Project")).toBeInTheDocument();
    expect(
      screen.getByText("Agents receive these files with every task in this project.")
    ).toBeInTheDocument();
  });

  it("shows an empty state when the project has no reference files yet", async () => {
    vi.spyOn(api.references, "list").mockResolvedValue([]);

    renderWithQuery(
      <ProjectReferencesDialog
        open
        onClose={() => {}}
        projectId="project-2"
        projectName="Empty Project"
      />
    );

    expect(await screen.findByText("No reference files yet.")).toBeInTheDocument();
  });

  it("does not fetch the list while closed", () => {
    const listSpy = vi.spyOn(api.references, "list").mockResolvedValue([]);

    renderWithQuery(
      <ProjectReferencesDialog
        open={false}
        onClose={() => {}}
        projectId="project-1"
        projectName="Acme Project"
      />
    );

    expect(listSpy).not.toHaveBeenCalled();
  });
});
