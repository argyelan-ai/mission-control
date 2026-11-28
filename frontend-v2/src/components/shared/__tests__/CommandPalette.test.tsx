import React from "react";
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import en from "../../../../messages/en.json";

// The search box (concept "Neuer Chat und Suche", P5): empty = recent chats,
// typing = Chats → In messages → Commands → Pages; an agent opens its chat.
//
// History kept: the palette used to carry "Approve all approvals" — one
// Enter resolved every open approval with no confirmation and no undo — and
// advertised shortcuts (Cmd+N, Cmd+Shift+A) that no handler implements.

const nav = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: nav.push, replace: vi.fn() }),
  usePathname: () => "/",
}));

vi.mock("framer-motion", () => ({
  motion: new Proxy(
    {},
    {
      get:
        (_t, tag: string) =>
        ({ children, initial: _i, animate: _a, exit: _e, transition: _tr, ...rest }: Record<string, unknown>) =>
          React.createElement(tag, rest, children as React.ReactNode),
    }
  ),
  AnimatePresence: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
  useReducedMotion: () => true,
}));

const state = vi.hoisted(() => ({
  commandPaletteOpen: true,
  activeBoardId: null,
  setCommandPaletteOpen: vi.fn(),
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector?: (s: typeof state) => unknown) => (selector ? selector(state) : state),
}));

type Hit = { chatId: string; agentId: string | null; snippet: string; highlight: [number, number] };
const rec = vi.hoisted(() => ({
  chats: [] as unknown[],
  search: { hits: [] as Hit[], enabled: true } as unknown,
  searchError: null as Error | null,
}));
const apiMock = vi.hoisted(() => ({
  agents: { list: vi.fn(async () => []) },
  approvals: { list: vi.fn(async () => []), resolve: vi.fn() },
  chatRecord: {
    listChats: vi.fn(async () => ({ chats: rec.chats })),
    search: vi.fn(async (_q: string) => {
      if (rec.searchError) throw rec.searchError;
      return rec.search;
    }),
  },
}));
vi.mock("@/lib/api", () => ({ api: apiMock }));

// The chat list's own data (drawer + desktop column) — the palette reads it.
const list = vi.hoisted(() => ({
  waiting: [] as unknown[],
  today: [] as unknown[],
  earlier: [] as unknown[],
}));
vi.mock("@/components/layout/useChatList", () => ({
  useChatList: () => ({ ...list, waitingCount: 0, loaded: true }),
}));

// Voice PR 5: Jarvis is reachable from the palette while the sidebar mic is
// the lead agent's live voice. Default: no lead → no Jarvis item.
const live = vi.hoisted(() => ({ lead: null as null | { agentId: string; agentName: string } }));
vi.mock("@/components/voice/live/VoiceLiveCall", () => ({
  useVoiceLiveCall: () => (live.lead ? { lead: live.lead } : null),
}));
const jarvis = vi.hoisted(() => ({ toggleButton: vi.fn() }));
vi.mock("@/components/voice/VoiceWidget", () => ({ useVoiceContext: () => jarvis }));

import CommandPalette from "../CommandPalette";

const row = (key: string, title: string, at: string, extra: Record<string, unknown> = {}) => {
  const [kind, id] = key.split(":");
  return {
    key,
    kind: kind === "group" ? "group" : "agent",
    id,
    title,
    at,
    state: "idle",
    href: kind === "group" ? `/sessions?group=${id}&from=link` : `/sessions?agent=${id}&from=link`,
    ...extra,
  };
};

beforeAll(() => {
  // cmdk measures its list; jsdom has no ResizeObserver / scrollIntoView.
  if (!("ResizeObserver" in globalThis)) {
    (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
  Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? function () {};
});

beforeEach(() => {
  nav.push.mockClear();
  state.setCommandPaletteOpen.mockClear();
  apiMock.chatRecord.search.mockClear();
  rec.chats = [];
  rec.search = { hits: [], enabled: true };
  rec.searchError = null;
  list.waiting = [];
  list.today = [
    row("agent:a1", "Sparky", "2026-11-28T10:00:00Z"),
    row("chat:c2", "Cooling stand", "2026-11-28T09:00:00Z", { id: "a1", chatId: "c2", href: "/sessions?agent=a1&from=link&chat=c2" }),
  ];
  list.earlier = [
    row("agent:a2", "Hermes", "2026-11-20T10:00:00Z"),
    row("group:g1", "Release crew", "2026-11-19T10:00:00Z"),
  ];
});

afterEach(() => {
  vi.useRealTimers();
});

function renderPalette() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CommandPalette />
    </QueryClientProvider>
  );
}

