/**
 * Changes panel actions (part 3): Revert per file, Commit, Open PR —
 * each behind one confirmation sheet.
 *
 * The guards that matter on this side: nothing is sent before the operator
 * taps the sheet's own button (Cancel sends nothing), a refusal is shown as
 * the translated sentence for its code (never raw JSON), and Commit / Open PR
 * appear only when the backend says they can run. Labels come from the real
 * EN catalog (test-setup's next-intl mock).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DiffPanel, chatFileFor } from "./DiffPanel";
import { actionReason } from "./ChangesActions";
import { api } from "@/lib/api";
import type { ChatDiffSource, CommitDiff, CommitDiffFile } from "@/lib/types";
import type { ChatChanges, ChatChangeFile } from "@/lib/chatTypes";

vi.mock("@/lib/api", () => ({
  api: {
    chat: {
      diff: vi.fn(),
      changes: vi.fn(),
      revertFile: vi.fn(),
      commitDraft: vi.fn(),
      commit: vi.fn(),
      prDraft: vi.fn(),
      openPr: vi.fn(),
    },
  },
}));

const chat = vi.mocked(api.chat);

const REPO: ChatDiffSource = { kind: "session", repo: "shop", branch: "feat/prices", path: "/workspace/shop" };

function mkFile(overrides: Partial<ChatChangeFile> = {}): ChatChangeFile {
  return {
    path: "app/prices.py",
    additions: 4,
    deletions: 2,
    edits: 3,
    created: false,
    written: false,
    lastAt: null,
    diff: { path: "app/prices.py", kind: "edit", lines: [{ t: "add", text: "x" }], additions: 4, deletions: 2, truncated: false },
    ...overrides,
  };
}

function mkChanges(over: Partial<ChatChanges> = {}): ChatChanges {
  return {
    files: [mkFile()],
    stats: { files: 1, additions: 4, deletions: 2 },
    repo: REPO,
    actions: { commit: { ok: true, reason: null, files: 1 }, pr: { ok: true, reason: null } },
    ...over,
  };
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <DiffPanel agentId="agent-1" />
    </QueryClientProvider>,
  );
}

let store: Record<string, string>;

beforeEach(() => {
  vi.clearAllMocks();
  chat.changes.mockResolvedValue(mkChanges());
  store = {};
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => {
        store[k] = v;
      },
      removeItem: (k: string) => {
        delete store[k];
      },
      clear: () => {
        store = {};
      },
      length: 0,
      key: () => null,
    },
  });
});

async function openRevertSheet(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByTestId("chat-change-row"));
  await user.click(screen.getByTestId("chat-change-revert"));
  return screen.findByTestId("revert-sheet");
}

describe("Revert file", () => {
  it("asks first — the sheet names the file and how many edits it undoes", async () => {
    const user = userEvent.setup();
    renderPanel();
    await openRevertSheet(user);

    expect(screen.getByText("Revert app/prices.py?")).toBeInTheDocument();
    expect(screen.getByText("This undoes 3 edits by this chat.")).toBeInTheDocument();
    expect(chat.revertFile).not.toHaveBeenCalled();
  });

  it("Cancel sends nothing", async () => {
    const user = userEvent.setup();
    renderPanel();
    await openRevertSheet(user);
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByTestId("revert-sheet")).not.toBeInTheDocument());
    expect(chat.revertFile).not.toHaveBeenCalled();
  });

  it("the sheet's Revert reverts exactly that file and says so", async () => {
    chat.revertFile.mockResolvedValue({ status: "reverted", path: "app/prices.py", method: "chat", deleted: false, edits: 3 });
    const user = userEvent.setup();
    renderPanel();
    await openRevertSheet(user);
    await user.click(screen.getByTestId("revert-sheet-confirm"));

    expect(chat.revertFile).toHaveBeenCalledWith("agent-1", "app/prices.py");
    expect(await screen.findByText("prices.py is back to its state before this chat.")).toBeInTheDocument();
    expect(chat.changes).toHaveBeenCalledTimes(2); // list refreshed after the act
  });

  it("a file the chat created says that reverting deletes it", async () => {
    chat.changes.mockResolvedValue(mkChanges({ files: [mkFile({ created: true, edits: 1 })] }));
    const user = userEvent.setup();
    renderPanel();
    await openRevertSheet(user);
    expect(screen.getByText("This chat created the file — reverting deletes it.")).toBeInTheDocument();
  });

  it("a refusal is the translated sentence, never raw JSON", async () => {
    chat.revertFile.mockRejectedValue(new Error('API 409: {"reason":"changed_externally"}'));
    const user = userEvent.setup();
    renderPanel();
    await openRevertSheet(user);
    await user.click(screen.getByTestId("revert-sheet-confirm"));

    expect(
      await screen.findByText("The file changed after this chat's last edit, so MC leaves it alone."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/changed_externally/)).not.toBeInTheDocument();
  });

  it("a permanent refusal (changed_externally) leaves only Close — Revert can never succeed", async () => {
    chat.revertFile.mockRejectedValue(new Error('API 409: {"reason":"changed_externally"}'));
    const user = userEvent.setup();
    renderPanel();
    await openRevertSheet(user);
    await user.click(screen.getByTestId("revert-sheet-confirm"));

    await screen.findByText("The file changed after this chat's last edit, so MC leaves it alone.");
    expect(screen.queryByTestId("revert-sheet-confirm")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });

  it("another permanent refusal (path outside the chat's folder) also drops to Close only", async () => {
    chat.revertFile.mockRejectedValue(new Error('API 400: {"reason":"path_outside"}'));
    const user = userEvent.setup();
    renderPanel();
    await openRevertSheet(user);
    await user.click(screen.getByTestId("revert-sheet-confirm"));

    await screen.findByText("This file is outside the chat's folder.");
    expect(screen.queryByTestId("revert-sheet-confirm")).not.toBeInTheDocument();
  });

  it("a transient refusal (agent busy) keeps Try again", async () => {
    chat.revertFile.mockRejectedValue(new Error('API 409: {"reason":"agent_busy"}'));
    const user = userEvent.setup();
    renderPanel();
    await openRevertSheet(user);
    await user.click(screen.getByTestId("revert-sheet-confirm"));

    await screen.findByText("The agent is working. Try again when its turn ends.");
    expect(screen.getByTestId("revert-sheet-confirm")).toBeInTheDocument();
  });

  it("a network error (no parsed reason) also keeps Try again", async () => {
    chat.revertFile.mockRejectedValue(new Error("Failed to fetch"));
    const user = userEvent.setup();
    renderPanel();
    await openRevertSheet(user);
    await user.click(screen.getByTestId("revert-sheet-confirm"));

    await screen.findByText("That didn't work.");
    expect(screen.getByTestId("revert-sheet-confirm")).toBeInTheDocument();
  });

  it("a file MC already put back shows 'Reverted' instead of the button", async () => {
    chat.changes.mockResolvedValue(mkChanges({ files: [mkFile({ reverted: true })] }));
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId("chat-change-row"));
    expect(screen.getByTestId("chat-change-reverted")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-change-revert")).not.toBeInTheDocument();
  });

  it("the Uncommitted tab offers Revert only on files this chat changed", async () => {
    store["mc.chat.changestab"] = "worktree";
    const hunks = [{ header: "@@ -1 +1 @@", lines: [{ type: "add" as const, content: "x", old_no: null, new_no: 1 }] }];
    const diff: CommitDiff = {
      hash: "", message: "", author: "", date: "", stats: { files: 2, additions: 2, deletions: 0 },
      files: [
        { filename: "app/prices.py", additions: 1, deletions: 0, hunks },
        { filename: "README.md", additions: 1, deletions: 0, hunks },
      ],
    };
    chat.diff.mockResolvedValue(diff);
    renderPanel();

    const buttons = await screen.findAllByTestId("diff-file-revert");
    expect(buttons).toHaveLength(1);
  });
});

describe("Commit and Open PR", () => {
  it("are offered only when the backend says they can run", async () => {
    chat.changes.mockResolvedValue(
      mkChanges({ actions: { commit: { ok: false, reason: "nothing_to_commit" }, pr: { ok: false, reason: "default_branch" } } }),
    );
    renderPanel();
    await screen.findByTestId("chat-change-row");
    expect(screen.queryByTestId("changes-commit")).not.toBeInTheDocument();
    expect(screen.queryByTestId("changes-open-pr")).not.toBeInTheDocument();
  });

  it("a chat without a repository has no footer at all", async () => {
    chat.changes.mockResolvedValue(mkChanges({ repo: null, actions: undefined }));
    renderPanel();
    await screen.findByTestId("chat-change-row");
    expect(screen.queryByTestId("changes-commit")).not.toBeInTheDocument();
  });

  it("Commit shows the suggested message, lets it be edited, and commits only on the sheet's button", async () => {
    chat.commitDraft.mockResolvedValue({
      branch: "feat/prices",
      files: [{ path: "app/prices.py", status: "modified", additions: 4, deletions: 2 }],
      message: "chore: update prices.py\n\n- app/prices.py (+4 -2)\n",
    });
    chat.commit.mockResolvedValue({ status: "committed", branch: "feat/prices", sha: "abc1234", subject: "fix: prices" });
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId("changes-commit"));

    expect(await screen.findByText("Commit 1 file on feat/prices?")).toBeInTheDocument();
    const box = screen.getByTestId("commit-message") as HTMLTextAreaElement;
    expect(box.value).toBe("chore: update prices.py\n\n- app/prices.py (+4 -2)\n");
    expect(chat.commit).not.toHaveBeenCalled();

    await user.clear(box);
    await user.type(box, "fix: prices");
    await user.click(screen.getByTestId("commit-sheet-confirm"));

    expect(chat.commit).toHaveBeenCalledWith("agent-1", "fix: prices");
    expect(await screen.findByText("abc1234 is on feat/prices.")).toBeInTheDocument();
  });

  it("shows a quiet hint when the message came from the local model, and none for old cached drafts", async () => {
    chat.commitDraft.mockResolvedValue({
      branch: "feat/prices",
      files: [{ path: "app/prices.py", status: "modified", additions: 4, deletions: 2 }],
      message: "feat(prices): round to two decimals\n\n- app/prices.py\n",
      source: "llm",
      mechanicalMessage: "chore: update prices.py\n\n- app/prices.py (+4 -2)\n",
    });
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId("changes-commit"));

    const box = await screen.findByTestId("commit-message") as HTMLTextAreaElement;
    expect(box.value).toBe("feat(prices): round to two decimals\n\n- app/prices.py\n");
    expect(await screen.findByTestId("commit-message-source")).toHaveTextContent("Suggested by the local model.");
  });

  it("shows the fallback hint when the local model didn't answer", async () => {
    chat.commitDraft.mockResolvedValue({
      branch: "feat/prices",
      files: [{ path: "app/prices.py", status: "modified", additions: 4, deletions: 2 }],
      message: "chore: update prices.py\n\n- app/prices.py (+4 -2)\n",
      source: "fallback",
    });
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId("changes-commit"));

    expect(await screen.findByTestId("commit-message-source")).toHaveTextContent(
      "Simple summary — the local model didn't answer in time.",
    );
  });

  it("has no source hint at all for an old cached draft without the field", async () => {
    chat.commitDraft.mockResolvedValue({
      branch: "feat/prices",
      files: [{ path: "app/prices.py", status: "modified", additions: 4, deletions: 2 }],
      message: "chore: update prices.py\n\n- app/prices.py (+4 -2)\n",
    });
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId("changes-commit"));
    await screen.findByTestId("commit-message");

    expect(screen.queryByTestId("commit-message-source")).not.toBeInTheDocument();
  });

  it("↻ suggest again re-fetches the draft and replaces the message with the new suggestion", async () => {
    chat.commitDraft
      .mockResolvedValueOnce({
        branch: "feat/prices",
        files: [{ path: "app/prices.py", status: "modified", additions: 4, deletions: 2 }],
        message: "chore: update prices.py\n\n- app/prices.py (+4 -2)\n",
        source: "fallback",
      })
      .mockResolvedValueOnce({
        branch: "feat/prices",
        files: [{ path: "app/prices.py", status: "modified", additions: 4, deletions: 2 }],
        message: "feat(prices): round to two decimals\n\n- app/prices.py\n",
        source: "llm",
      });
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId("changes-commit"));

    const box = await screen.findByTestId("commit-message") as HTMLTextAreaElement;
    expect(box.value).toBe("chore: update prices.py\n\n- app/prices.py (+4 -2)\n");

    await user.click(screen.getByTestId("commit-suggest-again"));

    await waitFor(() => expect(chat.commitDraft).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect((screen.getByTestId("commit-message") as HTMLTextAreaElement).value).toBe(
        "feat(prices): round to two decimals\n\n- app/prices.py\n",
      ),
    );
    expect(await screen.findByTestId("commit-message-source")).toHaveTextContent("Suggested by the local model.");
  });

  it("shows a loading skeleton for the message while the first draft is pending", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof chat.commitDraft>>) => void;
    chat.commitDraft.mockReturnValue(new Promise((r) => { resolve = r; }));
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId("changes-commit"));

    expect(await screen.findByTestId("commit-message-skeleton")).toBeInTheDocument();
    expect(screen.queryByTestId("commit-message")).not.toBeInTheDocument();

    resolve({
      branch: "feat/prices",
      files: [{ path: "app/prices.py", status: "modified", additions: 4, deletions: 2 }],
      message: "chore: update prices.py\n\n- app/prices.py (+4 -2)\n",
      source: "fallback",
    });
    await screen.findByTestId("commit-message");
    expect(screen.queryByTestId("commit-message-skeleton")).not.toBeInTheDocument();
  });

  it("Open PR names branch and base, then shows the link — also after the sheet closes", async () => {
    chat.prDraft.mockResolvedValue({ branch: "feat/prices", base: "main", title: "fix: prices", ahead: 1, uncommitted: 2 });
    chat.openPr.mockResolvedValue({ status: "opened", url: "https://github.com/example/shop/pull/7", branch: "feat/prices", base: "main" });
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId("changes-open-pr"));

    expect(await screen.findByText("Open a pull request?")).toBeInTheDocument();
    expect(screen.getByText("2 uncommitted files are not part of it — commit first.")).toBeInTheDocument();
    expect((screen.getByTestId("pr-title") as HTMLInputElement).value).toBe("fix: prices");
    await user.click(screen.getByTestId("pr-sheet-confirm"));

    expect(chat.openPr).toHaveBeenCalledWith("agent-1", "fix: prices");
    expect(await screen.findByTestId("pr-link")).toHaveAttribute("href", "https://github.com/example/shop/pull/7");
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(await screen.findByTestId("changes-pr-link")).toHaveAttribute("href", "https://github.com/example/shop/pull/7");
  });

  it("a refused PR says why", async () => {
    chat.prDraft.mockRejectedValue(new Error('API 409: {"reason":"default_branch"}'));
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId("changes-open-pr"));
    expect(await screen.findByText("This is the main branch. The work belongs on its own branch.")).toBeInTheDocument();
    expect(screen.queryByTestId("pr-sheet-confirm")).not.toBeInTheDocument();
  });
});

describe("helpers", () => {
  it("actionReason reads the backend's reason out of the request error", () => {
    expect(actionReason(new Error('API 409: {"reason":"agent_busy"}'))).toEqual({ reason: "agent_busy", detail: undefined });
    expect(actionReason(new Error("API 500: Internal Server Error"))).toBeNull();
    expect(actionReason("nope")).toBeNull();
  });

  it("chatFileFor matches git paths to the chat's files across folder levels", () => {
    const f = (filename: string): CommitDiffFile => ({ filename, additions: 0, deletions: 0, hunks: [] });
    const changes = mkChanges({
      files: [mkFile({ path: "app/prices.py" }), mkFile({ path: "/workspace/shop/abs.py" }), mkFile({ path: "gone.py", reverted: true })],
    });
    expect(chatFileFor(f("app/prices.py"), changes, REPO)?.path).toBe("app/prices.py");
    expect(chatFileFor(f("shop2/app/prices.py"), changes, REPO)?.path).toBe("app/prices.py");
    expect(chatFileFor(f("abs.py"), changes, REPO)?.path).toBe("/workspace/shop/abs.py");
    expect(chatFileFor(f("prices.py"), changes, REPO)?.path).toBe("app/prices.py");
    expect(chatFileFor(f("other/prices2.py"), changes, REPO)).toBeUndefined();
    expect(chatFileFor(f("gone.py"), changes, REPO)).toBeUndefined();
  });
});
