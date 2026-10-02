/**
 * The ⊕ sheet is the ONE phone menu (operator, 2026-10-02: "we should have
 * all menu items in this new + menu"). The former index drawer
 * (MobileNav, removed in this change) offered:
 *
 *   destinations — every route of the nav tree (pinned rows + the folded
 *                  groups together cover the whole tree) + Settings (chrome)
 *   actions      — board switch, who is signed in (name + email), theme
 *                  switch, log out
 *
 * This test pins that nothing of it is lost: everything the drawer had must
 * be reachable from the phone's own navigation — the tab bar or the sheet.
 * A destination added to lib/nav.ts later must show up here too.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CHROME_ITEMS, NAV_ITEMS, NAV_TREE } from "@/lib/nav";

vi.hoisted(() => {
  const mem: Record<string, string> = {};
  Object.defineProperty(globalThis, "localStorage", {
    value: {
      getItem: (k: string) => mem[k] ?? null,
      setItem: (k: string, v: string) => { mem[k] = v; },
      removeItem: (k: string) => { delete mem[k]; },
      clear: () => { for (const k of Object.keys(mem)) delete mem[k]; },
    },
    configurable: true,
    writable: true,
  });
});

const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  usePathname: () => "/agents",
  useSearchParams: () => new URLSearchParams(),
}));

const apiMock = vi.hoisted(() => ({
  clearToken: vi.fn(),
  boards: [
    { id: "b1", name: "Development", color: null, icon: null },
    { id: "b2", name: "Research", color: null, icon: null },
  ],
}));
vi.mock("@/lib/api", () => ({
  clearToken: apiMock.clearToken,
  api: {
    approvals: { list: vi.fn(async () => []) },
    boards: { list: vi.fn(async () => apiMock.boards) },
    tasks: { list: vi.fn(async () => []), comments: { list: vi.fn(async () => []) } },
    agents: {
      list: vi.fn(async () => []),
      listDockerSessions: vi.fn(async () => []),
      listHostSessions: vi.fn(async () => []),
    },
    groups: { list: vi.fn(async () => []) },
    heads: { list: vi.fn(async () => ({ runs: [] })), pairs: vi.fn(async () => null) },
  },
}));

const store = vi.hoisted(() => ({
  activeBoardId: "b1",
  currentUser: { name: "Operator", email: "operator@example.com" },
  setActiveBoardId: vi.fn(),
  pinnedNav: undefined,
}));
vi.mock("@/lib/store", () => ({
  useAppStore: Object.assign(
    (sel?: (s: typeof store) => unknown) => (sel ? sel(store) : store),
    { setState: () => {} },
  ),
}));

vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceButton: () => null,
  useVoiceContext: () => ({ toggleButton: vi.fn() }),
}));
vi.mock("@/components/shared/CreateTaskModal", () => ({ CreateTaskModal: () => null }));

import MobileNav, { MobileNavProvider } from "../MobileNav";
import { MobileTabBar } from "../MobileTabBar";
import { QuickSheet } from "../QuickSheet";

/** What the old drawer linked to, written out as it rendered on 2026-10-02. */
const OLD_DRAWER_DESTINATIONS = [
  "/", "/insights", "/office",
  "/tasks", "/inbox", "/sessions", "/agents",
  "/memory", "/files", "/repos", "/skills",
  "/bench",
  "/runtimes", "/loops", "/schedule",
  "/settings",
];

function renderPhone() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MobileNavProvider>
        <MobileNav />
        <MobileTabBar />
        <QuickSheet />
      </MobileNavProvider>
    </QueryClientProvider>,
  );
}

const routeOf = (href: string) => href.split("?")[0];

async function openSheet() {
  renderPhone();
  await userEvent.click(within(screen.getByTestId("mobile-tab-bar")).getByRole("button", { name: "New" }));
  return screen.getByRole("dialog", { name: "New" });
}

beforeEach(() => {
  router.replace.mockClear();
  apiMock.clearToken.mockClear();
  store.setActiveBoardId.mockClear();
});

describe("⊕ sheet = the one phone menu", () => {
  it("the written-out drawer list matches what the drawer derived from lib/nav.ts", () => {
    const derived = new Set([...NAV_TREE.flatMap((g) => g.children.map((c) => c.href)), ...CHROME_ITEMS]);
    expect(new Set(OLD_DRAWER_DESTINATIONS)).toEqual(derived);
  });

  it("every destination of the old drawer (and of nav.ts) is reachable from tab bar or sheet", async () => {
    const sheet = await openSheet();
    const bar = screen.getByTestId("mobile-tab-bar");
    const reachable = new Set(
      [...within(bar).getAllByRole("link"), ...within(sheet).getAllByRole("link")]
        .map((a) => routeOf(a.getAttribute("href") ?? "")),
    );
    const expected = new Set([...OLD_DRAWER_DESTINATIONS, ...NAV_ITEMS.map((i) => i.href)]);
    const missing = [...expected].filter((h) => !reachable.has(h));
    expect(missing).toEqual([]);
  });

  it("the sheet lists each other area exactly once, grouped like the desktop column", async () => {
    await openSheet();
    const areas = screen.getByTestId("quick-areas");
    const hrefs = within(areas).getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(new Set(hrefs).size).toBe(hrefs.length);
    // the four tab-bar places are not repeated in the sheet
    for (const h of ["/", "/tasks", "/sessions", "/inbox"]) expect(hrefs).not.toContain(h);
    // group headings in nav.ts order
    expect(within(areas).getAllByRole("region").map((r) => r.getAttribute("data-group"))).toEqual(
      ["overview", "work", "knowledge", "studio", "system"],
    );
  });

  it("board switch: every board is offered and choosing one switches", async () => {
    await openSheet();
    const account = screen.getByTestId("quick-account");
    await waitFor(() => expect(within(account).getByRole("radio", { name: /Research/ })).toBeInTheDocument());
    expect(within(account).getByRole("radio", { name: /Development/ })).toHaveAttribute("aria-checked", "true");
    await userEvent.click(within(account).getByRole("radio", { name: /Research/ }));
    expect(store.setActiveBoardId).toHaveBeenCalledWith("b2");
  });

  it("account: who is signed in, theme switch, Settings, log out", async () => {
    await openSheet();
    const account = screen.getByTestId("quick-account");
    expect(within(account).getByText("Operator")).toBeInTheDocument();
    expect(within(account).getByText("operator@example.com")).toBeInTheDocument();
    expect(within(account).getAllByRole("radio").length).toBeGreaterThanOrEqual(3); // dark · light · system
    expect(within(account).getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
    await userEvent.click(within(account).getByRole("button", { name: "Log out" }));
    expect(apiMock.clearToken).toHaveBeenCalledTimes(1);
    expect(router.replace).toHaveBeenCalledWith("/login");
  });

  it("the old drawer cannot be opened any more", async () => {
    const sheet = await openSheet();
    expect(within(sheet).queryByRole("button", { name: "More…" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Close menu" })).toBeNull();
  });
});
