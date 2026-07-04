import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Repo, RepoImportCandidate } from "@/lib/types";

// next/navigation is mocked so AppShell's auth guard + Sidebar/MobileNav render
// without a real Next router (same convention as FilesPage.test.tsx).
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/repos",
}));

import ReposPage from "../page";

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ReposPage />
    </QueryClientProvider>
  );
}

const makeRepo = (over: Partial<Repo> = {}): Repo => ({
  id: "repo-1",
  full_name: "acme/mc-workspace",
  url: "https://github.com/acme/mc-workspace",
  default_branch: "main",
  description: "Shared ad-hoc workspace repo",
  rules_md: null,
  visibility: "private",
  is_active: true,
  source: "mc",
  last_synced_at: "2026-07-01T00:00:00Z",
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
  linked_projects: [],
  ...over,
});

describe("ReposPage", () => {
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
  });

  it("shows the empty state and an import CTA when there are 0 repos", async () => {
    vi.spyOn(api.repos, "list").mockResolvedValue([]);

    renderPage();

    expect(await screen.findByRole("heading", { name: "Repos" })).toBeInTheDocument();
    expect(await screen.findByText("Noch keine Repos registriert")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Repo importieren/ }).length).toBeGreaterThan(0);
  });

  it("renders repo cards with visibility badge, branch, rules indicator and linked project chips", async () => {
    vi.spyOn(api.repos, "list").mockResolvedValue([
      makeRepo(),
      makeRepo({
        id: "repo-2",
        full_name: "acme/data-pipeline",
        visibility: "public",
        rules_md: "# Regeln\n- immer Tests schreiben",
        linked_projects: [
          { id: "p1", name: "Report Composer", status: "active", board_id: "b1" },
        ],
      }),
    ]);

    renderPage();

    expect(await screen.findByText("acme/mc-workspace")).toBeInTheDocument();
    expect(screen.getByText("acme/data-pipeline")).toBeInTheDocument();

    // Visibility badges
    expect(screen.getByText("Private")).toBeInTheDocument();
    expect(screen.getByText("Public")).toBeInTheDocument();

    // Rules indicator differs per repo
    expect(screen.getByText("Keine Regeln")).toBeInTheDocument();
    expect(screen.getByText("Regeln ✓")).toBeInTheDocument();

    // Linked project chip
    expect(screen.getByText("Report Composer")).toBeInTheDocument();
  });

  it("opens the repo detail panel when a card is clicked", async () => {
    vi.spyOn(api.repos, "list").mockResolvedValue([makeRepo()]);
    vi.spyOn(api.repos, "get").mockResolvedValue(makeRepo());

    renderPage();

    const card = await screen.findByText("acme/mc-workspace");
    await userEvent.click(card);

    // Rules editor from RepoDetailPanel appears once the repo loads into the drawer
    expect(await screen.findByLabelText(/Arbeitsregeln/)).toBeInTheDocument();
  });

  it("opens the import dialog and lists GitHub repos not yet registered", async () => {
    vi.spyOn(api.repos, "list").mockResolvedValue([]);
    const candidates: RepoImportCandidate[] = [
      {
        full_name: "acme/new-repo",
        url: "https://github.com/acme/new-repo",
        description: "A fresh repo",
        visibility: "private",
        default_branch: "main",
        is_archived: false,
        pushed_at: "2026-07-03T00:00:00Z",
      },
    ];
    vi.spyOn(api.repos, "importCandidates").mockResolvedValue(candidates);

    renderPage();

    await userEvent.click((await screen.findAllByRole("button", { name: /Repo importieren/ }))[0]);

    expect(await screen.findByText("acme/new-repo")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Importieren" })).toBeInTheDocument();
  });

  it("registers a repo via the import dialog and shows it as imported", async () => {
    vi.spyOn(api.repos, "list").mockResolvedValue([]);
    vi.spyOn(api.repos, "importCandidates").mockResolvedValue([
      {
        full_name: "acme/new-repo",
        url: "https://github.com/acme/new-repo",
        description: null,
        visibility: "private",
        default_branch: "main",
        is_archived: false,
        pushed_at: null,
      },
    ]);
    const registerSpy = vi.spyOn(api.repos, "register").mockResolvedValue(makeRepo({ full_name: "acme/new-repo" }));

    renderPage();

    await userEvent.click((await screen.findAllByRole("button", { name: /Repo importieren/ }))[0]);
    await userEvent.click(await screen.findByRole("button", { name: "Importieren" }));

    await waitFor(() => expect(registerSpy).toHaveBeenCalledWith("acme/new-repo"));
    expect(await screen.findByText("Importiert")).toBeInTheDocument();
  });
});
