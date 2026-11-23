import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
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
    currentUser: { id: "u1", email: "a@b.com", name: "Admin", role: "admin" } as {
      id: string; email: string; name: string; role: string;
    } | null,
    setActiveBoardId: (id: string | null) => { mockAppState.state.activeBoardId = id; },
    toggleSidebar: () => {},
    setCommandPaletteOpen: (open: boolean) => { mockAppState.state.commandPaletteOpen = open; },
    setBoards: (boards: unknown[]) => { mockAppState.state.boards = boards; },
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

const user = (id: string, name: string, active: boolean) => ({ ...OTHER_USER, id, name, email: `${id}@example.com`, is_active: active });

describe("Settings › Users — deactivated accounts fold away", () => {
  beforeEach(() => {
    nav.section = "users";
    vi.spyOn(api.auth.users, "list").mockResolvedValue([
      user("u2", "Beta", true),
      user("u3", "Gamma", false),
      user("u4", "Delta", false),
      user("u5", "Epsilon", false),
    ] as never);
  });

  it("shows active accounts and folds the deactivated ones into one line", async () => {
    renderPage();
    expect(await screen.findByText("Beta", {}, { timeout: 5000 })).toBeInTheDocument();
    const fold = screen.getByRole("button", { name: "3 deactivated" });
    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Gamma")).toBeNull();
    await userEvent.click(fold);
    expect(fold).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Gamma")).toBeInTheDocument();
  });
});

describe("Settings › Costs — same head as every section, the table gets the width", () => {
  beforeEach(() => {
    nav.section = "costs";
    vi.spyOn(api.modelPrices, "list").mockResolvedValue([
      {
        id: "p1", pattern: "model-a*", input_per_m: 1, output_per_m: 2, cache_read_per_m: 0.1,
        cache_write_per_m: 1.25, priority: 90, valid_from: "2020-01-01T00:00:00Z", note: "note",
      },
    ] as never);
    vi.spyOn(api.modelPrices, "unmatched").mockResolvedValue([] as never);
  });

  it("opens with the section head (h2 Costs) and drops the reading-width cap", async () => {
    renderPage();
    const head = await screen.findByRole("heading", { level: 2, name: "Costs" }, { timeout: 5000 });
    expect(head).toHaveClass("text-base");
    expect(screen.getByTestId("settings-section-body")).not.toHaveClass("max-w-3xl");
  });

  it("keeps the date on one line", async () => {
    renderPage();
    const date = await screen.findByText("2020-01-01", {}, { timeout: 5000 });
    expect(date.closest("td")).toHaveClass("whitespace-nowrap");
  });
});

describe("Settings › Language — head like the others", () => {
  it("uses the section head size, not a second page title", async () => {
    nav.section = "language";
    vi.spyOn(api.settings, "agentLanguage").mockResolvedValue({ defaults: { operator_language: "en", work_language: "en" } } as never);
    renderPage();
    const head = await screen.findByRole("heading", { level: 2, name: "Language" }, { timeout: 5000 });
    expect(head).toHaveClass("text-base");
    expect(head).not.toHaveClass("display");
  });
});

describe("Settings › About — which build runs", () => {
  it("shows the commit the frontend was built from, when the build names it", async () => {
    nav.section = "about";
    vi.stubEnv("NEXT_PUBLIC_GIT_COMMIT", "225f4486e9cb8fe264963c6fa5537269daf5ffb9");
    vi.spyOn(api.system, "version").mockResolvedValue({ current: "dev", latest: null, release_url: null, update_available: false } as never);
    renderPage();
    const row = await screen.findByTestId("about-commit", {}, { timeout: 5000 });
    expect(row).toHaveTextContent("Commit");
    expect(row).toHaveTextContent("225f448");
    vi.unstubAllEnvs();
  });

  it("leaves the row out when the build does not name a commit (K3)", async () => {
    nav.section = "about";
    vi.stubEnv("NEXT_PUBLIC_GIT_COMMIT", "");
    vi.spyOn(api.system, "version").mockResolvedValue({ current: "dev", latest: null, release_url: null, update_available: false } as never);
    renderPage();
    await screen.findByText("dev", {}, { timeout: 5000 });
    expect(screen.queryByTestId("about-commit")).toBeNull();
    vi.unstubAllEnvs();
  });
});
