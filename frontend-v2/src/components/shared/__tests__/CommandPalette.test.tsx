import React from "react";
import { describe, it, expect, vi, beforeAll } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import en from "../../../../messages/en.json";

// The palette used to carry "Approve all approvals": one Enter resolved every
// open approval (blockers included) with no confirmation and no undo. It also
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
}));

const state = vi.hoisted(() => ({
  commandPaletteOpen: true,
  activeBoardId: null,
  setCommandPaletteOpen: vi.fn(),
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector?: (s: typeof state) => unknown) => (selector ? selector(state) : state),
}));

const apiMock = vi.hoisted(() => ({
  agents: { list: vi.fn(async () => []) },
  approvals: { list: vi.fn(async () => []), resolve: vi.fn() },
}));
vi.mock("@/lib/api", () => ({ api: apiMock }));

// Voice PR 5: Jarvis is reachable from the palette while the sidebar mic is
// the lead agent's live voice. Default: no lead → no Jarvis item.
const live = vi.hoisted(() => ({ lead: null as null | { agentId: string; agentName: string } }));
vi.mock("@/components/voice/live/VoiceLiveCall", () => ({
  useVoiceLiveCall: () => (live.lead ? { lead: live.lead } : null),
}));
const jarvis = vi.hoisted(() => ({ toggleButton: vi.fn() }));
vi.mock("@/components/voice/VoiceWidget", () => ({ useVoiceContext: () => jarvis }));

import CommandPalette from "../CommandPalette";

beforeAll(() => {
  // cmdk measures its list; jsdom has no ResizeObserver.
  if (!("ResizeObserver" in globalThis)) {
    (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

function renderPalette() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CommandPalette />
    </QueryClientProvider>
  );
}

describe("CommandPalette — no one-keystroke bulk approval", () => {
  it("offers no 'Approve all approvals' action", () => {
    renderPalette();
    expect(screen.queryByText("Approve all approvals")).not.toBeInTheDocument();
  });

  it("offers 'Open inbox' in its place, which only navigates", () => {
    renderPalette();
    const item = screen.getByText(en.shell.openInbox);
    item.click();
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