const input = () => screen.getByRole("combobox");
const group = (testId: string) => screen.queryByTestId(testId);

describe("CommandPalette — empty box = recent chats", () => {
  it("shows the recent chats, no pages", () => {
    renderPalette();
    const recent = group("palette-group-recent")!;
    expect(recent).toBeInTheDocument();
    expect(within(recent).getAllByTestId("palette-chat").map((el) => el.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining("Sparky"), expect.stringContaining("Hermes")]),
    );
    expect(group("palette-group-pages")).toBeNull();
    // the settings gear appears only while searching — not on every recent row
    expect(screen.queryByTestId("palette-agent-settings")).toBeNull();
  });

  it("puts a pinned chat first", async () => {
    rec.chats = [{ id: "d2", agentDefault: true, legacyAgentId: "a2", pinned: true }];
    renderPalette();
    await waitFor(() => {
      const first = within(group("palette-group-recent")!).getAllByTestId("palette-chat")[0];
      expect(first).toHaveTextContent("Hermes");
    });
  });
});

describe("CommandPalette — typing: groups and ranking", () => {
  it("'Chats' finds by title, an agent hit opens its chat — not its settings page", () => {
    renderPalette();
    fireEvent.change(input(), { target: { value: "spar" } });
    const chats = group("palette-group-chats")!;
    const items = within(chats).getAllByTestId("palette-chat");
    expect(items[0]).toHaveTextContent("Sparky");
    fireEvent.click(items[0]);
    expect(nav.push).toHaveBeenCalledWith("/sessions?agent=a1&from=link");
    expect(nav.push).not.toHaveBeenCalledWith("/agents/a1");
  });

  it("a titled further chat is found by its agent's name, below the agent's own chat", () => {
    renderPalette();
    fireEvent.change(input(), { target: { value: "spar" } });
    const items = within(group("palette-group-chats")!).getAllByTestId("palette-chat");
    expect(items.map((el) => el.getAttribute("data-key"))).toEqual(["agent:a1", "chat:c2"]);
  });

  it("the agent's settings are a second action on its row", () => {
    renderPalette();
    fireEvent.change(input(), { target: { value: "hermes" } });
    fireEvent.click(screen.getByRole("button", { name: en.shell.searchAgentSettings.replace("{name}", "Hermes") }));
    expect(nav.push).toHaveBeenCalledWith("/agents/a2");
  });

  it("groups stand in the order Chats → Commands → Pages", () => {
    renderPalette();
    fireEvent.change(input(), { target: { value: "se" } });
    const order = screen.getAllByTestId(/^palette-group-/).map((el) => el.getAttribute("data-testid"));
    expect(order.indexOf("palette-group-pages")).toBe(order.length - 1);
  });

  it("'Commands' holds New chat, which opens the draft", () => {
    renderPalette();
    fireEvent.change(input(), { target: { value: "new" } });
    fireEvent.click(within(group("palette-group-commands")!).getByText(en.cockpit.newChat));
    expect(nav.push).toHaveBeenCalledWith("/sessions?new=1");
  });

  it("no fake 'New task' command that only opens a page", () => {
    renderPalette();
    fireEvent.change(input(), { target: { value: "new" } });
    expect(screen.queryByText(en.shell.newTask)).toBeNull();
  });

  it("nothing matches → 'No results'", async () => {
    renderPalette();
    fireEvent.change(input(), { target: { value: "zz" } });
    expect(await screen.findByText(en.shell.noResults)).toBeInTheDocument();
  });
});

