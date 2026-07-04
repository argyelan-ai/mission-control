/**
 * CreateTaskModal — Reference file staging + upload (ADR-053).
 *
 * The task mask lets the operator stage example/asset files before the task
 * exists. They can't be part of the JSON create-payload (no task_id yet), so
 * TaskFormFields keeps them in local state and reports them upward; this
 * modal uploads them sequentially once `api.tasks.create` returns an id.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CreateTaskModal } from "../CreateTaskModal";
import { api } from "@/lib/api";
import type { ReferenceFile, Task } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

async function openModal() {
  await userEvent.click(screen.getByRole("button", { name: "New task" }));
}

describe("CreateTaskModal — reference file upload (ADR-053)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    vi.spyOn(api.projects, "list").mockResolvedValue([]);
    vi.spyOn(api.credentials, "list").mockResolvedValue([]);
    vi.spyOn(api.repos, "list").mockResolvedValue([]);
  });

  it("(a) stages a file and uploads it with the new task's id after create", async () => {
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-99" } as Task);
    const uploadSpy = vi.spyOn(api.references, "upload").mockResolvedValue({} as ReferenceFile);

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();
    await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Task with refs");

    const file = new File(["hello"], "brief.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText(/Add files/i), file);
    expect(await screen.findByText("brief.pdf")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(createSpy).toHaveBeenCalled());
    await waitFor(() =>
      expect(uploadSpy).toHaveBeenCalledWith({ taskId: "task-99" }, file, undefined)
    );
  });

  it("(a2) forwards a shared note to every staged upload", async () => {
    vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-1" } as Task);
    const uploadSpy = vi.spyOn(api.references, "upload").mockResolvedValue({} as ReferenceFile);

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();
    await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Task with refs");

    const file = new File(["hello"], "brief.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText(/Add files/i), file);
    await userEvent.type(screen.getByLabelText("Note for reference files"), "Use this as ground truth");

    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() =>
      expect(uploadSpy).toHaveBeenCalledWith({ taskId: "task-1" }, file, "Use this as ground truth")
    );
  });

  it("(b) shows a banner and keeps the modal open when an upload fails", async () => {
    vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-1" } as Task);
    // A well-formed file (allowed extension) that the backend still rejects,
    // e.g. because it exceeds the 25MB limit — a real failure mode, not a
    // client-side accept-attribute mismatch.
    vi.spyOn(api.references, "upload").mockRejectedValue(new Error("API 413: file too large"));

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();
    await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Task with refs");

    const file = new File(["hello"], "huge.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText(/Add files/i), file);
    expect(await screen.findByText("huge.pdf")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    expect(await screen.findByText(/Task created, but 1 reference upload failed/)).toBeInTheDocument();
    expect(screen.getByText(/huge\.pdf: API 413: file too large/)).toBeInTheDocument();
    // The modal stayed open — the title field is still on screen.
    expect(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel")).toBeInTheDocument();
  });
});
