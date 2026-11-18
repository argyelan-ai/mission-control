/**
 * Phone overlays close on Esc — the UI probe found the menu stayed open at
 * 390 px (panel register rule 4: every overlay closes on Esc). Since variant
 * 2026-11-18 the chat drawer is the phone menu; Esc closes it.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.hoisted(() => {
  const mem: Record<string, string> = {};
  Object.defineProperty(globalThis, "localStorage", {
    value: {
      getItem: (k: string) => mem[k] ?? null,
      setItem: (k: string, v: string) => { mem[k] = v; },
      removeItem: (k: string) => { delete mem[k]; },
      clear: () => undefined,
    },
    configurable: true,
    writable: true,
  });
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/agents",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/api", () => ({
  clearToken: vi.fn(),
  api: {
    approvals: { list: vi.fn(async () => []) },
    boards: { list: vi.fn(async () => []) },
    heads: { list: vi.fn(async () => ({ runs: [] })) },
    tasks: { list: vi.fn(async () => []), comments: { list: vi.fn(async () => []) } },
    agents: {
      list: vi.fn(async () => []),
      listDockerSessions: vi.fn(async () => []),
      listHostSessions: vi.fn(async () => [{ id: "a1", name: "Lead", status: "idle" }]),
    },
    groups: { list: vi.fn(async () => []) },
  },
}));

vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceButton: () => null,
  useVoiceContext: () => ({ toggleButton: vi.fn() }),
}));

import MobileNav, { MobileNavProvider } from "../MobileNav";
import { ChatDrawer } from "../ChatDrawer";

describe("phone menu", () => {
  it("the chat drawer closes on Esc", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MobileNavProvider>
          <MobileNav />
          <ChatDrawer />
        </MobileNavProvider>
      </QueryClientProvider>,
    );

    await userEvent.click(screen.getByTestId("drawer-button"));
    expect(screen.getByRole("dialog", { name: "Chats" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});
