/**
 * CreateTaskModal — Repo-Auswahl (ADR-052).
 *
 * Die Task-Maske hat eine EINHEITLICHE Repo-Auswahl aus der Repo-Registry.
 * Der alte "Eigenes Repo"-Toggle ist weg — Ad-hoc-Tasks wählen ein
 * Registry-Repo (oder keins), Projekt-Tasks zeigen ein Rules-Badge und
 * können ein bestehendes Repo verknüpfen.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CreateTaskModal } from "../CreateTaskModal";
import { api } from "@/lib/api";
import type { Project, Repo, Task } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function mkRepo(overrides: Partial<Repo> = {}): Repo {
  return {
    id: "repo-1",
    full_name: "acme/tool",
    url: "https://github.com/acme/tool",
    default_branch: "main",
    description: null,
    rules_md: null,
    visibility: "private",
    is_active: true,
    source: "mc",
    last_synced_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    linked_projects: [],
    ...overrides,
  };
}

function mkProject(overrides: Partial<Project> = {}): Project {
  return {
    id: "project-1",
    board_id: "board-1",
    name: "Acme Project",
    description: null,
    project_type: "feature",
    status: "active",
    priority: "medium",
    plan_summary: null,
    progress_pct: 0,
    github_repo_url: null,
    github_repo_name: null,
    workspace_path: null,
    project_config: null,
    created_by: "user-1",
    started_at: null,
    completed_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

async function openModal() {
  await userEvent.click(screen.getByRole("button", { name: "New task" }));
}

describe("CreateTaskModal — Repo-Auswahl (ADR-052)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // Generic fetch stub for any unmocked call.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    vi.spyOn(api.projects, "list").mockResolvedValue([]);
    vi.spyOn(api.credentials, "list").mockResolvedValue([]);
    vi.spyOn(api.repos, "list").mockResolvedValue([]);
  });

  it("(a) defaults to 'no repository' with no toggle in the UI anymore", async () => {
    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();

    const select = (await screen.findByRole("combobox", { name: "Repository" })) as HTMLSelectElement;
    expect(select.value).toBe("");
    expect(screen.getByText("No repository (default)")).toBeInTheDocument();

    // The old checkbox toggle must be gone entirely.
    expect(screen.queryByRole("checkbox", { name: /Eigenes Repo/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Eigenes Repo/)).not.toBeInTheDocument();
  });

  it("(b) selecting a registry repo sends repo_id in the create call", async () => {
    const repo = mkRepo({ id: "repo-42", full_name: "acme/widgets" });
    vi.spyOn(api.repos, "list").mockResolvedValue([repo]);
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-1" } as Task);

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();

    await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Ad-hoc mit Repo");

    const select = await screen.findByRole("combobox", { name: "Repository" });
    await userEvent.selectOptions(select, repo.id);

    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(createSpy).toHaveBeenCalled());
    const [, body] = createSpy.mock.calls[0];
    expect(body).toMatchObject({ repo_id: repo.id });
  });

  it("(c) a project with repo rules shows the Rules-Badge", async () => {
    const project = mkProject();
    vi.spyOn(api.projects, "list").mockResolvedValue([project]);
    vi.spyOn(api.projects, "gitInfo").mockResolvedValue({
      has_repo: true,
      repo_name: "acme/tool",
      repo_url: "https://github.com/acme/tool",
      branches: ["main"],
      repo_id: "repo-1",
      has_rules: true,
    });

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();

    await userEvent.click(screen.getByText("Project (optional)"));
    await userEvent.click(await screen.findByText(project.name));

    expect(await screen.findByText("Rules active")).toBeInTheDocument();
  });

  it("(c2) a project without repo rules shows the neutral hint instead", async () => {
    const project = mkProject();
    vi.spyOn(api.projects, "list").mockResolvedValue([project]);
    vi.spyOn(api.projects, "gitInfo").mockResolvedValue({
      has_repo: true,
      repo_name: "acme/tool",
      repo_url: "https://github.com/acme/tool",
      branches: ["main"],
      repo_id: "repo-1",
      has_rules: false,
    });

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();

    await userEvent.click(screen.getByText("Project (optional)"));
    await userEvent.click(await screen.findByText(project.name));

    expect(await screen.findByText("No repo rules yet")).toBeInTheDocument();
    expect(screen.queryByText("Rules active")).not.toBeInTheDocument();
  });

  it("(d) 'Link existing repo' calls linkProject with the chosen repo and project", async () => {
    const project = mkProject();
    const repo = mkRepo({ id: "repo-9", full_name: "acme/legacy" });
    vi.spyOn(api.projects, "list").mockResolvedValue([project]);
    vi.spyOn(api.repos, "list").mockResolvedValue([repo]);
    vi.spyOn(api.projects, "gitInfo").mockResolvedValue({
      has_repo: false,
      repo_name: null,
      repo_url: null,
      branches: [],
      repo_id: null,
      has_rules: false,
    });
    const linkSpy = vi.spyOn(api.repos, "linkProject").mockResolvedValue(repo);

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();

    await userEvent.click(screen.getByText("Project (optional)"));
    await userEvent.click(await screen.findByText(project.name));

    await userEvent.click(await screen.findByText("Link existing repo"));
    const linkSelect = await screen.findByRole("combobox", { name: "Link existing repository" });
    await userEvent.selectOptions(linkSelect, repo.id);
    await userEvent.click(screen.getByRole("button", { name: "Link" }));

    await waitFor(() => expect(linkSpy).toHaveBeenCalledWith(repo.id, project.id));
  });
});

describe("CreateTaskModal — Human review default (05.07.)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    vi.spyOn(api.projects, "list").mockResolvedValue([]);
    vi.spyOn(api.credentials, "list").mockResolvedValue([]);
    vi.spyOn(api.repos, "list").mockResolvedValue([]);
  });

  it("defaults new tasks to human_review_required: true in the create call", async () => {
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-1" } as Task);

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();

    await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Default review task");
    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(createSpy).toHaveBeenCalled());
    const [, body] = createSpy.mock.calls[0];
    expect(body).toMatchObject({ human_review_required: true });
  });

  it("sends human_review_required: false when the pill is toggled off", async () => {
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-1" } as Task);

    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);
    await openModal();

    await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Agent-reviewed task");
    await userEvent.click(screen.getByRole("button", { name: /erweitert/i }));
    await userEvent.click(screen.getByRole("button", { name: /human review/i }));
    await userEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(createSpy).toHaveBeenCalled());
    const [, body] = createSpy.mock.calls[0];
    expect(body).toMatchObject({ human_review_required: false });
  });
});
