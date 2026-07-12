/**
 * CreateTaskModal — skip_review field sent in the create request body.
 *
 * Mirrors the ADR-052 test style (CreateTaskModal.test.tsx).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CreateTaskModal } from "../CreateTaskModal";
import { api } from "@/lib/api";
import type { Task } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("CreateTaskModal — skip_review body field", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    vi.spyOn(api.projects, "list").mockResolvedValue([]);
    vi.spyOn(api.credentials, "list").mockResolvedValue([]);
    vi.spyOn(api.repos, "list").mockResolvedValue([]);
  });

  it("sends skip_review: false by default", async () => {
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-1" } as Task);

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "New task" }));
    await userEvent.type(
      await screen.findByPlaceholderText("Kurzer, klarer Aufgabentitel"),
      "Dummy task",
    );
    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(createSpy).toHaveBeenCalled());
    const [, body] = createSpy.mock.calls[0];
    expect(body).toMatchObject({ skip_review: false });
  });

  it("sends skip_review: true when the toggle is clicked", async () => {
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-1" } as Task);

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "New task" }));
    await userEvent.type(
      await screen.findByPlaceholderText("Kurzer, klarer Aufgabentitel"),
      "Automation task",
    );

    // Open "Erweitert" section to reveal the Skip review pill
    const erweitertBtn = screen.getByRole("button", { name: /erweitert/i });
    await userEvent.click(erweitertBtn);

    // Now the Skip review pill should be visible
    const [pill] = await screen.findAllByRole("button", { name: /skip review/i });
    await userEvent.click(pill);

    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(createSpy).toHaveBeenCalled());
    const [, body] = createSpy.mock.calls[0];
    expect(body).toMatchObject({ skip_review: true });
  });
});
