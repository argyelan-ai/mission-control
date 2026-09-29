/**
 * Sidebar — the "Agents" row renders the /agents truth, not the online counter.
 *
 * The row read "14/14" while 12 of 14 agents were paused. Rendered with a
 * metrics answer where online (14) and active (2) differ, the row must show
 * "2/14" with the /agents label as tooltip.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/api", () => ({
  api: {
    system: {
      metrics: () =>
        Promise.resolve({
          tasks: { total: 0, active: 0 },
          agents: { total: 14, active: 2, paused: 12, online: 14 },
          approvals: { pending: 0 },
        }),
    },
  },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: () => ({ sidebarCollapsed: false, setCommandPaletteOpen: vi.fn() }),
}));
vi.mock("@/components/voice/VoiceWidget", () => ({ VoiceButton: () => null }));
vi.mock("../BoardPicker", () => ({ __esModule: true, default: () => null }));
vi.mock("../SidebarFooter", () => ({ __esModule: true, default: () => null }));

import Sidebar from "../Sidebar";

describe("Sidebar agents badge", () => {
  it("shows active/total with the /agents label, not online/total", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <Sidebar />
      </QueryClientProvider>,
    );
    const badge = await screen.findByText("2/14");
    expect(badge.getAttribute("title")).toBe("2 active · 12 paused");
    expect(screen.queryByText("14/14")).toBeNull();
  });
});
