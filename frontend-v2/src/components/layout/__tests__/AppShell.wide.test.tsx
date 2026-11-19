/**
 * AppShell — `wide` (25.10.2026). The Benchmark pages lay out their own
 * panes and reading widths, so they may use the whole width beside the
 * sidebar. Every other page keeps the centred 1600 px column — its left edge
 * must not move.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
vi.mock("@/lib/boards", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/boards")>()),
  useBoardsBootstrap: () => {},
}));
import AppShell from "../AppShell";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/bench",
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/api", () => ({
  getToken: () => "t",
  getStoredUser: () => ({ id: "u", username: "demo" }),
  api: {},
}));
vi.mock("@/lib/store", () => ({ useAppStore: () => ({ setCurrentUser: vi.fn() }) }));
vi.mock("@/hooks/useKeyboardShortcuts", () => ({ useKeyboardShortcuts: vi.fn() }));
vi.mock("../MobileNav", () => ({
  __esModule: true,
  default: () => null,
  MobileNavProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("../MobileTabBar", () => ({ MobileTabBar: () => null }));
vi.mock("../QuickSheet", () => ({ QuickSheet: () => null }));
vi.mock("../Sidebar", () => ({ __esModule: true, default: () => null }));
vi.mock("../AmbientBackground", () => ({ AmbientBackground: () => null }));
vi.mock("@/components/shared/CommandPalette", () => ({ __esModule: true, default: () => null }));
vi.mock("@/components/shared/ToastRenderer", () => ({ __esModule: true, default: () => null }));
vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  VoiceOverlay: () => null,
}));
vi.mock("@/components/voice/live/VoiceLiveCall", () => ({ FloatingVoiceCall: () => null }));

const wrapOf = () => screen.getByTestId("page").parentElement!;

describe("AppShell — wide", () => {
  for (const fullHeight of [false, true]) {
    const mode = fullHeight ? "full-height" : "scrolling";
    it(`${mode}: by default the page sits in the centred 1600 px column`, async () => {
      render(<AppShell fullHeight={fullHeight}><div data-testid="page" /></AppShell>);
      const wrap = (await screen.findByTestId("page")).parentElement!;
      expect(wrap.className).toMatch(/(^|\s)max-w-\[1600px\](\s|$)/);
      expect(wrap.className).toMatch(/(^|\s)mx-auto(\s|$)/);
    });

    it(`${mode}: wide lifts the cap and nothing else`, async () => {
      render(<AppShell fullHeight={fullHeight} wide><div data-testid="page" /></AppShell>);
      await screen.findByTestId("page");
      expect(wrapOf().className).not.toMatch(/max-w-/);
      expect(wrapOf().className).toMatch(/(^|\s)w-full(\s|$)/);
    });
  }
});
