/**
 * BoardPicker — the switcher disappears while there is only one board
 * (operator decision 12.10.2026, variant B): the board name stays visible
 * as plain text (no chevron, no dropdown, no lone "+"); "create board"
 * moves to Settings → Profile (`BoardsCard`, covered by its own test).
 * With a second board, the ordinary switcher (board name + dropdown) is
 * unchanged.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const oneBoard = [
  { id: "b1", name: "Only Board", slug: "only-board", description: null, icon: null, color: "#fff" },
];
const twoBoards = [
  ...oneBoard,
  { id: "b2", name: "Second Board", slug: "second-board", description: null, icon: null, color: "#000" },
];

let listResult: typeof oneBoard = oneBoard;

vi.mock("@/lib/api", () => ({
  api: {
    boards: {
      list: () => Promise.resolve(listResult),
      create: vi.fn(),
    },
  },
}));

vi.mock("@/lib/store", () => ({
  useAppStore: () => ({
    boards: listResult,
    activeBoardId: listResult[0]?.id ?? null,
    setActiveBoardId: vi.fn(),
    setBoards: vi.fn(),
  }),
}));

import BoardPicker from "../BoardPicker";

function renderPicker() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <BoardPicker />
    </QueryClientProvider>,
  );
}

describe("BoardPicker single-board mode", () => {
  beforeEach(() => {
    listResult = oneBoard;
  });

  it("shows the board name as plain text with one board — no button, no menu", async () => {
    renderPicker();
    const label = await screen.findByText("Only Board");
    // Plain text, not an interactive control: no enclosing button/listbox role.
    expect(label.closest("button")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("listbox")).toBeNull();
    await userEvent.click(label);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("shows the ordinary switcher with two or more boards", async () => {
    listResult = twoBoards;
    renderPicker();
    const trigger = await screen.findByText("Only Board");
    expect(trigger.closest("button")).not.toBeNull();
    await userEvent.click(trigger);
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(screen.getByText("Second Board")).toBeInTheDocument();
  });
});
