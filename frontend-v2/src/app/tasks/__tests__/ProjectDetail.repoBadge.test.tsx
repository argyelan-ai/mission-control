/**
 * The repo badge in the project header links to the repository's web page —
 * but a local registry repo (``source="local"``, e.g. the first run's practice
 * repo) has none: its URL is a ``file://`` path on the server. Same rule as
 * the repo registry's ``web_url`` (backend services/repo_registry.web_url_for):
 * only http(s) URLs become a link; anything else is a plain label.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ProjectDetail } from "../ProjectDetail";
import type { Project } from "@/lib/types";

vi.mock("@/components/heads/useHeadRuns", () => ({
  useHeadRuns: () => ({ runs: [], byTask: new Map(), archivedCount: 0, isLoading: false, error: null }),
}));

function mkProject(overrides: Partial<Project> = {}): Project {
  return {
    id: "proj-1", board_id: "board-1", name: "Project X", description: null,
    project_type: "feature", status: "active", priority: "medium", plan_summary: null,
    progress_pct: 0, github_repo_url: null, github_repo_name: null, workspace_path: null,
    project_config: null, created_by: "user-1", started_at: null, completed_at: null,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  } as Project;
}

function renderDetail(project: Project) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ProjectDetail project={project} tasks={[]} agents={[]} boardId="board-1" tags={[]} onTaskClick={vi.fn()} />
    </QueryClientProvider>,
  );
}

describe("ProjectDetail — repo badge", () => {
  it("a GitHub repo is a link to its page", () => {
    renderDetail(mkProject({ github_repo_url: "https://github.com/acme/app", github_repo_name: "acme/app" }));
    const link = screen.getByRole("link", { name: /acme\/app/ });
    expect(link).toHaveAttribute("href", "https://github.com/acme/app");
  });

  it("a local repo shows its name but is not a link to a file:// path", () => {
    renderDetail(
      mkProject({ github_repo_url: "file:///srv/mc/repos/mc-practice.git", github_repo_name: "local/mc-practice" }),
    );
    expect(screen.getByText("local/mc-practice")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /mc-practice/ })).not.toBeInTheDocument();
    expect(document.querySelector('a[href^="file:"]')).toBeNull();
  });
});
