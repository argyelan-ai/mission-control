/**
 * AppShell — `mobileChromeless` (Handy-Chat, 19.08.2026).
 *
 * Auf dem Chat-Bildschirm stapelten sich drei Leisten übereinander: die
 * App-Leiste oben (mit eigenem Titel), der Chat-Kopf (mit dem Agentennamen)
 * und die Tab-Leiste unten — dazu eine Polsterung von 88 px, die die
 * App-Leiste freihalten soll. In der Claude-App ist es EINE Leiste. Operator
 * hat sich für dasselbe entschieden ("das argyelan.ai header ganz oben
 * brauchen wir nicht wenn man im chat ist").
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import AppShell from "../AppShell";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/sessions",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/api", () => ({
  getToken: () => "t",
  getStoredUser: () => ({ id: "u", username: "mark" }),
  api: {},
}));

vi.mock("@/lib/store", () => ({
  useAppStore: () => ({ setCurrentUser: vi.fn() }),
}));

vi.mock("@/hooks/useKeyboardShortcuts", () => ({ useKeyboardShortcuts: vi.fn() }));

// Die schweren Nachbarn stehen hier nicht zur Debatte — sie werden auf
// erkennbare Platzhalter reduziert, damit der Test genau eine Frage stellt:
// ist die Leiste da oder nicht.
vi.mock("../MobileNav", () => ({
  __esModule: true,
  default: () => <div data-testid="mobile-appbar" />,
  MobileNavProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  MobileTabBar: () => <div data-testid="mobile-tabbar" />,
}));
vi.mock("../Sidebar", () => ({ __esModule: true, default: () => null }));
vi.mock("../WorkspaceSwitcher", () => ({ __esModule: true, default: () => null }));
vi.mock("../StatusBar", () => ({ __esModule: true, default: () => null }));
vi.mock("../TopBar", () => ({ __esModule: true, default: () => null }));
vi.mock("../AmbientBackground", () => ({ AmbientBackground: () => null }));
vi.mock("@/components/shared/CommandPalette", () => ({ __esModule: true, default: () => null }));
vi.mock("@/components/shared/ToastRenderer", () => ({ __esModule: true, default: () => null }));
vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  VoiceOverlay: () => null,
}));

describe("AppShell — mobileChromeless", () => {
  beforeEach(() => vi.clearAllMocks());

  it("zeigt normalerweise App-Leiste und Tab-Leiste", async () => {
    render(<AppShell fullHeight>inhalt</AppShell>);
    expect(await screen.findByTestId("mobile-appbar")).toBeTruthy();
    expect(screen.getByTestId("mobile-tabbar")).toBeTruthy();
  });

  it("blendet beide aus, wenn der Chat-Bildschirm sie übernimmt", async () => {
    render(<AppShell fullHeight mobileChromeless>inhalt</AppShell>);
    await screen.findByText("inhalt");
    expect(screen.queryByTestId("mobile-appbar")).toBeNull();
    expect(screen.queryByTestId("mobile-tabbar")).toBeNull();
  });

  it("nimmt die Polsterung mit, die nur die App-Leiste freihalten sollte", async () => {
    // Ohne das bliebe oben ein 88-px-Loch stehen, wo vorher die Leiste war —
    // die Seite waere kuerzer statt hoeher.
    const { container } = render(<AppShell fullHeight mobileChromeless>inhalt</AppShell>);
    await screen.findByText("inhalt");
    const main = container.querySelector("main");
    expect(main?.className).not.toContain("main-content-pt");
  });

  it("laesst die Polsterung stehen, wenn die Leiste da ist", async () => {
    const { container } = render(<AppShell fullHeight>inhalt</AppShell>);
    await screen.findByText("inhalt");
    const main = container.querySelector("main");
    expect(main?.className).toContain("main-content-pt");
  });
});
