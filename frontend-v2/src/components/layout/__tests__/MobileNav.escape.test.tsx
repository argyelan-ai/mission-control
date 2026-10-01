/**
 * Phone overlays close on Esc — the UI probe found the menu stayed open at
 * 390 px (panel register rule 4: every overlay closes on Esc). Since mobile
 * nav V2 the menu opens from the ⊕ sheet ("More…"); both must close on Esc.
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
  },
}));

vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceButton: () => null,
  useVoiceContext: () => ({ toggleButton: vi.fn() }),
}));

import MobileNav, { MobileNavProvider } from "../MobileNav";
import { MobileTabBar } from "../MobileTabBar";
import { QuickSheet } from "../QuickSheet";

describe("phone menu", () => {
  it("closes on Esc", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MobileNavProvider>
          <MobileNav />
          <MobileTabBar />
          <QuickSheet />
        </MobileNavProvider>
      </QueryClientProvider>,
    );

    // ⊕ sheet closes on Esc
    await userEvent.click(screen.getByRole("button", { name: "New" }));
    expect(screen.getByRole("dialog", { name: "New" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "New" })).toBeNull());

    // ⊕ → More… opens the full menu, which closes on Esc
    await userEvent.click(screen.getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByRole("button", { name: "More…" }));
    expect(screen.getByRole("button", { name: "Close menu" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Close menu" })).toBeNull());
  });
});
