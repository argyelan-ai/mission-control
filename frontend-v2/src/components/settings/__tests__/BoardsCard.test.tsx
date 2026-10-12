/**
 * BoardsCard (Settings → Profile) — "create board" moved here while the
 * sidebar switcher is hidden for a single board (operator decision
 * 12.10.2026, variant B). With two or more boards the sidebar's own
 * switcher already carries "+ New board", so this card steps back (K3).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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
const createMock = vi.fn((payload: Record<string, unknown>) =>
  Promise.resolve({ ...oneBoard[0], id: "b-new", name: "New One", ...payload }),
);

vi.mock("@/lib/api", () => ({
  api: {
    boards: {
      list: () => Promise.resolve(listResult),
      create: (payload: Record<string, unknown>) => createMock(payload),
    },
  },
}));

import { BoardsCard } from "../BoardsCard";

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <BoardsCard />
    </QueryClientProvider>,
  );
}

describe("BoardsCard", () => {
  beforeEach(() => {
    listResult = oneBoard;
    createMock.mockClear();
  });

  it("offers a quiet create entry with one board, and creates a board through it", async () => {
    renderCard();
    const createTrigger = await screen.findByRole("button", { name: /new board/i });
    await userEvent.click(createTrigger);

    const input = screen.getByPlaceholderText(/board name/i);
    await userEvent.type(input, "Second Board");
    await userEvent.click(screen.getByRole("button", { name: /create board/i }));

    expect(createMock).toHaveBeenCalledTimes(1);
    expect(createMock.mock.calls[0][0]).toMatchObject({ name: "Second Board" });
  });

  it("steps back once there are two or more boards — the sidebar switcher already offers it", async () => {
    listResult = twoBoards;
    const { container } = renderCard();
    // Renders nothing once the boards query resolves: no duplicate
    // "create board" entry (K3).
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
