/**
 * Completeness guard for the phone menu (variant B „Zwei Ebenen", 2026-10-02).
 *
 * Everything the old menu drawer (MobileNav "Index", reached via "More…") and
 * the previous ⊕ sheet offered must still be reachable on the phone — through
 * the tab bar, the ⊕ sheet (level 1 or level 2 "Pages"), the top app bar, or
 * the Settings page (account, board, log out, theme).
 *
 * The expected set is built from what the OLD drawer rendered — every leaf of
 * NAV_TREE (pinned rows + groups = the whole tree) plus CHROME_ITEMS — not
 * from the new sheet's own generator, so dropping a destination or an action
 * anywhere in the new menu turns this red.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

vi.hoisted(() => {
  const store: Record<string, string> = {};
  Object.defineProperty(globalThis, "localStorage", {
    value: {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
      clear: () => { for (const k of Object.keys(store)) delete store[k]; },
    },
    configurable: true,
    writable: true,
  });
});

const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  usePathname: () => "/agents",
  useSearchParams: () => new URLSearchParams(),
}));

const api = vi.hoisted(() => ({
  clearToken: vi.fn(),
  boards: [
    { id: "b1", name: "Board One", color: null, icon: null },
    { id: "b2", name: "Board Two", color: null, icon: null },
  ],
}));
vi.mock("@/lib/api", () => ({
  clearToken: api.clearToken,
  api: {
    approvals: { list: vi.fn(async () => []) },
    boards: { list: vi.fn(async () => api.boards) },
    tasks: { list: vi.fn(async () => []), comments: { list: vi.fn(async () => []) } },
    agents: {
      list: vi.fn(async () => []),
      listDockerSessions: vi.fn(async () => []),
      listHostSessions: vi.fn(async () => [{ id: "a1", name: "Lead" }]),
    },
    groups: { list: vi.fn(async () => []) },
    heads: { list: vi.fn(async () => ({ runs: [] })), pairs: vi.fn(async () => null) },
  },
}));

const store = vi.hoisted(() => ({
  activeBoardId: "b1",
  currentUser: { id: "u1", name: "Operator", email: "op@example.test", role: "admin" },
  setActiveBoardId: vi.fn(),
  pinnedNav: undefined,
}));
vi.mock("@/lib/store", () => ({
  useAppStore: Object.assign((sel?: (s: typeof store) => unknown) => (sel ? sel(store) : store), {
    setState: () => {},
  }),
}));

const voice = vi.hoisted(() => ({ toggleButton: vi.fn() }));
vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceButton: () => <button type="button" data-testid="appbar-voice">voice</button>,
  useVoiceContext: () => voice,
}));
vi.mock("@/components/shared/CreateTaskModal", () => ({
  CreateTaskModal: ({ openRequest }: { openRequest?: number }) =>
    openRequest ? <div data-testid="create-task-modal" /> : null,
}));

import { CHROME_ITEMS, NAV_TREE } from "@/lib/nav";
import MobileNav, { MobileNavProvider } from "../MobileNav";
import { MobileTabBar } from "../MobileTabBar";
import { QuickSheet } from "../QuickSheet";
import { PhoneAccountPanel } from "@/components/settings/PhoneAccountPanel";
import { AppearanceSection } from "@/components/settings/AppearanceSection";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function renderPhoneChrome() {
  return wrap(
    <MobileNavProvider>
      <MobileNav />
      <MobileTabBar />
      <QuickSheet />
    </MobileNavProvider>,
  );
}

const pathOf = (href: string | null) => (href ?? "").split("?")[0];
const linksIn = (el: HTMLElement) =>
  [...el.querySelectorAll("a[href]")].map((a) => pathOf(a.getAttribute("href")));

/** Every route the old drawer listed: the whole NAV_TREE + the chrome row (Settings). */
const OLD_DRAWER_ROUTES = [
  ...NAV_TREE.flatMap((g) => g.children.map((c) => c.href)),
  ...CHROME_ITEMS,
];

