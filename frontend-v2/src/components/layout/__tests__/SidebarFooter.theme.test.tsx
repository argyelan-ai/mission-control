/**
 * The user menu (sidebar footer) carries the theme switch in BOTH states —
 * expanded and collapsed — next to Settings and Logout.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { setTheme } from "@/lib/theme";

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));
vi.mock("@/lib/api", () => ({
  api: { system: { status: () => Promise.resolve({ components: { database: { status: "ok" }, redis: { status: "ok" } } }) } },
  clearToken: vi.fn(),
}));
vi.mock("@/lib/store", () => ({
  useAppStore: () => ({ currentUser: { name: "Op" }, sidebarCollapsed: false, toggleSidebar: vi.fn() }),
}));

import SidebarFooter from "../SidebarFooter";

function renderFooter(collapsed: boolean) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SidebarFooter collapsed={collapsed} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  const m = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
  });
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  act(() => setTheme("dark"));
});

describe("SidebarFooter theme switch", () => {
  it.each([false, true])("is present and switches the theme (collapsed=%s)", (collapsed) => {
    renderFooter(collapsed);
    const btn = screen.getByTestId("theme-cycle");
    expect(btn).toHaveAttribute("aria-label", "Theme: Dark — switch to Light");
    fireEvent.click(btn);
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });
});
