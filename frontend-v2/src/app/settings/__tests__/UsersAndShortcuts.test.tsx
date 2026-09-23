import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";

const nav = vi.hoisted(() => ({ section: "users" }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/settings",
  useSearchParams: () => new URLSearchParams(`section=${nav.section}`),
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
  useNotificationStore: Object.assign(
    (selector?: (s: { notifications: never[] }) => unknown) =>
      selector ? selector({ notifications: [] }) : { notifications: [] },
    { getState: () => ({ addNotification: vi.fn() }) }
  ),
  useAppStore: Object.assign(
    (selector?: (s: typeof mockAppState.state) => unknown) =>
      selector ? selector(mockAppState.state) : mockAppState.state,
    { setState: (partial: Partial<typeof mockAppState.state>) => Object.assign(mockAppState.state, partial) }
  ),
}));

import SettingsPage from "../page";

// The whole settings page mounts here; under a loaded parallel run it can take
// a few seconds, so give queries and tests headroom.
vi.setConfig({ testTimeout: 20_000 });

const OTHER_USER = {
  id: "u2",
  email: "beta@example.com",
  name: "Beta",
  role: "operator",
  is_active: true,
  has_password: true,
  created_at: "2026-01-01T00:00:00Z",
};

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

describe("Settings › Users — deactivating asks first", () => {
  beforeEach(() => {
    nav.section = "users";
    vi.spyOn(api.auth.users, "list").mockResolvedValue([OTHER_USER] as never);
  });

  it("does not deactivate on the first click; the dialog names the user", async () => {
    const update = vi.spyOn(api.auth.users, "update").mockResolvedValue({} as never);
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Deactivate" }, { timeout: 5000 }));
    expect(update).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog", {}, { timeout: 5000 });
    expect(dialog).toHaveTextContent("Beta");
  });

  it("cancel leaves the account active", async () => {
    const update = vi.spyOn(api.auth.users, "update").mockResolvedValue({} as never);
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Deactivate" }, { timeout: 5000 }));
    const dialog = await screen.findByRole("dialog", {}, { timeout: 5000 });
    await userEvent.click(within(dialog).getAllByRole("button", { name: "Cancel" }).at(-1)!);
    expect(update).not.toHaveBeenCalled();
  });

  it("confirming deactivates the account", async () => {
    const update = vi.spyOn(api.auth.users, "update").mockResolvedValue({} as never);
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Deactivate" }, { timeout: 5000 }));
    const dialog = await screen.findByRole("dialog", {}, { timeout: 5000 });
    await userEvent.click(within(dialog).getByRole("button", { name: "Deactivate user" }));
    expect(update).toHaveBeenCalledWith("u2", { is_active: false });
  });

  it("re-activating needs no confirmation (it restores access)", async () => {
    vi.spyOn(api.auth.users, "list").mockResolvedValue([{ ...OTHER_USER, is_active: false }] as never);
    const update = vi.spyOn(api.auth.users, "update").mockResolvedValue({} as never);
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Activate" }, { timeout: 5000 }));
    expect(update).toHaveBeenCalledWith("u2", { is_active: true });
  });
});

describe("Settings › Shortcuts — only shortcuts that exist", () => {
  it("does not list Cmd+N or Cmd+Shift+A (no handler implements them)", async () => {
    nav.section = "shortcuts";
    renderPage();
    await screen.findByText("Open command palette", {}, { timeout: 5000 });
    expect(screen.queryByText("New task")).not.toBeInTheDocument();
    expect(screen.queryByText("Approve all approvals")).not.toBeInTheDocument();
  });
});
