import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// next/navigation is mocked so AppShell's auth guard + Sidebar/MobileNav render
// without a real Next router (same convention as LoopsPage.test.tsx).
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/runtimes",
}));

// The real zustand store uses persist middleware — a plain selector-mock
// dodges the jsdom localStorage write (same convention as
// runtimes/__tests__/HostsSection.test.tsx / LoopsPage.test.tsx).
const mockAppState = vi.hoisted(() => ({
  state: {
    activeBoardId: "board-1" as string | null,
    sidebarCollapsed: false,
    commandPaletteOpen: false,
    boards: [] as unknown[],
    boardGroups: [] as unknown[],
    currentUser: null as { id: string; email: string; name: string; role: string } | null,
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
  // AppShell mounts ToastRenderer, which reads notifications via this selector.
  useNotificationStore: (selector?: (s: { notifications: unknown[] }) => unknown) =>
    selector ? selector({ notifications: [] }) : { notifications: [] },
}));

// The tabs each own a deep tree of their own queries (OverviewTab/ModelsTab/
// AdminTab) — irrelevant to what this test verifies (tab switching + the
// download-count badge), so they're stubbed to a marker div. useDownloadCount
// is mocked directly so the badge can be driven without wiring the LM Studio
// downloads endpoint.
vi.mock("../OverviewTab", () => ({
  OverviewTab: () => <div data-testid="overview-tab-stub">Overview content</div>,
}));
vi.mock("../ModelsTab", () => ({
  ModelsTab: () => <div data-testid="models-tab-stub">Models content</div>,
}));
vi.mock("../AdminTab", () => ({
  AdminTab: () => <div data-testid="admin-tab-stub">Admin content</div>,
}));

const mockDownloadCount = vi.hoisted(() => ({ value: 0 }));
vi.mock("../useDownloadCount", () => ({
  useDownloadCount: () => mockDownloadCount.value,
}));

import RuntimesPage from "../page";

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <RuntimesPage />
    </QueryClientProvider>
  );
}

describe("RuntimesPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mockDownloadCount.value = 0;
    mockAppState.state.currentUser = null;

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

    // Generic fetch stub for anything AppShell's chrome (Sidebar, TopBar,
    // StatusBar, WorkspaceSwitcher, CommandPalette) fires off.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
  });

  it("renders the Overview tab by default", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Runtimes" })).toBeInTheDocument();
    expect(await screen.findByTestId("overview-tab-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("models-tab-stub")).not.toBeInTheDocument();
    expect(screen.queryByTestId("admin-tab-stub")).not.toBeInTheDocument();
  });

  it("switches content when clicking the Models and Administration tabs", async () => {
    renderPage();
    await screen.findByTestId("overview-tab-stub");

    await userEvent.click(screen.getByRole("button", { name: /Models/ }));
    expect(await screen.findByTestId("models-tab-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("overview-tab-stub")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Administration" }));
    expect(await screen.findByTestId("admin-tab-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("models-tab-stub")).not.toBeInTheDocument();
  });

  it("shows the download-count badge on the Models tab label when downloads are active", async () => {
    mockDownloadCount.value = 2;
    renderPage();

    const modelsButton = await screen.findByRole("button", { name: /Models/ });
    expect(modelsButton).toHaveTextContent("2");
  });

  it("hides the download-count badge when there are no active downloads", async () => {
    mockDownloadCount.value = 0;
    renderPage();

    const modelsButton = await screen.findByRole("button", { name: /Models/ });
    await waitFor(() => expect(modelsButton).not.toHaveTextContent(/\d/));
  });
});
