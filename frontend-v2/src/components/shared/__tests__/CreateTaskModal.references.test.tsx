/**
 * CreateTaskModal — Reference file staging + upload (ADR-053) + the
 * dispatch-race / double-task review fixes (C2/M2).
 *
 * The task mask lets the operator stage example/asset files before the task
 * exists. They can't be part of the JSON create-payload (no task_id yet), so
 * TaskFormFields keeps them in local state and reports them upward; this
 * modal uploads them sequentially once `api.tasks.create` returns an id.
 *
 * C2: `api.tasks.create` normally auto-dispatches server-side immediately,
 * which would build the agent brief before the uploads land. When files are
 * staged we pass `defer_dispatch: true` and fetch the dispatch up ourselves
 * via `api.tasks.dispatchDeferred` only once every upload has succeeded.
 *
 * M2: if some uploads fail, the task already exists — resubmitting must not
 * create a second task or re-upload files that already succeeded.
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

  it("(a) sends defer_dispatch: true when a file is staged", async () => {
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-99" } as Task);
    vi.spyOn(api.references, "upload").mockResolvedValue({} as ReferenceFile);
    vi.spyOn(api.tasks, "dispatchDeferred").mockResolvedValue({ status: "dispatch_triggered" });

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();
    await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Task with refs");

    const file = new File(["hello"], "brief.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText(/Add files/i), file);
    expect(await screen.findByText("brief.pdf")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(createSpy).toHaveBeenCalled());
    const [, body] = createSpy.mock.calls[0];
    expect(body).toMatchObject({ defer_dispatch: true });
  });

  it("(a-no-files) omits defer_dispatch when no files are staged", async () => {
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-1" } as Task);

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();
    await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Plain task");

    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(createSpy).toHaveBeenCalled());
    const [, body] = createSpy.mock.calls[0] as [string, Record<string, unknown>];
    expect(body.defer_dispatch).toBeUndefined();
  });

  it("(a2) forwards a shared note to every staged upload", async () => {
    vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-1" } as Task);
    const uploadSpy = vi.spyOn(api.references, "upload").mockResolvedValue({} as ReferenceFile);
    vi.spyOn(api.tasks, "dispatchDeferred").mockResolvedValue({ status: "dispatch_triggered" });

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

  it("(b) dispatches the deferred task exactly once after all staged uploads succeed", async () => {
    vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-99" } as Task);
    vi.spyOn(api.references, "upload").mockResolvedValue({} as ReferenceFile);
    const dispatchSpy = vi.spyOn(api.tasks, "dispatchDeferred").mockResolvedValue({ status: "dispatch_triggered" });

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();
    await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Task with refs");

    const file = new File(["hello"], "brief.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText(/Add files/i), file);
    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(dispatchSpy).toHaveBeenCalledTimes(1));
    expect(dispatchSpy).toHaveBeenCalledWith("board-1", "task-99");
  });

  it("(b-no-files) does not call dispatchDeferred when nothing was staged (normal auto-dispatch applies)", async () => {
    vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-1" } as Task);
    const dispatchSpy = vi.spyOn(api.tasks, "dispatchDeferred").mockResolvedValue({ status: "dispatch_triggered" });

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();
    await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Plain task");
    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(api.tasks.create).toHaveBeenCalled());
    expect(dispatchSpy).not.toHaveBeenCalled();
  });

  it("(banner) shows a banner, keeps the modal open, and does not dispatch when an upload fails", async () => {
    vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-1" } as Task);
    // A well-formed file (allowed extension) that the backend still rejects,
    // e.g. because it exceeds the 25MB limit — a real failure mode, not a
    // client-side accept-attribute mismatch.
    vi.spyOn(api.references, "upload").mockRejectedValue(new Error("API 413: file too large"));
    const dispatchSpy = vi.spyOn(api.tasks, "dispatchDeferred").mockResolvedValue({ status: "dispatch_triggered" });

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();
    await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Task with refs");

    const file = new File(["hello"], "huge.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText(/Add files/i), file);
    expect(await screen.findByText("huge.pdf")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    expect(await screen.findByText(/Task created, but 1 reference upload failed/)).toBeInTheDocument();
    expect(screen.getByText(/huge\.pdf: API 413: file too large/)).toBeInTheDocument();
    expect(screen.getByText(/It has not been dispatched yet/)).toBeInTheDocument();
    // The modal stayed open — the title field is still on screen.
    expect(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel")).toBeInTheDocument();
    expect(dispatchSpy).not.toHaveBeenCalled();
    // The button switched into the retry state instead of "Create task".
    expect(screen.getByRole("button", { name: "Retry uploads" })).toBeInTheDocument();
  });

  it("(c) resubmitting after a partial failure retries only the failed uploads, doesn't recreate the task, then dispatches", async () => {
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-1" } as Task);
    let bFailedOnce = false;
    const uploadSpy = vi.spyOn(api.references, "upload").mockImplementation(async (_target, file) => {
      if (file.name === "b.png" && !bFailedOnce) {
        bFailedOnce = true;
        throw new Error("API 500: transient");
      }
      return {} as ReferenceFile;
    });
    const dispatchSpy = vi.spyOn(api.tasks, "dispatchDeferred").mockResolvedValue({ status: "dispatch_triggered" });

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();
    await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Task with refs");

    const fileA = new File(["a"], "a.png", { type: "image/png" });
    const fileB = new File(["b"], "b.png", { type: "image/png" });
    await userEvent.upload(screen.getByLabelText(/Add files/i), [fileA, fileB]);
    expect(await screen.findByText("a.png")).toBeInTheDocument();
    expect(screen.getByText("b.png")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    expect(await screen.findByText(/Task created, but 1 reference upload failed/)).toBeInTheDocument();
    expect(screen.getByText(/b\.png: API 500: transient/)).toBeInTheDocument();
    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(uploadSpy).toHaveBeenCalledTimes(2); // a.png (ok) + b.png (failed)
    expect(dispatchSpy).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Retry uploads" }));

    await waitFor(() => expect(dispatchSpy).toHaveBeenCalledTimes(1));
    // Still only the original create call — no second task.
    expect(createSpy).toHaveBeenCalledTimes(1);
    // Exactly one more upload call (b.png retried), a.png was not re-uploaded.
    expect(uploadSpy).toHaveBeenCalledTimes(3);
    expect(uploadSpy).toHaveBeenNthCalledWith(3, { taskId: "task-1" }, fileB, undefined);
    expect(dispatchSpy).toHaveBeenCalledWith("board-1", "task-1");
  });
});
