/**
 * Phone navigation „Drei Daumen" (operator decision 2026-11-18, ADR-102):
 * Chat · Models · Knowledge.
 *
 * Pins: exactly three labelled places, Chat carries the real count of what
 * waits on the operator (lib/inbox.ts) and no badge when nothing waits, each
 * tab leads to its page and lights up on it, the bar steps aside for the
 * keyboard. The old ⊕ sheet (QuickSheet) is no longer mounted by the shell;
 * its own behaviour stays pinned below through a test opener.
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
  journeys: null as unknown,
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
    system: { journeys: vi.fn(async () => ({ result: data.journeys })) },
  },
}));

// The journey count is admin-only; the journey test signs in as admin.
const user = vi.hoisted(() => ({ current: null as null | { id: string; role: string } }));
vi.mock("@/lib/store", () => {
  const state = {
    activeBoardId: "board-1",
    get currentUser() {
      return user.current;
    },
    setActiveBoardId: () => {},
    pinnedNav: undefined,
  };
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

// Voice PR 5: with a lead agent known the top-bar mic is the live voice and
// Jarvis moves into this sheet. Default here: no lead (live voice off).
const live = vi.hoisted(() => ({ lead: null as null | { agentId: string; agentName: string } }));
vi.mock("@/components/voice/live/VoiceLiveCall", () => ({
  useVoiceLiveCall: () => (live.lead ? { lead: live.lead } : null),
}));

// The New-task modal is a large component of its own; here only "it opens".
vi.mock("@/components/shared/CreateTaskModal", () => ({
  CreateTaskModal: ({ openRequest }: { openRequest?: number }) =>
    openRequest ? <div data-testid="create-task-modal" data-request={openRequest} /> : null,
}));

import MobileNav, { MobileNavProvider, useMobileNav } from "../MobileNav";
import { MobileTabBar } from "../MobileTabBar";
import { QuickSheet } from "../QuickSheet";

/** The shell no longer has a ⊕ button; this stands in for it. */
function OpenSheet() {
  const { setQuickOpen } = useMobileNav();
  return (
    <button type="button" data-testid="open-sheet" onClick={() => setQuickOpen(true)}>
      open sheet
    </button>
  );
}

function renderNav() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MobileNavProvider>
        <MobileNav />
        <MobileTabBar />
        <QuickSheet />
        <OpenSheet />
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
  data.journeys = null;
  localStorage.clear();
  voice.toggleButton.mockClear();
});

afterEach(() => {
  // @ts-expect-error — test cleanup of the keyboard stub
  delete window.visualViewport;
});

