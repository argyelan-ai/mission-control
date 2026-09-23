/**
 * GitPanel — PR chip falls back to the task's own PR reference.
 *
 * The chip used to come only from the live /git-info probe. A task whose PR
 * the backend recorded on the task itself (task.pr_url / pr_number) showed no
 * PR link anywhere in the detail view.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { GitPanel, gitSectionInfo } from "../GitPanel";
import type { TaskGitInfo } from "@/lib/types";

vi.mock("@/lib/api", () => ({ api: { tasks: { gitDiff: vi.fn() } } }));

const gitInfo: TaskGitInfo = {
  branch: "main",
  last_commit: null,
  uncommitted: false,
  ahead: 0,
  workspace_path: null,
  commits: [],
  pr_url: null,
};

function renderPanel(props: Partial<React.ComponentProps<typeof GitPanel>> = {}) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <GitPanel gitInfo={gitInfo} boardId="b" taskId="t" {...props} />
    </QueryClientProvider>,
  );
}

describe("GitPanel PR chip", () => {
  it("links the task's recorded PR when the git probe has none", () => {
    renderPanel({ taskPrUrl: "https://github.com/acme/app/pull/632", taskPrNumber: 632 });
    const link = screen.getByRole("link", { name: /PR #632/ });
    expect(link).toHaveAttribute("href", "https://github.com/acme/app/pull/632");
  });

  it("prefers the live probe's PR URL when both exist", () => {
    renderPanel({
      gitInfo: { ...gitInfo, pr_url: "https://github.com/acme/app/pull/700" },
      taskPrUrl: "https://github.com/acme/app/pull/632",
    });
    const link = screen.getByRole("link", { name: /PR/ });
    expect(link).toHaveAttribute("href", "https://github.com/acme/app/pull/700");
  });

  it("shows no PR chip when neither source has one", () => {
    renderPanel();
    expect(screen.queryByRole("link", { name: /PR/ })).toBeNull();
  });

  it("shows the section with only a PR chip for a task that has a PR but no workspace", () => {
    const info = gitSectionInfo(undefined, "https://github.com/acme/app/pull/632");
    expect(info).not.toBeNull();
    renderPanel({ gitInfo: info!, taskPrUrl: "https://github.com/acme/app/pull/632", taskPrNumber: 632 });
    expect(screen.getByRole("link", { name: /PR #632/ })).toBeInTheDocument();
    expect(screen.queryByTestId("git-branch")).toBeNull();
  });

  it("hides the section when there is neither a branch nor a PR", () => {
    expect(gitSectionInfo(undefined, null)).toBeNull();
    expect(gitSectionInfo({ ...gitInfo, branch: null }, undefined)).toBeNull();
    expect(gitSectionInfo(gitInfo, null)).toBe(gitInfo);
  });
});
