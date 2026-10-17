/**
 * AppShell — a signed-in admin whose setup wizard is still pending is sent to
 * /setup on page load (finding 17.10.2026: the fresh install landed on an
 * empty Home after the registration). An install whose wizard is done is
 * never redirected.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, waitFor } from "@testing-library/react";

const replace = vi.fn();
const firstRun = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/api", () => ({
  getToken: () => "t",
  getStoredUser: () => ({ id: "u1", email: "op@example.com", name: "Op", role: "admin" }),
  setStoredUser: vi.fn(),
  api: {
    auth: {
      me: () => Promise.resolve({ id: "u1", email: "op@example.com", name: "Op", role: "admin" }),
      firstRun: () => firstRun(),
    },
  },
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
vi.mock("@/lib/boards", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/boards")>()),
  useBoardsBootstrap: () => {},
}));

import AppShell from "../AppShell";
import { _resetFirstRunCheckForTests } from "@/lib/firstRun";

beforeEach(() => {
  vi.clearAllMocks();
  _resetFirstRunCheckForTests();
});

describe("AppShell first-run redirect", () => {
  it("sends the admin to /setup while the wizard is pending", async () => {
    firstRun.mockResolvedValue({ wizard_pending: true });
    render(<AppShell>content</AppShell>);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/setup"));
  });

  it("never redirects once the wizard is done", async () => {
    firstRun.mockResolvedValue({ wizard_pending: false });
    render(<AppShell>content</AppShell>);
    await waitFor(() => expect(firstRun).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 0));
    expect(replace).not.toHaveBeenCalled();
  });
});
