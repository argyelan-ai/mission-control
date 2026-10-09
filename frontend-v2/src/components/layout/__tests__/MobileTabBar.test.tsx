/**
 * Phone navigation V2 „Schnell-Knopf": Home · Tasks · ⊕ New · Chats · Inbox.
 *
 * Pins the behaviour the operator chose: five labelled places, the Inbox
 * badge is the real count from lib/inbox.ts (reviews + approvals + open head
 * questions), Chats opens the last chat directly and leads back to the list
 * on the chats page, ⊕ opens the menu sheet (variant B „Zwei Ebenen": new
 * job, voice, last chat, Pages ›, Settings ›), and the bar steps aside for
 * the keyboard.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mem = vi.hoisted(() => {
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
  return store;
});

const nav = vi.hoisted(() => ({ pathname: "/agents" }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => nav.pathname,
  useSearchParams: () => new URLSearchParams(),
}));

const data = vi.hoisted(() => ({
  approvals: [] as unknown[],
  reviews: [] as unknown[],
  runs: [] as unknown[],
  hostAgents: [] as unknown[],
}));
vi.mock("@/lib/api", () => ({
  clearToken: vi.fn(),
  api: {
    approvals: { list: vi.fn(async () => data.approvals) },
    boards: { list: vi.fn(async () => []) },
    tasks: {
      list: vi.fn(async () => data.reviews),
      comments: { list: vi.fn(async () => []) },
    },
    agents: {
      list: vi.fn(async () => []),
      listDockerSessions: vi.fn(async () => []),
      listHostSessions: vi.fn(async () => data.hostAgents),
    },
    groups: { list: vi.fn(async () => []) },
    heads: { list: vi.fn(async () => ({ runs: data.runs })), pairs: vi.fn(async () => null) },
  },
}));

vi.mock("@/lib/store", () => {
  const state = { activeBoardId: "board-1", currentUser: null, setActiveBoardId: () => {}, pinnedNav: undefined };
  return {
    useAppStore: Object.assign((sel?: (s: typeof state) => unknown) => (sel ? sel(state) : state), {
      setState: () => {},
    }),
  };
});

const voice = vi.hoisted(() => ({ toggleButton: vi.fn() }));
vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceButton: () => null,
  useVoiceContext: () => voice,
}));

// The New-task modal is a large component of its own; here only "it opens".
vi.mock("@/components/shared/CreateTaskModal", () => ({
  CreateTaskModal: ({ openRequest }: { openRequest?: number }) =>
    openRequest ? <div data-testid="create-task-modal" data-request={openRequest} /> : null,
}));

import MobileNav, { MobileNavProvider } from "../MobileNav";
import { MobileTabBar } from "../MobileTabBar";
import { QuickSheet } from "../QuickSheet";

function renderNav() {
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

const bar = () => screen.getByTestId("mobile-tab-bar");

beforeEach(() => {
  nav.pathname = "/agents";
  data.approvals = [];
  data.reviews = [];
  data.runs = [];
  data.hostAgents = [];
  localStorage.clear();
  voice.toggleButton.mockClear();
});

afterEach(() => {
  // @ts-expect-error — test cleanup of the keyboard stub
  delete window.visualViewport;
});

describe("phone tab bar", () => {
  it("shows five labelled places in the chosen order", () => {
    renderNav();
    const labels = within(bar())
      .getAllByRole("link")
      .map((a) => a.textContent)
      .concat(within(bar()).getAllByRole("button").map((b) => b.textContent));
    expect(labels).toEqual(["Home", "Tasks", "Chats", "Inbox", "New"]);
    // DOM order — New sits in the middle
    expect([...bar().querySelectorAll("[data-tab]")].map((e) => e.getAttribute("data-tab"))).toEqual([
      "home",
      "tasks",
      "new",
      "chats",
      "inbox",
    ]);
  });

  it("Inbox badge = reviews + approvals + open head questions", async () => {
    data.approvals = [{ id: "a1", status: "pending" }, { id: "a2", status: "pending" }];
    data.reviews = [{ id: "t1", status: "review", assigned_agent_id: null, human_review_required: false }];
    data.runs = [
      { run_id: "r1", task_id: "h1", state: "needs_you", created_at: "2026-10-01T10:00:00Z", task_deleted: false },
      { run_id: "r2", task_id: "h2", state: "passed", created_at: "2026-10-01T10:00:00Z", task_deleted: false },
    ];
    renderNav();
    await waitFor(() => expect(screen.getByTestId("inbox-badge")).toHaveTextContent("4"));
    expect(within(bar()).getByRole("link", { name: "Inbox, 4 waiting" })).toHaveAttribute("href", "/inbox");
  });

  it("shows no badge when nothing waits", async () => {
    renderNav();
    // give the queries a tick to resolve
    await waitFor(() => expect(within(bar()).getByRole("link", { name: "Inbox" })).toBeInTheDocument());
    expect(screen.queryByTestId("inbox-badge")).toBeNull();
  });

  it("Chats opens the last chat directly from anywhere else", () => {
    localStorage.setItem("mc-recent-chats", JSON.stringify([{ kind: "agent", id: "agent-7" }]));
    renderNav();
    expect(within(bar()).getByRole("link", { name: "Chats" })).toHaveAttribute("href", "/sessions?agent=agent-7&enter=1");
  });

  it("Chats on the chats page leads back to the list", () => {
    nav.pathname = "/sessions";
    localStorage.setItem("mc-recent-chats", JSON.stringify([{ kind: "agent", id: "agent-7" }]));
    renderNav();
    const chats = within(bar()).getByRole("link", { name: "Chats" });
    expect(chats).toHaveAttribute("href", "/sessions");
    expect(chats).toHaveAttribute("aria-current", "page");
  });

  it("marks the active place", () => {
    nav.pathname = "/tasks";
    renderNav();
    expect(within(bar()).getByRole("link", { name: "Tasks" })).toHaveAttribute("aria-current", "page");
    expect(within(bar()).getByRole("link", { name: "Home" })).not.toHaveAttribute("aria-current");
  });

  it("steps aside while the on-screen keyboard is up", () => {
    Object.defineProperty(window, "visualViewport", {
      value: { height: window.innerHeight - 300, offsetTop: 0, addEventListener: () => {}, removeEventListener: () => {} },
      configurable: true,
    });
    renderNav();
    expect(screen.queryByTestId("mobile-tab-bar")).toBeNull();
  });
});

describe("⊕ sheet — variant B, level 1", () => {
  it("shows exactly New job · Voice · last chat · Pages › · Settings ›", async () => {
    localStorage.setItem(
      "mc-recent-chats",
      JSON.stringify([
        { kind: "agent", id: "gone" },
        { kind: "agent", id: "a1" },
        { kind: "agent", id: "a2" },
      ]),
    );
    data.hostAgents = [
      { id: "a1", name: "Lead" },
      { id: "a2", name: "Helper" },
    ];
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    const sheet = screen.getByRole("dialog", { name: "Menu" });

    // last chat: one row, the newest chat whose agent still exists
    await waitFor(() => expect(screen.getByTestId("quick-last-chat")).toBeInTheDocument());
    expect(screen.getByTestId("quick-last-chat")).toHaveAttribute("href", "/sessions?agent=a1&enter=1");
    expect(screen.getByTestId("quick-last-chat")).toHaveTextContent("Continue with Lead");
    expect(within(sheet).queryByText(/Helper/)).toBeNull();

    const rows = [...screen.getByTestId("quick-root").querySelectorAll("a, button")].map(
      (e) => e.getAttribute("data-testid"),
    );
    expect(rows).toEqual(["quick-new-job", "quick-voice", "quick-last-chat", "quick-pages", "quick-settings"]);
    expect(screen.getByTestId("quick-settings")).toHaveAttribute("href", "/settings");

    // calm: no theme switch, no tile grid, no "More…", no visible title
    expect(within(sheet).queryByRole("radiogroup")).toBeNull();
    expect(within(sheet).queryByText("More…")).toBeNull();
    expect(screen.queryByTestId("quick-areas")).toBeNull();
    expect(within(sheet).queryByRole("heading")).toBeNull();
  });

  it("New job closes the sheet and opens the New-task modal", async () => {
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByTestId("quick-new-job"));
    await waitFor(() => expect(screen.queryByTestId("quick-sheet")).toBeNull());
    expect(screen.getByTestId("create-task-modal")).toHaveAttribute("data-request", "1");
  });

  it("Voice command starts the voice assistant", async () => {
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByTestId("quick-voice"));
    expect(voice.toggleButton).toHaveBeenCalledTimes(1);
  });

  it("hides the last-chat row when this device has no recent chat", async () => {
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    expect(screen.queryByTestId("quick-last-chat")).toBeNull();
    expect(mem["mc-recent-chats"]).toBeUndefined();
  });
});

describe("⊕ sheet — variant B, level 2 Pages", () => {
  it("Pages › opens every page outside the tab bar, Used most first", async () => {
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByTestId("quick-pages"));
    const level = screen.getByTestId("quick-pages-level");
    expect(within(level).getByRole("heading", { name: "Pages" })).toBeInTheDocument();
    const hrefs = [...level.querySelectorAll("a[data-page]")].map((a) => a.getAttribute("href"));
    expect(hrefs.slice(0, 4)).toEqual(["/runtimes", "/agents", "/insights", "/memory"]);
    // the rest alphabetical by label: Benchmark, Files, Loops, Office, Repos, Schedule, Skills
    expect(hrefs.slice(4)).toEqual(["/bench", "/files", "/loops", "/office", "/repos", "/schedule", "/skills"]);
    // tab-bar places and Settings are not repeated
    for (const h of ["/", "/tasks", "/sessions", "/inbox", "/settings"]) expect(hrefs).not.toContain(h);
  });

  it("‹ Back returns to level 1", async () => {
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByTestId("quick-pages"));
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByTestId("quick-root")).toBeInTheDocument();
    expect(screen.queryByTestId("quick-pages-level")).toBeNull();
  });

  it("Esc on level 2 goes back one level, Esc on level 1 closes", async () => {
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByTestId("quick-pages"));
    await userEvent.keyboard("{Escape}");
    expect(screen.getByTestId("quick-root")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("quick-sheet")).toBeNull());
  });

  it("reopening always starts on level 1", async () => {
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByTestId("quick-pages"));
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByTestId("quick-sheet")).toBeNull());
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    expect(screen.getByTestId("quick-root")).toBeInTheDocument();
  });
});
