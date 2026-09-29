/**
 * AppShell — the signed-in user comes from the server, not only from the
 * login form's localStorage copy.
 *
 * With a token but no (or an outdated) `mc_user` entry, the shell showed "?"
 * as avatar, "—" as name and the wrong role until the next login. It now asks
 * /auth/me once and stores the answer.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, waitFor } from "@testing-library/react";

const setCurrentUser = vi.fn();
const me = vi.fn();
const setStoredUser = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/api", () => ({
  getToken: () => "t",
  getStoredUser: () => null,
  setStoredUser: (u: unknown) => setStoredUser(u),
  api: { auth: { me: () => me() } },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: () => ({ setCurrentUser }),
}));
vi.mock("@/hooks/useKeyboardShortcuts", () => ({ useKeyboardShortcuts: vi.fn() }));
vi.mock("../MobileNav", () => ({
  __esModule: true,
  default: () => null,
  MobileNavProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  MobileTabBar: () => null,
}));
vi.mock("../Sidebar", () => ({ __esModule: true, default: () => null }));
vi.mock("../AmbientBackground", () => ({ AmbientBackground: () => null }));
vi.mock("@/components/shared/CommandPalette", () => ({ __esModule: true, default: () => null }));
vi.mock("@/components/shared/ToastRenderer", () => ({ __esModule: true, default: () => null }));
vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  VoiceOverlay: () => null,
}));

import AppShell from "../AppShell";

beforeEach(() => vi.clearAllMocks());

describe("AppShell user refresh", () => {
  it("loads /auth/me when no user is stored and keeps the answer", async () => {
    const user = { id: "u1", email: "op@example.com", name: "Operator", role: "admin" };
    me.mockResolvedValue(user);
    render(<AppShell>content</AppShell>);
    await waitFor(() => expect(setCurrentUser).toHaveBeenCalledWith(user));
    expect(setStoredUser).toHaveBeenCalledWith(user);
  });
});
