/**
 * Phone menu ("Open menu" in the tab bar) closes on Esc — the UI probe found
 * it stayed open at 390 px (panel register rule 4: every overlay closes on Esc).
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
  },
}));

vi.mock("@/components/voice/VoiceWidget", () => ({ VoiceButton: () => null }));

import MobileNav, { MobileNavProvider, MobileTabBar } from "../MobileNav";

describe("phone menu", () => {
  it("closes on Esc", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MobileNavProvider>
          <MobileNav />
          <MobileTabBar />
        </MobileNavProvider>
      </QueryClientProvider>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Open menu" }));
    expect(screen.getByRole("button", { name: "Close menu" })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Close menu" })).toBeNull());
  });
});