describe("phone tab bar", () => {
  it("shows exactly three labelled places: Chat · Models · Knowledge", () => {
    renderNav();
    const links = within(bar()).getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual(["Chat", "Models", "Knowledge"]);
    expect(within(bar()).queryAllByRole("button")).toEqual([]);
    expect([...bar().querySelectorAll("[data-tab]")].map((e) => e.getAttribute("data-tab"))).toEqual([
      "chat",
      "models",
      "knowledge",
    ]);
  });

  it("each tab leads to its page", () => {
    renderNav();
    expect(within(bar()).getByRole("link", { name: "Chat" })).toHaveAttribute("href", "/sessions");
    expect(within(bar()).getByRole("link", { name: "Models" })).toHaveAttribute("href", "/runtimes");
    expect(within(bar()).getByRole("link", { name: "Knowledge" })).toHaveAttribute("href", "/knowledge");
  });

  // Shell review A2: plain /sessions sent an open chat back to the old list.
  it("Chat leads straight into the last chat, without the list-entry marker", () => {
    localStorage.setItem("mc-recent-chats", JSON.stringify([{ kind: "agent", id: "agent-7" }]));
    renderNav();
    expect(within(bar()).getByRole("link", { name: "Chat" })).toHaveAttribute("href", "/sessions?agent=agent-7");
  });

  it("Chat badge = reviews + approvals + open head questions", async () => {
    data.approvals = [{ id: "a1", status: "pending" }, { id: "a2", status: "pending" }];
    data.reviews = [{ id: "t1", status: "review", assigned_agent_id: null, human_review_required: false }];
    data.runs = [
      { run_id: "r1", task_id: "h1", state: "needs_you", created_at: "2026-10-01T10:00:00Z", task_deleted: false },
      { run_id: "r2", task_id: "h2", state: "passed", created_at: "2026-10-01T10:00:00Z", task_deleted: false },
    ];
    renderNav();
    await waitFor(() => expect(screen.getByTestId("chat-badge")).toHaveTextContent("4"));
    expect(within(bar()).getByRole("link", { name: "Chat, 4 waiting for you" })).toHaveAttribute("href", "/sessions");
  });

  it("Chat badge counts failed journey tests too (same number as the desktop sidebar)", async () => {
    user.current = { id: "u1", role: "admin" };
    data.approvals = [{ id: "a1", status: "pending" }];
    data.journeys = {
      status: "red", finished_at: new Date().toISOString(), commit: "abc", total: 3, passed: 1,
      failed: ["J-usage", "J-setup"], known_gaps: 0, reason: null,
    };
    renderNav();
    await waitFor(() => expect(screen.getByTestId("chat-badge")).toHaveTextContent("3"));
    user.current = null;
  });

  it("shows no badge when nothing waits", async () => {
    renderNav();
    await waitFor(() => expect(within(bar()).getByRole("link", { name: "Chat" })).toBeInTheDocument());
    expect(screen.queryByTestId("chat-badge")).toBeNull();
  });

  it("lights Chat on the chat page and on the start route", () => {
    nav.pathname = "/sessions";
    renderNav();
    expect(within(bar()).getByRole("link", { name: "Chat" })).toHaveAttribute("aria-current", "page");
    expect(within(bar()).getByRole("link", { name: "Models" })).not.toHaveAttribute("aria-current");
  });

  it("lights Models on runtimes and bench, Knowledge on memory and skills", () => {
    nav.pathname = "/bench";
    const { unmount } = renderNav();
    expect(within(bar()).getByRole("link", { name: "Models" })).toHaveAttribute("aria-current", "page");
    unmount();
    nav.pathname = "/memory";
    renderNav();
    expect(within(bar()).getByRole("link", { name: "Knowledge" })).toHaveAttribute("aria-current", "page");
  });

  it("lights nothing on a page that left the bar (Tasks)", () => {
    nav.pathname = "/tasks";
    renderNav();
    expect(within(bar()).queryAllByRole("link").filter((a) => a.getAttribute("aria-current"))).toEqual([]);
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
  it("shows exactly New job · Voice · Test a model · last chat · Pages › · Settings ›", async () => {
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
    await userEvent.click(screen.getByTestId("open-sheet"));
    const sheet = screen.getByRole("dialog", { name: "Menu" });

    // last chat: one row, the newest chat whose agent still exists
    await waitFor(() => expect(screen.getByTestId("quick-last-chat")).toBeInTheDocument());
    expect(screen.getByTestId("quick-last-chat")).toHaveAttribute("href", "/sessions?agent=a1&enter=1");
    expect(screen.getByTestId("quick-last-chat")).toHaveTextContent("Continue with Lead");
    expect(within(sheet).queryByText(/Helper/)).toBeNull();

    const rows = [...screen.getByTestId("quick-root").querySelectorAll("a, button")].map(
      (e) => e.getAttribute("data-testid"),
    );
    expect(rows).toEqual(["quick-new-job", "quick-voice", "quick-bench-test", "quick-last-chat", "quick-pages", "quick-settings"]);
    expect(screen.getByTestId("quick-settings")).toHaveAttribute("href", "/settings");

    // calm: no theme switch, no tile grid, no "More…", no visible title
    expect(within(sheet).queryByRole("radiogroup")).toBeNull();
    expect(within(sheet).queryByText("More…")).toBeNull();
    expect(screen.queryByTestId("quick-areas")).toBeNull();
    expect(within(sheet).queryByRole("heading")).toBeNull();
  });

  it("Test a model opens the bench test sheet for the running model (bench W1 P2)", async () => {
    renderNav();
    await userEvent.click(screen.getByTestId("open-sheet"));
    const row = screen.getByTestId("quick-bench-test");
    expect(row).toHaveTextContent("Test a model");
    expect(row).toHaveTextContent("The running model, everything pre-filled");
    expect(row).toHaveAttribute("href", "/bench?test=1&from=quick");
    row.addEventListener("click", (e) => e.preventDefault());
    await userEvent.click(row);
    await waitFor(() => expect(screen.queryByTestId("quick-sheet")).toBeNull());
  });

  it("New job closes the sheet and opens the New-task modal", async () => {
    renderNav();
    await userEvent.click(screen.getByTestId("open-sheet"));
    await userEvent.click(screen.getByTestId("quick-new-job"));
    await waitFor(() => expect(screen.queryByTestId("quick-sheet")).toBeNull());
    expect(screen.getByTestId("create-task-modal")).toHaveAttribute("data-request", "1");
  });

  it("Voice command starts the voice assistant", async () => {
    renderNav();
    await userEvent.click(screen.getByTestId("open-sheet"));
    await userEvent.click(screen.getByTestId("quick-voice"));
    expect(voice.toggleButton).toHaveBeenCalledTimes(1);
  });

  it("with the live voice on, the voice row is Jarvis (classic voice) and still starts Jarvis", async () => {
    live.lead = { agentId: "lead-1", agentName: "Chief" };
    try {
      renderNav();
      await userEvent.click(screen.getByTestId("open-sheet"));
      const row = screen.getByTestId("quick-voice");
      expect(row).toHaveTextContent("Jarvis (classic voice)");
      expect(row).not.toHaveTextContent("Voice command");
      voice.toggleButton.mockClear();
      await userEvent.click(row);
      expect(voice.toggleButton).toHaveBeenCalledTimes(1);
    } finally {
      live.lead = null;
    }
  });

  it("with the live voice off, the voice row stays as it was", async () => {
    renderNav();
    await userEvent.click(screen.getByTestId("open-sheet"));
    expect(screen.getByTestId("quick-voice")).not.toHaveTextContent("Jarvis");
  });

  it("hides the last-chat row when this device has no recent chat", async () => {
    renderNav();
    await userEvent.click(screen.getByTestId("open-sheet"));
    expect(screen.queryByTestId("quick-last-chat")).toBeNull();
    expect(mem["mc-recent-chats"]).toBeUndefined();
  });
});

describe("⊕ sheet — variant B, level 2 Pages", () => {
  it("Pages › opens every page outside the tab bar, Used most first", async () => {
    renderNav();
    await userEvent.click(screen.getByTestId("open-sheet"));
    await userEvent.click(screen.getByTestId("quick-pages"));
    const level = screen.getByTestId("quick-pages-level");
    expect(within(level).getByRole("heading", { name: "Pages" })).toBeInTheDocument();
    const hrefs = [...level.querySelectorAll("a[data-page]")].map((a) => a.getAttribute("href"));
    // Runtimes is a tab now (Models), so "Used most" starts at Agents
    expect(hrefs.slice(0, 2)).toEqual(["/agents", "/insights"]);
    // the rest alphabetical by label — Home, Inbox and Tasks left the bar;
    // Office and Loops are frozen (Etappe 2) and offered nowhere
    expect(hrefs.slice(2)).toEqual([
      "/bench", "/files", "/home", "/inbox", "/repos", "/schedule", "/tasks",
    ]);
    // tab-bar places, pages under a tab (Memory, Skills) and Settings are not repeated
    for (const h of ["/sessions", "/runtimes", "/knowledge", "/settings", "/memory", "/skills"]) {
      expect(hrefs).not.toContain(h);
    }
  });

  it("‹ Back returns to level 1", async () => {
    renderNav();
    await userEvent.click(screen.getByTestId("open-sheet"));
    await userEvent.click(screen.getByTestId("quick-pages"));
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByTestId("quick-root")).toBeInTheDocument();
    expect(screen.queryByTestId("quick-pages-level")).toBeNull();
  });

  it("Esc on level 2 goes back one level, Esc on level 1 closes", async () => {
    renderNav();
    await userEvent.click(screen.getByTestId("open-sheet"));
    await userEvent.click(screen.getByTestId("quick-pages"));
    await userEvent.keyboard("{Escape}");
    expect(screen.getByTestId("quick-root")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("quick-sheet")).toBeNull());
  });

  it("reopening always starts on level 1", async () => {
    renderNav();
    await userEvent.click(screen.getByTestId("open-sheet"));
    await userEvent.click(screen.getByTestId("quick-pages"));
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByTestId("quick-sheet")).toBeNull());
    await userEvent.click(screen.getByTestId("open-sheet"));
    expect(screen.getByTestId("quick-root")).toBeInTheDocument();
  });
});