describe("CommandPalette — 'In messages' (server search)", () => {
  it("asks only from three characters and after the 250 ms pause", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    renderPalette();
    fireEvent.change(input(), { target: { value: "kü" } });
    await act(async () => { vi.advanceTimersByTime(400); });
    expect(apiMock.chatRecord.search).not.toHaveBeenCalled();
    fireEvent.change(input(), { target: { value: "küh" } });
    fireEvent.change(input(), { target: { value: "kühl" } });
    await act(async () => { vi.advanceTimersByTime(240); });
    expect(apiMock.chatRecord.search).not.toHaveBeenCalled();
    await act(async () => { vi.advanceTimersByTime(20); });
    expect(apiMock.chatRecord.search).toHaveBeenCalledTimes(1);
    expect(apiMock.chatRecord.search.mock.calls[0][0]).toBe("kühl");
  });

  it("shows one hit per chat with the word highlighted; a tap opens the chat", async () => {
    rec.chats = [{ id: "c2", agentDefault: false, legacyAgentId: "a1", pinned: false }];
    rec.search = {
      enabled: true,
      hits: [{ chatId: "c2", agentId: "a1", snippet: "…print the Kühlstand base", highlight: [11, 20] }],
    };
    renderPalette();
    fireEvent.change(input(), { target: { value: "kühl" } });
    const hit = await screen.findByTestId("palette-message-hit");
    expect(within(group("palette-group-messages")!).getAllByTestId("palette-message-hit")).toHaveLength(1);
    expect(hit.querySelector("mark")).toHaveTextContent("Kühlstand");
    expect(hit).toHaveTextContent("Cooling stand");
    fireEvent.click(hit);
    expect(nav.push).toHaveBeenCalledWith("/sessions?agent=a1&from=link&chat=c2");
  });

  it("record off (enabled: false) → the group stays hidden, quietly", async () => {
    rec.search = { hits: [], enabled: false };
    renderPalette();
    fireEvent.change(input(), { target: { value: "spark" } });
    await waitFor(() => expect(apiMock.chatRecord.search).toHaveBeenCalled());
    await act(async () => {});
    expect(group("palette-group-messages")).toBeNull();
    expect(group("palette-group-chats")).toBeInTheDocument();
  });

  it("once the record says it is off, typing on asks the server no more", async () => {
    rec.search = { hits: [], enabled: false };
    renderPalette();
    fireEvent.change(input(), { target: { value: "spark" } });
    await waitFor(() => expect(apiMock.chatRecord.search).toHaveBeenCalledTimes(1));
    fireEvent.change(input(), { target: { value: "sparky chat" } });
    await new Promise((r) => setTimeout(r, 400));
    expect(apiMock.chatRecord.search).toHaveBeenCalledTimes(1);
  });

  it("an older backend (404) → the group stays hidden, the rest works", async () => {
    rec.searchError = new Error('API 404: {"detail":"Not Found"}');
    renderPalette();
    fireEvent.change(input(), { target: { value: "spark" } });
    await waitFor(() => expect(apiMock.chatRecord.search).toHaveBeenCalled());
    await act(async () => {});
    expect(group("palette-group-messages")).toBeNull();
    expect(screen.queryByText(en.shell.searchSearching)).toBeNull();
    expect(group("palette-group-chats")).toBeInTheDocument();
  });
});

describe("CommandPalette — no one-keystroke bulk approval", () => {
  it("offers no 'Approve all approvals' action", () => {
    renderPalette();
    fireEvent.change(input(), { target: { value: "approv" } });
    expect(screen.queryByText("Approve all approvals")).not.toBeInTheDocument();
  });

  it("the inbox is a page that only navigates", () => {
    renderPalette();
    fireEvent.change(input(), { target: { value: "inbox" } });
    fireEvent.click(within(group("palette-group-pages")!).getByText("Inbox"));
    expect(nav.push).toHaveBeenCalledWith("/inbox");
    expect(apiMock.approvals.resolve).not.toHaveBeenCalled();
  });

  it("does not advertise shortcuts that no handler implements", () => {
    renderPalette();
    expect(screen.queryByText("Cmd+N")).not.toBeInTheDocument();
    expect(screen.queryByText("Cmd+Shift+A")).not.toBeInTheDocument();
  });
});

describe("CommandPalette — Jarvis (classic voice), voice PR 5", () => {
  it("offers Jarvis while the sidebar mic is the lead's live voice; selecting it starts Jarvis and closes", () => {
    live.lead = { agentId: "lead-1", agentName: "Chief" };
    try {
      renderPalette();
      const item = screen.getByTestId("palette-jarvis");
      expect(item).toHaveTextContent(en.voiceLive.jarvisClassic);
      item.click();
      expect(jarvis.toggleButton).toHaveBeenCalledTimes(1);
      expect(state.setCommandPaletteOpen).toHaveBeenCalledWith(false);
    } finally {
      live.lead = null;
    }
  });

  it("no Jarvis item when the live voice is off — the sidebar mic is Jarvis then", () => {
    renderPalette();
    expect(screen.queryByTestId("palette-jarvis")).toBeNull();
  });
});
