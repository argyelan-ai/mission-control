/**
 * The chat drawer (phone) and the chat column (desktop) — operator decision
 * 2026-11-18, variant C with variant A's menu.
 *
 * Pins: "New chat" on top, sections Waiting for you / Today / Earlier with a
 * calm row (title + time, one dot only for waiting/working), the drawer's
 * foot Models · Knowledge · Settings, a pick closes the drawer; the desktop
 * column carries Chat · Models · Knowledge + More at its foot.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

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

const nav = vi.hoisted(() => ({ pathname: "/sessions" }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => nav.pathname,
  useSearchParams: () => new URLSearchParams(),
}));

const iso = (dayOffset: number, h: number) => {
  const d = new Date();
  d.setDate(d.getDate() + dayOffset);
  d.setHours(h, 5, 0, 0);
  return d.toISOString();
};

const data = vi.hoisted(() => ({
  approvals: [] as unknown[],
  hostAgents: [] as unknown[],
  groups: [] as unknown[],
}));
vi.mock("@/lib/api", () => ({
  clearToken: vi.fn(),
  api: {
    approvals: { list: vi.fn(async () => data.approvals) },
    boards: { list: vi.fn(async () => []) },
    system: { status: vi.fn(async () => null) },
    tasks: { list: vi.fn(async () => []), comments: { list: vi.fn(async () => []) } },
    agents: {
      list: vi.fn(async () => []),
      listDockerSessions: vi.fn(async () => []),
      listHostSessions: vi.fn(async () => data.hostAgents),
    },
    groups: { list: vi.fn(async () => data.groups) },
    heads: { list: vi.fn(async () => ({ runs: [] })) },
  },
}));

const palette = vi.hoisted(() => ({ open: vi.fn() }));
vi.mock("@/lib/store", () => {
  const state = {
    activeBoardId: "board-1",
    currentUser: null,
    sidebarCollapsed: false,
    toggleSidebar: () => {},
    setCommandPaletteOpen: palette.open,
  };
  return {
    useAppStore: Object.assign((sel?: (s: typeof state) => unknown) => (sel ? sel(state) : state), {
      setState: () => {},
    }),
  };
});

vi.mock("@/components/voice/live/NavVoiceButton", () => ({ NavVoiceButton: () => null }));
vi.mock("@/components/shared/ThemeSwitch", () => ({ ThemeCycleButton: () => null }));

import MobileNav, { MobileNavProvider } from "../MobileNav";
import { ChatDrawer } from "../ChatDrawer";
import { ChatSidebar } from "../ChatSidebar";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MobileNavProvider>{ui}</MobileNavProvider>
    </QueryClientProvider>,
  );
}

async function openDrawer() {
  wrap(
    <>
      <MobileNav />
      <ChatDrawer />
    </>,
  );
  await userEvent.click(screen.getByTestId("drawer-button"));
  return screen.getByTestId("chat-drawer");
}

beforeEach(() => {
  nav.pathname = "/sessions";
  localStorage.clear();
  data.approvals = [];
  data.groups = [];
  data.hostAgents = [
    { id: "a1", name: "Recipe rework", status: "idle", last_task_activity_at: iso(0, 9), updated_at: iso(-30, 9) },
    { id: "a2", name: "Bench video", status: "working", last_task_activity_at: iso(0, 8), updated_at: iso(-30, 9) },
    { id: "a3", name: "Cooling stand", status: "idle", last_task_activity_at: iso(-4, 9), updated_at: iso(-30, 9) },
  ];
});

describe("chat drawer (phone)", () => {
  it("is closed until the sidebar icon is tapped", () => {
    wrap(
      <>
        <MobileNav />
        <ChatDrawer />
      </>,
    );
    expect(screen.queryByTestId("chat-drawer")).toBeNull();
    expect(screen.getByTestId("drawer-button")).toHaveAttribute("aria-expanded", "false");
  });

  it("puts New chat on top, leading to the picker", async () => {
    const drawer = await openDrawer();
    const first = within(drawer).getAllByRole("link")[0];
    expect(first).toHaveTextContent("New chat");
    expect(first).toHaveAttribute("href", "/sessions?pick=1");
  });

  it("sorts chats into Waiting for you, Today and Earlier", async () => {
    data.approvals = [{ id: "p1", agent_id: "a3", status: "pending" }];
    const drawer = await openDrawer();
    await waitFor(() => expect(within(drawer).getByTestId("chat-section-waiting")).toBeInTheDocument());
    const titles = (id: string) =>
      within(within(drawer).getByTestId(id)).getAllByTestId("chat-list-row").map((r) => r.querySelector(".truncate")?.textContent);
    expect(titles("chat-section-waiting")).toEqual(["Cooling stand"]);
    expect(titles("chat-section-today")).toEqual(["Recipe rework", "Bench video"]);
    expect(within(drawer).queryByTestId("chat-section-earlier")).toBeNull();
    expect(within(drawer).getByRole("heading", { name: "Waiting for you" })).toBeInTheDocument();
  });

  // Operator pictures 18.11.2026: the desktop had a Today section the phone
  // did not — opening a chat on one device must not regroup it there.
  it("opening an old chat on this device does not move it to Today", async () => {
    localStorage.setItem("mc-recent-chats", JSON.stringify([{ kind: "agent", id: "a3" }]));
    const drawer = await openDrawer();
    await waitFor(() => expect(within(drawer).getByTestId("chat-section-earlier")).toBeInTheDocument());
    expect(within(within(drawer).getByTestId("chat-section-earlier")).getByText("Cooling stand")).toBeInTheDocument();
  });

  it("an older chat lands in Earlier", async () => {
    const drawer = await openDrawer();
    await waitFor(() => expect(within(drawer).getByTestId("chat-section-earlier")).toBeInTheDocument());
    expect(within(within(drawer).getByTestId("chat-section-earlier")).getByText("Cooling stand")).toBeInTheDocument();
  });

  it("a row is title + time with one dot only for working or waiting — no badges", async () => {
    const drawer = await openDrawer();
    await waitFor(() => expect(within(drawer).getAllByTestId("chat-list-row")).toHaveLength(3));
    const row = (name: string) => within(drawer).getByText(name).closest("a") as HTMLElement;
    expect(row("Bench video").querySelector("[data-state='working']")).not.toBeNull();
    expect(row("Recipe rework").querySelector("[data-state]")).toBeNull();
    expect(row("Recipe rework")).toHaveAttribute("href", "/sessions?agent=a1&enter=1");
    expect(row("Recipe rework").textContent).toMatch(/Recipe rework\d{2}:05/);
  });

  it("marks the chat that is open on the chat page", async () => {
    localStorage.setItem("mc-recent-chats", JSON.stringify([{ kind: "agent", id: "a2" }]));
    const drawer = await openDrawer();
    await waitFor(() => expect(within(drawer).getAllByTestId("chat-list-row")).toHaveLength(3));
    expect(within(drawer).getByText("Bench video").closest("a")).toHaveAttribute("aria-current", "page");
    expect(within(drawer).getByText("Recipe rework").closest("a")).not.toHaveAttribute("aria-current");
  });

  it("ends with Models · Knowledge · Settings", async () => {
    const drawer = await openDrawer();
    const links = within(drawer).getAllByRole("link");
    expect(links.slice(-3).map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["Models", "/runtimes"],
      ["Knowledge", "/knowledge"],
      ["Settings", "/settings"],
    ]);
  });

  it("closes when a chat is picked", async () => {
    const drawer = await openDrawer();
    await waitFor(() => expect(within(drawer).getAllByTestId("chat-list-row")).toHaveLength(3));
    await userEvent.click(within(drawer).getByText("Bench video"));
    await waitFor(() => expect(screen.queryByTestId("chat-drawer")).toBeNull());
  });

  // Shell review D1: a modal drawer takes focus, keeps it, gives it back.
  it("moves focus into the drawer and gives it back to the icon on close", async () => {
    await openDrawer();
    // the dialog itself — no ring around "New chat" that reads as a selection
    expect(document.activeElement).toBe(screen.getByTestId("chat-drawer"));
    await userEvent.tab();
    expect(document.activeElement).toHaveTextContent("New chat");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("chat-drawer")).toBeNull());
    expect(document.activeElement).toBe(screen.getByTestId("drawer-button"));
    await userEvent.click(screen.getByTestId("drawer-button"));
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("chat-drawer")).toBeNull());
    expect(document.activeElement).toBe(screen.getByTestId("drawer-button"));
  });

  it("Tab from the last control wraps to the first, never into the chat behind", async () => {
    const drawer = await openDrawer();
    const links = within(drawer).getAllByRole("link");
    links[links.length - 1].focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(links[0]);
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(links[links.length - 1]);
  });

  it("is a dialog that holds its own navigation landmark", async () => {
    const drawer = await openDrawer();
    expect(drawer.tagName).toBe("DIV");
    expect(within(drawer).getByRole("navigation")).toHaveTextContent("Settings");
  });

  // Shell review A4: widening the window past md with the drawer open.
  it("closes when the window grows to the desktop layout", async () => {
    const listeners: (() => void)[] = [];
    let desktop = false;
    const original = window.matchMedia;
    window.matchMedia = ((q: string) => ({
      get matches() { return desktop; },
      media: q,
      addEventListener: (_: string, fn: () => void) => listeners.push(fn),
      removeEventListener: () => {},
    })) as unknown as typeof window.matchMedia;
    try {
      await openDrawer();
      desktop = true;
      act(() => listeners.forEach((fn) => fn()));
      await waitFor(() => expect(screen.queryByTestId("chat-drawer")).toBeNull());
    } finally {
      window.matchMedia = original;
    }
  });

  it("says so when there are no chats", async () => {
    data.hostAgents = [];
    const drawer = await openDrawer();
    await waitFor(() => expect(within(drawer).getByText("No chats yet")).toBeInTheDocument());
  });
});

describe("chat column (desktop)", () => {
  it("shows the same list and Chat · Models · Knowledge · More at its foot", async () => {
    wrap(<ChatSidebar />);
    const column = screen.getByTestId("chat-sidebar");
    await waitFor(() => expect(within(column).getAllByTestId("chat-list-row")).toHaveLength(3));
    const nav = within(column).getByRole("navigation", { name: "Main navigation" });
    expect(within(nav).getAllByRole("link").map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["Chat", "/sessions"],
      ["Models", "/runtimes"],
      ["Knowledge", "/knowledge"],
      ["More", "/settings?section=more"],
    ]);
    expect(within(nav).getByRole("link", { name: /^Chat/ })).toHaveAttribute("aria-current", "page");
  });

  it("carries the waiting count next to Chat", async () => {
    data.approvals = [{ id: "p1", agent_id: "a3", status: "pending" }];
    wrap(<ChatSidebar />);
    await waitFor(() => expect(screen.getByTestId("chat-sidebar-badge")).toHaveTextContent("1"));
  });

  // Operator pictures 18.11.2026: search (⌘K) was missing in the column.
  it("offers search (⌘K) and opens the command palette", async () => {
    wrap(<ChatSidebar />);
    const search = screen.getByTestId("chat-sidebar-search");
    expect(search).toHaveTextContent("⌘K");
    await userEvent.click(search);
    expect(palette.open).toHaveBeenCalledWith(true);
  });

  // …and the last row was cut by the nav block without a fade.
  it("fades the list out above the nav block instead of cutting a row", () => {
    wrap(<ChatSidebar />);
    expect(screen.getByTestId("chat-sidebar-scroll").className).toContain("fade-bottom");
    // …and the class is real: a bottom mask plus as much end padding.
    const css = readFileSync(resolve(__dirname, "../../../styles/globals.css"), "utf8");
    expect(css).toMatch(/\.fade-bottom\s*\{[^}]*mask-image:\s*linear-gradient\(to bottom[^}]*padding-bottom:\s*32px/);
  });

  it("has no collapse button (the column cannot collapse)", () => {
    wrap(<ChatSidebar />);
    expect(screen.queryByRole("button", { name: /collapse/i })).toBeNull();
  });
});
