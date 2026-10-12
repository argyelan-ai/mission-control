/**
 * Sidebar top area — variant B + V1 (operator decisions 12.10.2026):
 *
 * - One board: the board name is plain text (no switcher/chevron/+), and
 *   the voice assistant (Jarvis) is a separate button beside the search
 *   row, not floating next to the name and not inside the search field
 *   (commit f61f392a moved it out of there once already). Its accessible
 *   name is "Voice (Jarvis)" — never reads as "search by voice".
 * - Two or more boards: the ordinary switcher (name + dropdown) is back,
 *   and the mic stays in the same place beside search.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const oneBoard = [
  { id: "b1", name: "Only Board", slug: "only-board", description: null, icon: null, color: "#fff" },
];
const twoBoards = [
  ...oneBoard,
  { id: "b2", name: "Second Board", slug: "second-board", description: null, icon: null, color: "#000" },
];

let boardsResult: typeof oneBoard = oneBoard;

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    system: {
      metrics: () =>
        Promise.resolve({
          tasks: { total: 0, active: 0 },
          agents: { total: 0, active: 0, paused: 0, online: 0 },
          approvals: { pending: 0 },
        }),
    },
    boards: {
      list: () => Promise.resolve(boardsResult),
      create: vi.fn(),
    },
  },
}));

vi.mock("@/lib/store", () => ({
  useAppStore: () => ({
    boards: boardsResult,
    activeBoardId: boardsResult[0]?.id ?? null,
    setActiveBoardId: vi.fn(),
    setBoards: vi.fn(),
    sidebarCollapsed: false,
    setCommandPaletteOpen: vi.fn(),
  }),
}));

// The real mic starts a LiveKit call and needs <VoiceProvider> — this test
// only cares about placement and the accessible label, so it stubs the
// button down to what Sidebar actually passes it.
vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceButton: ({ label }: { label?: string }) => (
    <button type="button" aria-label={label} data-testid="voice-button">
      mic
    </button>
  ),
}));

vi.mock("../SidebarFooter", () => ({ __esModule: true, default: () => null }));

import Sidebar from "../Sidebar";

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Sidebar />
    </QueryClientProvider>,
  );
}

describe("Sidebar top area", () => {
  beforeEach(() => {
    boardsResult = oneBoard;
  });

  it("one board: name is plain text, mic sits beside search with a clear label", async () => {
    renderSidebar();

    const name = await screen.findByText("Only Board");
    expect(name.closest("button")).toBeNull();
    expect(screen.queryByRole("listbox")).toBeNull();

    const mic = screen.getByTestId("voice-button");
    expect(mic).toHaveAccessibleName("Voice (Jarvis)");

    // Beside the search row, not floating next to the board name: the
    // search trigger and the mic share one parent row.
    const search = screen.getByText("Search").closest("button")!;
    expect(mic.parentElement).toBe(search.parentElement);
  });

  it("two boards: the ordinary switcher is back, mic unchanged", async () => {
    boardsResult = twoBoards;
    renderSidebar();

    const trigger = await screen.findByText("Only Board");
    expect(trigger.closest("button")).not.toBeNull();

    const mic = screen.getByTestId("voice-button");
    expect(mic).toHaveAccessibleName("Voice (Jarvis)");
    const search = screen.getByText("Search").closest("button")!;
    expect(mic.parentElement).toBe(search.parentElement);
  });
});
