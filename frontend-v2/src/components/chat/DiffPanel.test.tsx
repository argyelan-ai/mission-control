/**
 * DiffPanel — Task C1 vitest.
 *
 * Coverage: renders the loaded diff via GitDiffView, scope switch refetches
 * with the new scope, `refreshHot` drives the 15s auto-poll on/off, the
 * manual refresh button always works, and the two special-case states
 * (empty diff / no_workspace 404) render their German copy.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DiffPanel } from "./DiffPanel";
import { api } from "@/lib/api";
import type { CommitDiff } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: {
    chat: {
      diff: vi.fn(),
    },
  },
}));

const diffMock = vi.mocked(api.chat.diff);

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function mkDiff(overrides: Partial<CommitDiff> = {}): CommitDiff {
  return {
    hash: "abc123",
    message: "wip",
    author: "Cody",
    date: "2026-08-16T10:00:00Z",
    stats: { files: 1, additions: 2, deletions: 1 },
    files: [
      {
        filename: "foo.ts",
        additions: 2,
        deletions: 1,
        hunks: [
          {
            header: "@@ -1,3 +1,4 @@",
            lines: [
              { type: "ctx", content: "a", old_no: 1, new_no: 1 },
              { type: "add", content: "b", old_no: null, new_no: 2 },
              { type: "del", content: "c", old_no: 2, new_no: null },
            ],
          },
        ],
      },
    ],
    ...overrides,
  };
}

// jsdom in this environment has no working localStorage (every other
// *.test.tsx in the repo hits the same gap and stubs its own — see
// SessionsPage.test.tsx) — a plain in-memory Storage shim, reset per test.
let store: Record<string, string>;

beforeEach(() => {
  vi.clearAllMocks();
  store = {};
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
      clear: () => { store = {}; },
      length: 0,
      key: () => null,
    },
  });
});

describe("DiffPanel", () => {
  it("renders the loaded diff", async () => {
    diffMock.mockResolvedValue(mkDiff());
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("foo.ts")).toBeInTheDocument();
    expect(diffMock).toHaveBeenCalledWith("agent-1", "worktree");
  });

  it("defaults to the Arbeitsstand (worktree) scope tab active", async () => {
    diffMock.mockResolvedValue(mkDiff());
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    await waitFor(() => expect(diffMock).toHaveBeenCalled());
    expect(screen.getByRole("tab", { name: "Arbeitsstand" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Letzter Commit" })).toHaveAttribute("aria-selected", "false");
  });

  it("switching to Letzter Commit refetches with scope=last-commit", async () => {
    diffMock.mockResolvedValue(mkDiff());
    const user = userEvent.setup();
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    await waitFor(() => expect(diffMock).toHaveBeenCalledWith("agent-1", "worktree"));

    await user.click(screen.getByRole("tab", { name: "Letzter Commit" }));

    await waitFor(() => expect(diffMock).toHaveBeenCalledWith("agent-1", "last-commit"));
    expect(screen.getByRole("tab", { name: "Letzter Commit" })).toHaveAttribute("aria-selected", "true");
  });

  it("persists the scope choice to localStorage and restores it on remount", async () => {
    diffMock.mockResolvedValue(mkDiff());
    const user = userEvent.setup();
    const { unmount } = renderWithQuery(<DiffPanel agentId="agent-1" />);

    await waitFor(() => expect(diffMock).toHaveBeenCalledWith("agent-1", "worktree"));
    await user.click(screen.getByRole("tab", { name: "Letzter Commit" }));
    await waitFor(() => expect(diffMock).toHaveBeenCalledWith("agent-1", "last-commit"));
    unmount();

    diffMock.mockClear();
    renderWithQuery(<DiffPanel agentId="agent-1" />);
    await waitFor(() => expect(diffMock).toHaveBeenCalledWith("agent-1", "last-commit"));
  });

  it("shows the empty state when the diff has no files", async () => {
    diffMock.mockResolvedValue(mkDiff({ files: [], stats: { files: 0, additions: 0, deletions: 0 } }));
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("Keine Änderungen")).toBeInTheDocument();
  });

  it("shows the no-workspace state on a 404 no_workspace error", async () => {
    diffMock.mockRejectedValue(new Error('API 404: {"reason": "no_workspace"}'));
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("Kein Workspace")).toBeInTheDocument();
  });

  it("shows a generic error state for other failures", async () => {
    diffMock.mockRejectedValue(new Error("API 500: boom"));
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("Diff konnte nicht geladen werden.")).toBeInTheDocument();
  });

  it("manual refresh button always refetches, even when refreshHot is false", async () => {
    diffMock.mockResolvedValue(mkDiff());
    const user = userEvent.setup();
    renderWithQuery(<DiffPanel agentId="agent-1" refreshHot={false} />);

    await waitFor(() => expect(diffMock).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Aktualisieren" }));

    await waitFor(() => expect(diffMock).toHaveBeenCalledTimes(2));
  });

  it("does not poll when refreshHot is false", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    diffMock.mockResolvedValue(mkDiff());
    renderWithQuery(<DiffPanel agentId="agent-1" refreshHot={false} />);

    await vi.waitFor(() => expect(diffMock).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(20_000);
    expect(diffMock).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("polls every 15s when refreshHot is true", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    diffMock.mockResolvedValue(mkDiff());
    renderWithQuery(<DiffPanel agentId="agent-1" refreshHot={true} />);

    await vi.waitFor(() => expect(diffMock).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(15_000);
    await vi.waitFor(() => expect(diffMock).toHaveBeenCalledTimes(2));
    await vi.advanceTimersByTimeAsync(15_000);
    await vi.waitFor(() => expect(diffMock).toHaveBeenCalledTimes(3));
    vi.useRealTimers();
  });
});
