import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Loop } from "@/lib/types";

// next/navigation is mocked so AppShell's auth guard + Sidebar/MobileNav render
// without a real Next router (same convention as ReposPage.test.tsx).
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/loops",
}));

// The real zustand store uses persist middleware — a plain selector-mock
// dodges the jsdom localStorage write (same convention as
// runtimes/__tests__/HostsSection.test.tsx). Supports both the selector form
// (LoopsPage/CreateLoopDialog: useAppStore((s) => s.activeBoardId)) and the
// destructured form used by Sidebar/AppShell/StatusBar/etc.
const mockAppState = vi.hoisted(() => ({
  state: {
    activeBoardId: "board-1" as string | null,
    sidebarCollapsed: false,
    commandPaletteOpen: false,
    boards: [] as unknown[],
    boardGroups: [] as unknown[],
    currentUser: null as { id: string; email: string; name: string; role: string } | null,
    setActiveBoardId: (id: string | null) => { mockAppState.state.activeBoardId = id; },
    toggleSidebar: () => {},
    setCommandPaletteOpen: (open: boolean) => { mockAppState.state.commandPaletteOpen = open; },
    setBoards: (boards: unknown[]) => { mockAppState.state.boards = boards; },
    setBoardGroups: (boardGroups: unknown[]) => { mockAppState.state.boardGroups = boardGroups; },
    setCurrentUser: (user: typeof mockAppState.state.currentUser) => { mockAppState.state.currentUser = user; },
  },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: Object.assign(
    (selector?: (s: typeof mockAppState.state) => unknown) =>
      selector ? selector(mockAppState.state) : mockAppState.state,
    { setState: (partial: Partial<typeof mockAppState.state>) => Object.assign(mockAppState.state, partial) }
  ),
}));

import LoopsPage from "../page";

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <LoopsPage />
    </QueryClientProvider>
  );
}

const makeLoop = (over: Partial<Loop> = {}): Loop => ({
  id: "loop-1",
  board_id: "board-1",
  project_id: null,
  name: "Nightly polish loop",
  goal: "Drive down open bugs until the backlog is empty.",
  backlog_source: "markdown",
  backlog_md: "- fix flaky test",
  backlog_tag: null,
  round_brief: null,
  human_every_n_rounds: 0,
  pause_on_failed_rounds: 2,
  escalate_on: null,
  max_rounds: 10,
  max_duration_minutes: null,
  stop_on_backlog_empty: true,
  status: "draft",
  rounds_completed: 0,
  consecutive_failed_rounds: 0,
  current_round_no: null,
  current_task_id: null,
  last_error: null,
  started_at: null,
  finished_at: null,
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
  ...over,
});

describe("LoopsPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();

    // AppShell auth guard requires a token to authorize and render content.
    const store: Record<string, string> = { mc_auth_token: "tok" };
    Object.defineProperty(globalThis, "localStorage", {
      value: {
        getItem: (k: string) => store[k] ?? null,
        setItem: (k: string, v: string) => { store[k] = v; },
        removeItem: (k: string) => { delete store[k]; },
        clear: () => undefined,
      },
      configurable: true,
      writable: true,
    });

    // Generic fetch stub for any unmocked api call (approvals badge, boards, etc.).
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );

    mockAppState.state.activeBoardId = "board-1";
  });

  it("shows the empty state when there are 0 loops", async () => {
    vi.spyOn(api.loops, "list").mockResolvedValue([]);

    renderPage();

    expect(await screen.findByRole("heading", { name: "Loops" })).toBeInTheDocument();
    expect(await screen.findByText("No loops yet")).toBeInTheDocument();
  });

  it("renders a running and a paused loop with status badges and progress", async () => {
    vi.spyOn(api.loops, "list").mockResolvedValue([
      makeLoop({
        id: "loop-running",
        name: "Report engine grind",
        status: "running",
        rounds_completed: 3,
        current_round_no: 4,
        max_rounds: 10,
      }),
      makeLoop({
        id: "loop-paused",
        name: "Docs cleanup loop",
        status: "paused",
        rounds_completed: 2,
        consecutive_failed_rounds: 2,
      }),
    ]);

    renderPage();

    expect(await screen.findByText("Report engine grind")).toBeInTheDocument();
    expect(screen.getByText("Docs cleanup loop")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(screen.getByText("Round 4 of 10")).toBeInTheDocument();
    expect(screen.getByText(/2 failed rounds in a row/)).toBeInTheDocument();
  });

  it("starts a draft loop via the Start action", async () => {
    vi.spyOn(api.loops, "list").mockResolvedValue([makeLoop({ status: "draft" })]);
    const startSpy = vi.spyOn(api.loops, "start").mockResolvedValue(makeLoop({ status: "running" }));

    renderPage();

    const startBtn = await screen.findByRole("button", { name: "Start" });
    await userEvent.click(startBtn);

    await waitFor(() => expect(startSpy).toHaveBeenCalledWith("loop-1"));
  });

  it("shows the backend's 409 error text when start is blocked by an active loop on the board", async () => {
    vi.spyOn(api.loops, "list").mockResolvedValue([makeLoop({ status: "draft" })]);
    vi.spyOn(api.loops, "start").mockRejectedValue(
      new Error('API 409: {"detail":"Board already has an active loop — pause or stop it first"}')
    );

    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Start" }));

    expect(
      await screen.findByText("Board already has an active loop — pause or stop it first")
    ).toBeInTheDocument();
  });

  it("requires a markdown backlog when the backlog source is Markdown list", async () => {
    vi.spyOn(api.loops, "list").mockResolvedValue([]);
    vi.spyOn(api.boards, "list").mockResolvedValue([
      { id: "board-1", name: "MC Development" } as never,
    ]);
    const createSpy = vi.spyOn(api.loops, "create");

    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "New loop" }));

    const nameInput = await screen.findByPlaceholderText("Nightly polish loop");
    await userEvent.type(nameInput, "Backlog grind");

    const boardSelect = await screen.findByDisplayValue("MC Development");
    expect(boardSelect).toBeInTheDocument();

    const goalInput = screen.getByPlaceholderText(/Drive down open bugs/);
    await userEvent.type(goalInput, "Clear the bug backlog");

    // Backlog source defaults to "Markdown list" — leave backlog_md empty and submit.
    await userEvent.click(screen.getByRole("button", { name: "Create loop" }));

    expect(
      await screen.findByText("Markdown backlog is required when backlog source is a Markdown list.")
    ).toBeInTheDocument();
    expect(createSpy).not.toHaveBeenCalled();
  });
});
