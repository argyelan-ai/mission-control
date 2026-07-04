import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RepoDetailPanel } from "../RepoDetailPanel";
import { api } from "@/lib/api";
import type { Repo } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const makeRepo = (over: Partial<Repo> = {}): Repo => ({
  id: "repo-1",
  full_name: "owner/name",
  url: "https://github.com/owner/name",
  default_branch: "main",
  description: "A test repo",
  rules_md: null,
  visibility: "private",
  is_active: true,
  source: "mc",
  last_synced_at: null,
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
  linked_projects: [],
  ...over,
});

describe("RepoDetailPanel", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("loads the repo and shows the rules editor with the dispatch hint", async () => {
    vi.spyOn(api.repos, "get").mockResolvedValue(makeRepo());

    renderWithQuery(<RepoDetailPanel repoId="repo-1" open onClose={vi.fn()} />);

    expect(await screen.findByLabelText(/Working rules/)).toBeInTheDocument();
    expect(
      screen.getByText("These rules are included in every agent dispatch for this repo.")
    ).toBeInTheDocument();
    // Save is disabled until something actually changes
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("saves edited rules via PATCH and shows a confirmation", async () => {
    vi.spyOn(api.repos, "get").mockResolvedValue(makeRepo());
    const updateSpy = vi.spyOn(api.repos, "update").mockResolvedValue(makeRepo({ rules_md: "# Rules" }));

    renderWithQuery(<RepoDetailPanel repoId="repo-1" open onClose={vi.fn()} />);

    const textarea = await screen.findByLabelText(/Working rules/);
    await userEvent.type(textarea, "# Rules");

    const saveBtn = screen.getByRole("button", { name: "Save" });
    expect(saveBtn).toBeEnabled();
    await userEvent.click(saveBtn);

    await waitFor(() =>
      expect(updateSpy).toHaveBeenCalledWith("repo-1", { description: "A test repo", rules_md: "# Rules" })
    );
    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("shows the linked projects with an unlink control", async () => {
    vi.spyOn(api.repos, "get").mockResolvedValue(
      makeRepo({ linked_projects: [{ id: "p1", name: "Feature X", status: "active", board_id: "b1" }] })
    );

    renderWithQuery(<RepoDetailPanel repoId="repo-1" open onClose={vi.fn()} />);

    expect(await screen.findByText("Feature X")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unlink Feature X" })).toBeInTheDocument();
  });

  it("shows the backend's 409 error text when delete is blocked by linked projects", async () => {
    vi.spyOn(api.repos, "get").mockResolvedValue(
      makeRepo({ linked_projects: [{ id: "p1", name: "Feature X", status: "active", board_id: "b1" }] })
    );
    vi.spyOn(api.repos, "remove").mockRejectedValue(
      new Error('API 409: {"detail":"Repo ist mit Projekten verknüpft (Feature X) — erst entkoppeln"}')
    );

    renderWithQuery(<RepoDetailPanel repoId="repo-1" open onClose={vi.fn()} />);

    // Open the confirm dialog from the panel's danger zone
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));

    // The confirm dialog renders its own "Delete" button after the panel's trigger
    const deleteButtons = await screen.findAllByRole("button", { name: "Delete" });
    await userEvent.click(deleteButtons[deleteButtons.length - 1]);

    expect(
      await screen.findByText("Repo ist mit Projekten verknüpft (Feature X) — erst entkoppeln")
    ).toBeInTheDocument();
  });

  it("toggles archive state via the danger-zone button", async () => {
    vi.spyOn(api.repos, "get").mockResolvedValue(makeRepo({ is_active: true }));
    const updateSpy = vi.spyOn(api.repos, "update").mockResolvedValue(makeRepo({ is_active: false }));

    renderWithQuery(<RepoDetailPanel repoId="repo-1" open onClose={vi.fn()} />);

    await userEvent.click(await screen.findByRole("button", { name: /Archive/ }));

    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith("repo-1", { is_active: false }));
  });
});
