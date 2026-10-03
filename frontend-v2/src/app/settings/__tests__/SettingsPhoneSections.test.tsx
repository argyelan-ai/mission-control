/**
 * /settings on the phone — one list of sections; a row opens the section as
 * its own screen with a back button. The section lives in the URL, so reload
 * and the browser's back step work.
 *
 * jsdom applies no CSS, so "phone only" / "desktop only" is asserted through
 * the `md:hidden` / `hidden md:block` classes that decide it in the browser.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode, MouseEvent } from "react";

const nav = vi.hoisted(() => ({
  search: "",
  replace: vi.fn(),
  push: vi.fn(),
  back: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: nav.push, back: nav.back, refresh: vi.fn() }),
  usePathname: () => "/settings",
  useSearchParams: () => new URLSearchParams(nav.search),
}));

// A plain anchor: the test drives the URL itself (nav.search) instead of
// Next's router, and only needs the href and the click callback.
vi.mock("next/link", () => ({
  __esModule: true,
  default: ({ href, children, onClick, scroll: _scroll, ...rest }: {
    href: string;
    children: ReactNode;
    onClick?: (e: MouseEvent<HTMLAnchorElement>) => void;
    scroll?: boolean;
  } & Record<string, unknown>) => (
    <a
      href={href}
      {...rest}
      onClick={(e) => {
        e.preventDefault();
        onClick?.(e);
      }}
    >
      {children}
    </a>
  ),
}));

vi.mock("@/components/layout/AppShell", () => ({
  __esModule: true,
  default: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

const app = vi.hoisted(() => ({
  state: {
    activeBoardId: null as string | null,
    boards: [] as unknown[],
    currentUser: { id: "u1", email: "a@b.com", name: "Admin", role: "admin" } as {
      id: string; email: string; name: string; role: string;
    } | null,
    setActiveBoardId: () => {},
    setBoards: () => {},
    setCurrentUser: () => {},
  },
}));
vi.mock("@/lib/store", () => ({
  useNotificationStore: Object.assign(
    (selector?: (s: { notifications: never[] }) => unknown) =>
      selector ? selector({ notifications: [] }) : { notifications: [] },
    { getState: () => ({ addNotification: vi.fn() }) },
  ),
  useAppStore: Object.assign(
    (selector?: (s: typeof app.state) => unknown) => (selector ? selector(app.state) : app.state),
    { setState: (partial: Partial<typeof app.state>) => Object.assign(app.state, partial) },
  ),
}));

import SettingsPage from "../page";
import { SECTIONS } from "@/components/settings/sections";

vi.setConfig({ testTimeout: 20_000 });

// Written out on purpose, not read from SECTIONS: a section dropped from the
// registry must turn this test red, not silently shrink the expectation.
const ALL = [
  "profile", "security", "appearance", "shortcuts",
  "autonomy", "intelligence", "costs", "night-shift",
  "github", "slack", "telegram", "ai-providers",
  "apikeys", "credentials",
  "users", "about",
];
const MEMBER = ["profile", "security", "appearance", "shortcuts", "about"];

const ADMIN = { id: "u1", email: "a@b.com", name: "Admin", role: "admin" };
const MEMBER_USER = { id: "u2", email: "m@b.com", name: "Member", role: "member" };

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui = () => (
    <QueryClientProvider client={qc}>
      <SettingsPage />
    </QueryClientProvider>
  );
  const r = render(ui());
  return { ...r, rerenderPage: () => r.rerender(ui()) };
}

const phoneList = () => screen.queryByTestId("settings-phone-list");
const classes = (el: Element) => el.className.split(/\s+/);

beforeEach(() => {
  nav.search = "";
  nav.replace.mockReset();
  nav.push.mockReset();
  nav.back.mockReset();
  app.state.currentUser = ADMIN;
});

describe("Settings on the phone — section list", () => {
  it("keeps the registry complete (guard for the list below)", () => {
    expect(SECTIONS.map((s) => s.id)).toEqual(ALL);
  });

  it("lists every section as a row that links to its own screen (admin)", () => {
    renderPage();
    const list = phoneList()!;
    expect(list).not.toBeNull();
    expect(classes(list)).toContain("md:hidden");
    const links = within(list).getAllByRole("link");
    expect(links.map((a) => a.getAttribute("data-section"))).toEqual(ALL);
    for (const a of links) {
      expect(a.getAttribute("href")).toBe(`/settings?section=${a.getAttribute("data-section")}`);
    }
  });

  it("gives a member only the sections a member may open", () => {
    app.state.currentUser = MEMBER_USER;
    renderPage();
    const links = within(phoneList()!).getAllByRole("link");
    expect(links.map((a) => a.getAttribute("data-section"))).toEqual(MEMBER);
  });

  it("rows are labelled, sit in labelled groups and are at least 44 px tall", () => {
    renderPage();
    const list = phoneList()!;
    expect(within(list).getByRole("link", { name: "Night shift" })).toBeInTheDocument();
    expect(within(list).getByRole("list", { name: "Connections" })).toBeInTheDocument();
    for (const a of within(list).getAllByRole("link")) {
      expect(classes(a)).toContain("min-h-12"); // 48 px
    }
  });

  it("keeps the account panel (#738) at the top of the list screen, before the sections", () => {
    renderPage();
    const account = screen.getByTestId("phone-account");
    const list = phoneList()!;
    expect(account.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("on the list screen hides the section column on the phone and shows no back button", () => {
    renderPage();
    expect(classes(screen.getByTestId("settings-section"))).toEqual(
      expect.arrayContaining(["hidden", "md:block"]),
    );
    expect(screen.queryByTestId("settings-phone-back")).toBeNull();
  });
});

describe("Settings on the phone — an opened section", () => {
  it.each(ALL)("opens %s as its own screen with a back button", (id) => {
    nav.search = `section=${id}`;
    renderPage();
    expect(phoneList()).toBeNull();
    expect(screen.getByTestId("settings-phone-back")).toHaveAccessibleName("Back to Settings");
    const column = screen.getByTestId("settings-section");
    expect(classes(column)).not.toContain("hidden");
    // Something of the section is rendered (its header or its loader).
    expect(column.querySelector(".max-w-3xl")!.childElementCount).toBeGreaterThan(0);
  });

  it("leaves the account panel on the list — an open section shows only the section", () => {
    nav.search = "section=profile";
    renderPage();
    expect(screen.queryByTestId("phone-account")).toBeNull();
  });

  it("hides the page header on the phone while a section is open", () => {
    nav.search = "section=about";
    renderPage();
    const header = screen.getByRole("heading", { level: 1 }).parentElement!;
    expect(classes(header)).toEqual(expect.arrayContaining(["hidden", "md:block"]));
  });

  it("back after tapping a row steps back in history (the list keeps its place)", async () => {
    const { rerenderPage } = renderPage();
    await userEvent.click(within(phoneList()!).getByRole("link", { name: "About" }));
    nav.search = "section=about"; // what the link's navigation did
    rerenderPage();
    await userEvent.click(screen.getByTestId("settings-phone-back"));
    expect(nav.back).toHaveBeenCalledTimes(1);
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("back after a deep link or reload replaces to the list instead of leaving the app", async () => {
    nav.search = "section=github";
    renderPage();
    await userEvent.click(screen.getByTestId("settings-phone-back"));
    expect(nav.back).not.toHaveBeenCalled();
    expect(nav.replace).toHaveBeenCalledWith("/settings", { scroll: false });
  });

  it("shows the list when a member follows a link to an admin section", () => {
    app.state.currentUser = MEMBER_USER;
    nav.search = "section=users";
    renderPage();
    expect(phoneList()).not.toBeNull();
    expect(screen.queryByTestId("settings-phone-back")).toBeNull();
  });

  it("shows the list for an unknown section", () => {
    nav.search = "section=nope";
    renderPage();
    expect(phoneList()).not.toBeNull();
  });
});
