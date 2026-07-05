import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ProviderTemplate, SecretEntry } from "@/lib/types";

// Deep-link straight into ?section=apikeys so only ApiKeysSection mounts
// (same convention as GithubSection.test.tsx).
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/settings",
  useSearchParams: () => new URLSearchParams("section=apikeys"),
}));

const mockAppState = vi.hoisted(() => ({
  state: {
    activeBoardId: null as string | null,
    sidebarCollapsed: false,
    commandPaletteOpen: false,
    boards: [] as unknown[],
    boardGroups: [] as unknown[],
    currentUser: { id: "u1", email: "a@b.com", name: "Admin", role: "admin" } as {
      id: string; email: string; name: string; role: string;
    } | null,
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

import SettingsPage from "../page";

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsPage />
    </QueryClientProvider>
  );
}

const PROVIDERS: ProviderTemplate[] = [
  {
    provider: "anthropic",
    key: "anthropic_api_key",
    label: "Anthropic API Key",
    description: "For Claude directly via Anthropic",
    placeholder: "sk-ant-...",
  },
  {
    provider: "github",
    key: "github_token",
    label: "GitHub Personal Access Token",
    description: "For the agent git workflow",
    placeholder: "ghp_...",
  },
  {
    provider: "github",
    key: "github_owner",
    label: "GitHub Owner",
    description: "GitHub user/org MC creates project repos under",
    placeholder: "my-github-user",
  },
];

const GITHUB_SECRET: SecretEntry = {
  id: "s1",
  key: "github_token",
  value_masked: "****abcd",
  provider: "github",
  label: "GitHub Personal Access Token",
  description: null,
  created_at: null,
  updated_at: null,
};

describe("ApiKeysSection GitHub dedup (Settings)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mockAppState.state.currentUser = { id: "u1", email: "a@b.com", name: "Admin", role: "admin" };

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

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
  });

  it("renders a hint instead of editable GitHub cards when a GitHub secret exists", async () => {
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS);
    vi.spyOn(api.secrets, "list").mockResolvedValue([GITHUB_SECRET]);

    renderPage();

    expect(await screen.findByRole("heading", { name: "API Keys" })).toBeInTheDocument();

    // Non-GitHub provider still renders normally.
    expect(await screen.findByText("Anthropic API Key")).toBeInTheDocument();

    // No editable GitHub cards (labels from the provider templates are absent).
    expect(screen.queryByText("GitHub Personal Access Token")).not.toBeInTheDocument();
    expect(screen.queryByText("GitHub Owner")).not.toBeInTheDocument();

    // Single dedicated hint entry instead.
    expect(screen.getByText("GitHub credentials are managed in the GitHub section.")).toBeInTheDocument();
  });

  it("does not offer GitHub in the Add-key flow", async () => {
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS);
    vi.spyOn(api.secrets, "list").mockResolvedValue([]);

    renderPage();

    await screen.findByRole("heading", { name: "API Keys" });

    // Anthropic (not configured) still exposes an "Add" button.
    expect((await screen.findAllByRole("button", { name: "Add" })).length).toBeGreaterThan(0);
    // No GitHub template card is rendered at all when unconfigured.
    expect(screen.queryByText("GitHub Personal Access Token")).not.toBeInTheDocument();
    expect(screen.queryByText("GitHub Owner")).not.toBeInTheDocument();
  });

  it("clicking the hint's Go to GitHub button navigates to the GitHub section", async () => {
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS);
    vi.spyOn(api.secrets, "list").mockResolvedValue([GITHUB_SECRET]);
    vi.spyOn(api.repos, "githubStatus").mockResolvedValue({
      owner: "acme",
      owner_source: "vault",
      token_set: true,
      token_source: "vault",
      configured: true,
      connected: null,
      login: null,
      owner_type: null,
      rate_limit_remaining: null,
      rate_limit_total: null,
      error: null,
    });

    renderPage();

    await screen.findByRole("heading", { name: "API Keys" });
    await userEvent.click(await screen.findByRole("button", { name: /Go to GitHub/i }));

    expect(await screen.findByRole("heading", { name: "GitHub" })).toBeInTheDocument();
  });
});
