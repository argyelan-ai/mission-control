import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Loop } from "@/lib/types";

// Plain selector-mock for the persisted zustand store (same convention as
// LoopsPage.test.tsx) — CreateLoopDialog only reads activeBoardId.
const mockAppState = vi.hoisted(() => ({
  state: { activeBoardId: "board-1" as string | null },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector?: (s: typeof mockAppState.state) => unknown) =>
    selector ? selector(mockAppState.state) : mockAppState.state,
}));

import { CreateLoopDialog } from "../CreateLoopDialog";

function renderDialog(over: { onCreated?: (l: Loop) => void; onClose?: () => void } = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <CreateLoopDialog
        open
        onClose={over.onClose ?? vi.fn()}
        onCreated={over.onCreated ?? vi.fn()}
      />
    </QueryClientProvider>
  );
}

describe("CreateLoopDialog", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.boards, "list").mockResolvedValue([
      { id: "board-1", name: "MC Development" } as never,
    ]);
  });

  it("shows the source-specific input nested inside the selected source row", async () => {
    renderDialog();

    // Default source is the Markdown list — its textarea is present.
    expect(await screen.findByLabelText("Backlog (Markdown) *")).toBeInTheDocument();

    // Switch to Tag — markdown textarea leaves, tag input appears.
    await userEvent.click(screen.getByRole("radio", { name: /Tag/ }));
    expect(screen.queryByLabelText("Backlog (Markdown) *")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Tag *")).toBeInTheDocument();
  });

  it("hides the stop-on-empty-backlog switch for an open-ended loop", async () => {
    renderDialog();

    expect(
      await screen.findByRole("switch", { name: "Stop when the backlog is empty" })
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: /Open-ended/ }));
    expect(
      screen.queryByRole("switch", { name: "Stop when the backlog is empty" })
    ).not.toBeInTheDocument();
  });

  it("restates the guardrails in the live contract summary", async () => {
    renderDialog();

    // Defaults: 10 rounds, breaker at 2, telegram reports on, stop on empty.
    const summary = await screen.findByText(/max 10 rounds/);
    expect(summary.textContent).toContain("pause after 2 failed rounds");
    expect(summary.textContent).toContain("stops on empty backlog");
    expect(summary.textContent).toContain("Telegram report per round");

    // Entering a budget adds it to the contract line.
    await userEvent.type(screen.getByLabelText(/Budget \(USD\)/), "5");
    expect(screen.getByText(/max 10 rounds/).textContent).toContain("budget $5");
  });

  it("clears a stale source error when the source changes", async () => {
    renderDialog();

    // Submit with Tag selected but empty → source error appears.
    await userEvent.click(await screen.findByRole("radio", { name: /^Tag/ }));
    await userEvent.click(screen.getByRole("button", { name: "Create loop" }));
    expect(await screen.findByText("Enter a tag to pull the backlog from.")).toBeInTheDocument();

    // Switching to Open-ended must drop the now-irrelevant error.
    await userEvent.click(screen.getByRole("radio", { name: /Open-ended/ }));
    expect(screen.queryByText("Enter a tag to pull the backlog from.")).not.toBeInTheDocument();
  });

  it("submits the tag payload and reports the created loop", async () => {
    const created = { id: "loop-9" } as Loop;
    const createSpy = vi.spyOn(api.loops, "create").mockResolvedValue(created);
    const onCreated = vi.fn();
    renderDialog({ onCreated });

    await userEvent.type(await screen.findByPlaceholderText("Nightly polish loop"), "Polish grind");
    await userEvent.type(screen.getByPlaceholderText(/Drive down open bugs/), "Polish the UI");
    await userEvent.click(screen.getByRole("radio", { name: /^Tag/ }));
    await userEvent.type(screen.getByLabelText("Tag *"), "polish");
    await userEvent.click(screen.getByRole("button", { name: "Create loop" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created));
    expect(createSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        board_id: "board-1",
        name: "Polish grind",
        backlog_source: "tag",
        backlog_tag: "polish",
        stop_on_backlog_empty: true,
        telegram_reports: true,
      })
    );
  });
});
