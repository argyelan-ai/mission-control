/**
 * DiffPanel ("Changes") vitest.
 *
 * Coverage: "This chat" is the default tab and lists the chat's own changes
 * (ChatChangesList: rows, tap → combined diff, back); the git tabs exist ONLY
 * when the chat's folder is a repository (`changes.repo`) — otherwise a quiet
 * "not a git repository" line and the git diff is never fetched; git tab
 * switch refetches with the new scope, `refreshHot` drives the 15s auto-poll,
 * the manual refresh button always works, the end of a turn refetches, the
 * special-case states (empty diff / no commit yet / no_workspace 404) render
 * their catalog copy, and the repo line names WHICH repository is shown.
 * Labels come from the real EN catalog (test-setup's next-intl mock).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DiffPanel } from "./DiffPanel";
import { api } from "@/lib/api";
import type { ChatDiffSource, CommitDiff } from "@/lib/types";
import type { ChatChanges, ChatChangeFile } from "@/lib/chatTypes";

vi.mock("@/lib/api", () => ({
  api: {
    chat: {
      diff: vi.fn(),
      changes: vi.fn(),
    },
  },
}));

const diffMock = vi.mocked(api.chat.diff);
const changesMock = vi.mocked(api.chat.changes);

const REPO: ChatDiffSource = { kind: "session", repo: "scratch", branch: "main", path: "/workspace/scratch" };

function mkFile(overrides: Partial<ChatChangeFile> = {}): ChatChangeFile {
  return {
    path: "src/app.py",
    additions: 3,
    deletions: 1,
    edits: 2,
    created: false,
    written: false,
    lastAt: null,
    diff: {
      path: "src/app.py",
      kind: "edit",
      lines: [
        { t: "hunk", text: "@@ -1 +1 @@" },
        { t: "del", text: "old line" },
        { t: "add", text: "new line" },
      ],
      additions: 3,
      deletions: 1,
      truncated: false,
    },
    ...overrides,
  };
}

function mkChanges(files: ChatChangeFile[] = [mkFile()], repo: ChatDiffSource | null = REPO): ChatChanges {
  return {
    files,
    stats: {
      files: files.length,
      additions: files.reduce((n, f) => n + f.additions, 0),
      deletions: files.reduce((n, f) => n + f.deletions, 0),
    },
    repo,
  };
}

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
  return {
    ...view,
    rerenderWithQuery: (next: React.ReactElement) =>
      view.rerender(<QueryClientProvider client={qc}>{next}</QueryClientProvider>),
  };
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
  changesMock.mockResolvedValue(mkChanges());
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
    store["mc.chat.changestab"] = "worktree";
    diffMock.mockResolvedValue(mkDiff());
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("foo.ts")).toBeInTheDocument();
    expect(diffMock).toHaveBeenCalledWith("agent-1", "worktree");
  });

  it("defaults to the This chat tab, and fetches no git diff for it", async () => {
    diffMock.mockResolvedValue(mkDiff());
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByRole("tab", { name: "This chat" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Uncommitted" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: "Last commit" })).toHaveAttribute("aria-selected", "false");
    expect(changesMock).toHaveBeenCalledWith("agent-1");
    expect(diffMock).not.toHaveBeenCalled();
  });

  it("switching to Last commit refetches with scope=last-commit", async () => {
    store["mc.chat.changestab"] = "worktree";
    diffMock.mockResolvedValue(mkDiff());
    const user = userEvent.setup();
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    await waitFor(() => expect(diffMock).toHaveBeenCalledWith("agent-1", "worktree"));

    await user.click(screen.getByRole("tab", { name: "Last commit" }));

    await waitFor(() => expect(diffMock).toHaveBeenCalledWith("agent-1", "last-commit"));
    expect(screen.getByRole("tab", { name: "Last commit" })).toHaveAttribute("aria-selected", "true");
  });

  it("persists the tab choice to localStorage and restores it on remount", async () => {
    diffMock.mockResolvedValue(mkDiff());
    const user = userEvent.setup();
    const { unmount } = renderWithQuery(<DiffPanel agentId="agent-1" />);

    await user.click(await screen.findByRole("tab", { name: "Last commit" }));
    await waitFor(() => expect(diffMock).toHaveBeenCalledWith("agent-1", "last-commit"));
    unmount();

    diffMock.mockClear();
    renderWithQuery(<DiffPanel agentId="agent-1" />);
    await waitFor(() => expect(diffMock).toHaveBeenCalledWith("agent-1", "last-commit"));
  });

  it("shows the empty state when the diff has no files", async () => {
    store["mc.chat.changestab"] = "worktree";
    diffMock.mockResolvedValue(mkDiff({ files: [], stats: { files: 0, additions: 0, deletions: 0 } }));
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("No uncommitted changes")).toBeInTheDocument();
  });

  it("shows the no-workspace state on a 404 no_workspace error", async () => {
    store["mc.chat.changestab"] = "worktree";
    diffMock.mockRejectedValue(new Error('API 404: {"reason": "no_workspace"}'));
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("This chat's folder is not a git repository")).toBeInTheDocument();
  });

  it("shows a generic error state for other failures", async () => {
    store["mc.chat.changestab"] = "worktree";
    diffMock.mockRejectedValue(new Error("API 500: boom"));
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("Couldn't load the diff.")).toBeInTheDocument();
  });

  it("manual refresh button always refetches, even when refreshHot is false", async () => {
    store["mc.chat.changestab"] = "worktree";
    diffMock.mockResolvedValue(mkDiff());
    const user = userEvent.setup();
    renderWithQuery(<DiffPanel agentId="agent-1" refreshHot={false} />);

    await waitFor(() => expect(diffMock).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(diffMock).toHaveBeenCalledTimes(2));
  });

  it("does not poll when refreshHot is false", async () => {
    store["mc.chat.changestab"] = "worktree";
    vi.useFakeTimers({ shouldAdvanceTime: true });
    diffMock.mockResolvedValue(mkDiff());
    renderWithQuery(<DiffPanel agentId="agent-1" refreshHot={false} />);

    await vi.waitFor(() => expect(diffMock).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(20_000);
    expect(diffMock).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("polls every 15s when refreshHot is true", async () => {
    store["mc.chat.changestab"] = "worktree";
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

  it("refetches once when a turn ends (refreshHot true -> false)", async () => {
    store["mc.chat.changestab"] = "worktree";
    diffMock.mockResolvedValue(mkDiff());
    const { rerenderWithQuery } = renderWithQuery(<DiffPanel agentId="agent-1" refreshHot={true} />);
    await waitFor(() => expect(diffMock).toHaveBeenCalledTimes(1));

    rerenderWithQuery(<DiffPanel agentId="agent-1" refreshHot={false} />);

    await waitFor(() => expect(diffMock).toHaveBeenCalledTimes(2));
    expect(diffMock).toHaveBeenLastCalledWith("agent-1", "worktree");
  });

  it("does not refetch when refreshHot merely stays false", async () => {
    store["mc.chat.changestab"] = "worktree";
    diffMock.mockResolvedValue(mkDiff());
    const { rerenderWithQuery } = renderWithQuery(<DiffPanel agentId="agent-1" refreshHot={false} />);
    await waitFor(() => expect(diffMock).toHaveBeenCalledTimes(1));

    rerenderWithQuery(<DiffPanel agentId="agent-1" refreshHot={false} />);

    await new Promise((r) => setTimeout(r, 50));
    expect(diffMock).toHaveBeenCalledTimes(1);
  });

  it("names the repository, branch and why it is shown", async () => {
    changesMock.mockResolvedValue(mkChanges([], REPO));
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    const line = await screen.findByTestId("diff-source");
    expect(line).toHaveTextContent("scratch · main");
    expect(within(line).getByText("Chat folder")).toBeInTheDocument();
    expect(line).toHaveAttribute("title", "/workspace/scratch");
  });

  it("shows ahead/behind of the upstream only when non-zero", async () => {
    changesMock.mockResolvedValue(
      mkChanges([], { kind: "session", repo: "mc", branch: "feat/x", path: "/workspace/mc", ahead: 2, behind: 0 }),
    );
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    const counts = await screen.findByTestId("diff-ahead-behind");
    expect(counts).toHaveTextContent(/^↑2$/);
    expect(within(counts).getByTitle("2 commits ahead of upstream")).toBeInTheDocument();
  });

  it("shows no ahead/behind without an upstream", async () => {
    changesMock.mockResolvedValue(
      mkChanges([], { kind: "session", repo: "mc", branch: "main", path: "/workspace/mc", ahead: null, behind: null }),
    );
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    await screen.findByTestId("diff-source");
    expect(screen.queryByTestId("diff-ahead-behind")).not.toBeInTheDocument();
  });

  it("shows no raw key for a source kind it does not know", async () => {
    changesMock.mockResolvedValue(
      mkChanges([], { kind: "future" as never, repo: "r", branch: null, path: "/workspace/r" }),
    );
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    const line = await screen.findByTestId("diff-source");
    expect(line).toHaveTextContent("r");
    expect(line.textContent).not.toContain("kind.");
  });

  it("last commit shows its short id and age next to the message", async () => {
    store["mc.chat.changestab"] = "last-commit";
    const committedAt = new Date(Date.now() - 3 * 60_000).toISOString();
    diffMock.mockResolvedValue(
      mkDiff({ scope: "last-commit", hash: "ea3dd2f", message: "test: probe", committed_at: committedAt }),
    );
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("test: probe")).toBeInTheDocument();
    expect(screen.getByText(/^ea3dd2f · 3 minutes ago$/)).toBeInTheDocument();
  });

  it("says 'No commit yet' for a repository without commits", async () => {
    store["mc.chat.changestab"] = "last-commit";
    diffMock.mockResolvedValue(
      mkDiff({ scope: "last-commit", hash: "", message: "", files: [], stats: { files: 0, additions: 0, deletions: 0 } }),
    );
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("No commit yet")).toBeInTheDocument();
  });

  it("an uncommitted diff renders no backend sentence, only the stats", async () => {
    store["mc.chat.changestab"] = "worktree";
    diffMock.mockResolvedValue(mkDiff({ scope: "worktree", hash: "", message: "" }));
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("1 file")).toBeInTheDocument();
    expect(screen.queryByText("Uncommitted changes")).not.toBeInTheDocument();
  });

  // ── "This chat" (default tab) ─────────────────────────────────────────────

  it("lists the files this chat changed, newest first, with +/− and edit count", async () => {
    changesMock.mockResolvedValue(
      mkChanges([
        mkFile({ path: "README.md", additions: 4, deletions: 0, edits: 1, created: true }),
        mkFile({ path: "src/app.py", additions: 3, deletions: 1, edits: 2 }),
      ]),
    );
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    const rows = await screen.findAllByTestId("chat-change-row");
    expect(rows.map((r) => r.getAttribute("title"))).toEqual(["README.md", "src/app.py"]);
    expect(rows[0]).toHaveTextContent("New file");
    expect(rows[1]).toHaveTextContent("src · 2 edits");
    expect(within(rows[1]).getByText("+3")).toBeInTheDocument();
    expect(within(rows[1]).getByText("−1")).toBeInTheDocument();
    expect(screen.getByTestId("chat-changes-summary")).toHaveTextContent("2 files");
  });

  it("tapping a file opens its combined diff; back returns to the list", async () => {
    const user = userEvent.setup();
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    await user.click(await screen.findByTestId("chat-change-row"));

    const detail = screen.getByTestId("chat-change-detail");
    expect(within(detail).getByTestId("tool-diff")).toBeInTheDocument();
    expect(within(detail).getByText("new line")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "All files" }));
    expect(await screen.findByTestId("chat-change-row")).toBeInTheDocument();
  });

  it("says so when the chat changed no file yet", async () => {
    changesMock.mockResolvedValue(mkChanges([]));
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("No file changes in this chat yet")).toBeInTheDocument();
  });

  it("a chat folder without a repository: no git tabs, an honest line, no git diff fetched", async () => {
    store["mc.chat.changestab"] = "worktree"; // remembered from a repo chat
    changesMock.mockResolvedValue(mkChanges([mkFile()], null));
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByTestId("diff-no-repo")).toHaveTextContent(
      "This chat's folder is not a git repository",
    );
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(screen.queryByTestId("diff-source")).not.toBeInTheDocument();
    expect(await screen.findByTestId("chat-change-row")).toBeInTheDocument();
    expect(diffMock).not.toHaveBeenCalled();
  });

  it("a failed changes request says so, without a raw error", async () => {
    changesMock.mockRejectedValue(new Error("API 500: boom"));
    renderWithQuery(<DiffPanel agentId="agent-1" />);

    expect(await screen.findByText("Couldn't load this chat's changes.")).toBeInTheDocument();
  });

  it("a turn end refetches the chat's list too", async () => {
    const { rerenderWithQuery } = renderWithQuery(<DiffPanel agentId="agent-1" refreshHot={true} />);
    await waitFor(() => expect(changesMock).toHaveBeenCalledTimes(1));

    rerenderWithQuery(<DiffPanel agentId="agent-1" refreshHot={false} />);

    await waitFor(() => expect(changesMock).toHaveBeenCalledTimes(2));
  });
});
