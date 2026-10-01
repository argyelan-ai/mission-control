/**
 * Phone navigation V2 „Schnell-Knopf": Home · Tasks · ⊕ New · Chats · Inbox.
 *
 * Pins the behaviour the operator chose: five labelled places, the Inbox
 * badge is the real count from lib/inbox.ts (reviews + approvals + open head
 * questions), Chats opens the last chat directly and leads back to the list
 * on the chats page, ⊕ opens the sheet (new job, voice, last chats, all
 * areas — "More…" opens the full menu), and the bar steps aside for the
 * keyboard.
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
    expect(within(bar()).getByRole("link", { name: "Chats" })).toHaveAttribute("href", "/sessions?agent=agent-7");
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

describe("⊕ sheet", () => {
  it("offers new job, voice, the last chats and all areas", async () => {
    localStorage.setItem(
      "mc-recent-chats",
      JSON.stringify([
        { kind: "agent", id: "a1" },
        { kind: "agent", id: "gone" },
        { kind: "agent", id: "a2" },
      ]),
    );
    data.hostAgents = [
      { id: "a1", name: "Lead" },
      { id: "a2", name: "Helper" },
    ];
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    const sheet = screen.getByRole("dialog", { name: "New" });
    expect(within(sheet).getByText("New job")).toBeInTheDocument();
    expect(within(sheet).getByText("Voice command")).toBeInTheDocument();

    // last chats: names resolved, a chat whose agent is gone is skipped
    await waitFor(() => expect(within(sheet).getByRole("link", { name: "Lead" })).toBeInTheDocument());
    expect(within(sheet).getByRole("link", { name: "Lead" })).toHaveAttribute("href", "/sessions?agent=a1");
    expect(within(sheet).getByRole("link", { name: "Helper" })).toHaveAttribute("href", "/sessions?agent=a2");

    const areas = within(screen.getByTestId("quick-areas"));
    for (const [name, href] of [
      ["Runtimes", "/runtimes"],
      ["Insights", "/insights"],
      ["Agents", "/agents"],
      ["Memory", "/memory"],
      ["Schedule", "/schedule"],
    ]) {
      expect(areas.getByRole("link", { name })).toHaveAttribute("href", href);
    }
    expect(areas.getByRole("button", { name: "More…" })).toBeInTheDocument();
  });

  it("New job closes the sheet and opens the New-task modal", async () => {
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByTestId("quick-new-job"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "New" })).toBeNull());
    expect(screen.getByTestId("create-task-modal")).toHaveAttribute("data-request", "1");
  });

  it("Voice command starts the voice assistant", async () => {
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByTestId("quick-voice"));
    expect(voice.toggleButton).toHaveBeenCalledTimes(1);
  });

  it("More… opens the full menu (all routes, board, account)", async () => {
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByRole("button", { name: "More…" }));
    expect(screen.getByRole("button", { name: "Close menu" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "New" })).toBeNull());
  });

  it("hides the chat chips when this device has no recent chat", async () => {
    renderNav();
    await userEvent.click(within(bar()).getByRole("button", { name: "New" }));
    expect(screen.queryByTestId("quick-chats")).toBeNull();
    expect(mem["mc-recent-chats"]).toBeUndefined();
  });
});
