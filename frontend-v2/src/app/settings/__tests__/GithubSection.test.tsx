import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { GithubStatus, GithubConfigStatus } from "@/lib/types";

// Deep-link straight into ?section=github so only GithubSection mounts
// (avoids having to also stub ProfileSection's own queries).
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/settings",
  useSearchParams: () => new URLSearchParams("section=github"),
}));

// The real zustand store uses persist middleware — a plain selector-mock
// dodges the jsdom localStorage write (same convention as
// runtimes/__tests__/HostsSection.test.tsx and loops/__tests__/LoopsPage.test.tsx).
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

const CONFIGURED_STATUS: GithubStatus = {
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
};

describe("GithubSection (Settings)", () => {
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

    // Generic fetch stub for any unmocked api call (Sidebar/AppShell chrome).
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
  });

  it("renders the connection status and prefills owner/token badges", async () => {
    vi.spyOn(api.repos, "githubStatus").mockResolvedValue(CONFIGURED_STATUS);

    renderPage();

    expect(await screen.findByRole("heading", { name: "GitHub" })).toBeInTheDocument();
    expect(await screen.findByText("Not tested")).toBeInTheDocument();
    expect(screen.getAllByText("acme").length).toBeGreaterThan(0);
    expect(screen.getByText("Set ••••")).toBeInTheDocument();
    expect(screen.getAllByText("App").length).toBeGreaterThan(0); // vault → "App" badge
  });

  it("saves a rotated token via PUT github-config and shows an inline success message", async () => {
    vi.spyOn(api.repos, "githubStatus").mockResolvedValue(CONFIGURED_STATUS);
    const saveSpy = vi.spyOn(api.repos, "setGithubConfig").mockResolvedValue({
      owner: "acme",
      owner_source: "vault",
      token_set: true,
      token_source: "vault",
      configured: true,
    } as GithubConfigStatus);

    renderPage();

    await screen.findByRole("heading", { name: "GitHub" });
    await userEvent.type(await screen.findByLabelText("GitHub token"), "ghp_newtoken123");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(saveSpy).toHaveBeenCalledWith({ token: "ghp_newtoken123" }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("test connection shows a live probe result in the status card", async () => {
    vi.spyOn(api.repos, "githubStatus").mockImplementation(async (probe?: boolean) =>
      probe
        ? { ...CONFIGURED_STATUS, connected: true, login: "acme-bot", owner_type: "Organization", rate_limit_remaining: 4999, rate_limit_total: 5000 }
        : CONFIGURED_STATUS
    );

    renderPage();

    await screen.findByRole("heading", { name: "GitHub" });
    await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));

    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("acme-bot")).toBeInTheDocument();
    expect(screen.getByText("4999/5000")).toBeInTheDocument();
  });
});