/** Every route reachable on the phone without a URL: app bar + tab bar + both sheet levels. */
async function reachablePhoneRoutes(): Promise<Set<string>> {
  renderPhoneChrome();
  const found = new Set<string>();
  // app bar (wordmark → Home) + tab bar
  linksIn(document.body).forEach((h) => found.add(h));
  // ⊕ level 1
  await userEvent.click(within(screen.getByTestId("mobile-tab-bar")).getByRole("button", { name: "New" }));
  await waitFor(() => expect(screen.getByTestId("quick-last-chat")).toBeInTheDocument());
  linksIn(screen.getByTestId("quick-sheet")).forEach((h) => found.add(h));
  // ⊕ level 2 "Pages"
  await userEvent.click(screen.getByTestId("quick-pages"));
  linksIn(screen.getByTestId("quick-sheet")).forEach((h) => found.add(h));
  return found;
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("mc-recent-chats", JSON.stringify([{ kind: "agent", id: "a1" }]));
  router.replace.mockClear();
  api.clearToken.mockClear();
  store.setActiveBoardId.mockClear();
  voice.toggleButton.mockClear();
});

describe("phone menu completeness (old drawer + old ⊕ sheet → variant B)", () => {
  it("the old drawer's route list is not empty (guards the guard)", () => {
    expect(OLD_DRAWER_ROUTES.length).toBeGreaterThanOrEqual(15);
  });

  it("every route of the old drawer is reachable via app bar, tab bar or the ⊕ sheet", async () => {
    const found = await reachablePhoneRoutes();
    const missing = OLD_DRAWER_ROUTES.filter((h) => !found.has(h));
    expect(missing).toEqual([]);
  });

  it("every action of the old ⊕ sheet stays: new job, voice, continue a chat", async () => {
    renderPhoneChrome();
    await userEvent.click(within(screen.getByTestId("mobile-tab-bar")).getByRole("button", { name: "New" }));
    await waitFor(() =>
      expect(screen.getByTestId("quick-last-chat")).toHaveAttribute("href", "/sessions?agent=a1&enter=1"),
    );
    await userEvent.click(screen.getByTestId("quick-voice"));
    expect(voice.toggleButton).toHaveBeenCalledTimes(1);
    await userEvent.click(within(screen.getByTestId("mobile-tab-bar")).getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByTestId("quick-new-job"));
    expect(screen.getByTestId("create-task-modal")).toBeInTheDocument();
  });

  it("the old drawer's chrome stays: wordmark → Home, voice in the app bar, Inbox in the bar", () => {
    renderPhoneChrome();
    expect(document.querySelector("header a[href='/']")).not.toBeNull();
    expect(screen.getByTestId("appbar-voice")).toBeInTheDocument();
    expect(within(screen.getByTestId("mobile-tab-bar")).getByRole("link", { name: "Inbox" })).toHaveAttribute(
      "href",
      "/inbox",
    );
  });

  it("the Settings page carries the phone account panel (board, account, log out)", () => {
    // Rendering the whole 2 400-line Settings page here would test far more
    // than this guard needs; the panel's behaviour is tested below.
    const page = readFileSync(resolve(__dirname, "../../../app/settings/page.tsx"), "utf8");
    expect(page).toMatch(/<PhoneAccountPanel\s*\/>/);
  });

  it("Settings (reached from the sheet) shows who is signed in", async () => {
    wrap(<PhoneAccountPanel />);
    const user = screen.getByTestId("phone-account-user");
    expect(user).toHaveTextContent("Operator");
    expect(user).toHaveTextContent("op@example.test");
  });

  it("Settings switches the board", async () => {
    wrap(<PhoneAccountPanel />);
    await userEvent.click(await screen.findByTestId("phone-board-toggle"));
    await userEvent.click(screen.getByRole("radio", { name: /Board Two/ }));
    expect(store.setActiveBoardId).toHaveBeenCalledWith("b2");
  });

  it("Settings logs out", async () => {
    wrap(<PhoneAccountPanel />);
    await userEvent.click(screen.getByTestId("phone-logout"));
    expect(api.clearToken).toHaveBeenCalledTimes(1);
    expect(router.replace).toHaveBeenCalledWith("/login");
  });

  it("the theme switch lives in Settings › Appearance — and not in the sheet", async () => {
    wrap(<AppearanceSection />);
    expect(screen.getByRole("radiogroup")).toBeInTheDocument();
    cleanup();
    renderPhoneChrome();
    await userEvent.click(within(screen.getByTestId("mobile-tab-bar")).getByRole("button", { name: "New" }));
    expect(within(screen.getByTestId("quick-sheet")).queryByRole("radiogroup")).toBeNull();
  });
});
