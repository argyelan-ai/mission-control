/**
 * Completeness guard for the phone navigation.
 *
 * Shell „Drei Daumen" (operator decision 2026-11-18): the tab bar holds only
 * Chat · Models · Knowledge, the chat drawer holds the chats plus Models ·
 * Knowledge · Settings, Memory and Skills sit on the Knowledge landing, and
 * every other page sits on the More page (/more). Everything the earlier
 * menus offered must still be reachable on the phone without typing a URL —
 * through the app bar, the tab bar, the drawer, the Knowledge landing or More
 * (account, board, log out and theme stay in Settings).
 *
 * The expected set is built from the whole navigation tree (NAV_TREE +
 * CHROME_ITEMS), not from the new menus' own generators, so dropping a
 * destination anywhere turns this red.
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
    vault: { list: vi.fn(async () => ({ count: 0, items: [] })) },
    skills: { list: vi.fn(async () => ({ skills: [], total: 0 })) },
  },
}));
vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
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
import { ChatDrawer } from "../ChatDrawer";
import { MorePagesSection } from "@/components/settings/MorePagesSection";
import KnowledgePage from "@/app/knowledge/page";
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
      <ChatDrawer />
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

/** Every route reachable on the phone without a URL: app bar + tab bar + drawer + Knowledge + More. */
async function reachablePhoneRoutes(): Promise<Set<string>> {
  renderPhoneChrome();
  const found = new Set<string>();
  linksIn(document.body).forEach((h) => found.add(h));
  await userEvent.click(screen.getByTestId("drawer-button"));
  await waitFor(() => expect(screen.getAllByTestId("chat-list-row").length).toBeGreaterThan(0));
  linksIn(screen.getByTestId("chat-drawer")).forEach((h) => found.add(h));
  cleanup();
  wrap(<MorePagesSection />);
  linksIn(screen.getByTestId("more-pages")).forEach((h) => found.add(h));
  cleanup();
  wrap(<KnowledgePage />);
  linksIn(screen.getByTestId("knowledge-links")).forEach((h) => found.add(h));
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

describe("phone navigation completeness (tab bar + drawer + Knowledge + More)", () => {
  it("the navigation tree is not empty (guards the guard)", () => {
    expect(OLD_DRAWER_ROUTES.length).toBeGreaterThanOrEqual(15);
  });

  it("every route of the navigation tree is reachable via app bar, tab bar, drawer, Knowledge or More", async () => {
    const found = await reachablePhoneRoutes();
    const missing = OLD_DRAWER_ROUTES.filter((h) => !found.has(h));
    expect(missing).toEqual([]);
  });

  it("the last chat is one tap away in the drawer, New chat leads to the picker", async () => {
    renderPhoneChrome();
    await userEvent.click(screen.getByTestId("drawer-button"));
    const drawer = screen.getByTestId("chat-drawer");
    await waitFor(() =>
      expect(within(drawer).getByText("Lead").closest("a")).toHaveAttribute("href", "/sessions?agent=a1&enter=1"),
    );
    expect(within(drawer).getByTestId("chat-list-new")).toHaveAttribute("href", "/sessions?new=1");
  });

  it("the app bar keeps the wordmark (→ the last chat via /) and the voice button", () => {
    renderPhoneChrome();
    expect(document.querySelector("header a[href='/']")).not.toBeNull();
    expect(screen.getByTestId("appbar-voice")).toBeInTheDocument();
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

  it("the theme switch lives in Settings › Appearance — and not in the drawer", async () => {
    wrap(<AppearanceSection />);
    expect(screen.getByRole("radiogroup")).toBeInTheDocument();
    cleanup();
    renderPhoneChrome();
    await userEvent.click(screen.getByTestId("drawer-button"));
    expect(within(screen.getByTestId("chat-drawer")).queryByRole("radiogroup")).toBeNull();
  });
});
