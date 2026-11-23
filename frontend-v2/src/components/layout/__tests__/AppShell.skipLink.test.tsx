/**
 * AppShell — skip link to the message input (a11y): on a chat page the first
 * tab stop on the desktop jumps past the sidebar straight into the composer.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
vi.mock("@/lib/boards", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/boards")>()),
  useBoardsBootstrap: () => {},
}));
import AppShell from "../AppShell";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/sessions",
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
vi.mock("../ChatDrawer", () => ({ ChatDrawer: () => null, DrawerButton: () => null }));
vi.mock("../ChatSidebar", () => ({ ChatSidebar: () => null }));
vi.mock("../Sidebar", () => ({ __esModule: true, default: () => null }));
vi.mock("../AmbientBackground", () => ({ AmbientBackground: () => null }));
vi.mock("@/components/shared/CommandPalette", () => ({ __esModule: true, default: () => null }));
vi.mock("@/components/shared/ToastRenderer", () => ({ __esModule: true, default: () => null }));
vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  VoiceOverlay: () => null,
}));
vi.mock("@/components/voice/live/VoiceLiveCall", () => ({ FloatingVoiceCall: () => null }));

describe("AppShell — skip link to the composer", () => {
  it("is the first tab stop and puts the focus into the message input", async () => {
    render(
      <AppShell fullHeight skipToComposer>
        <a href="/elsewhere">a link in the page</a>
        <div data-testid="composer-dropzone">
          <textarea aria-label="Message" />
        </div>
      </AppShell>,
    );
    const user = userEvent.setup();
    await screen.findByRole("textbox", { name: "Message" });
    await user.tab();
    const skip = screen.getByRole("link", { name: "Skip to message input" });
    expect(skip).toHaveFocus();
    // desktop only: hidden below md, visible only while focused
    expect(skip.className).toMatch(/max-md:hidden/);
    expect(skip.className).toMatch(/\bsr-only\b/);
    await user.keyboard("{Enter}");
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
  });

  it("is not there on pages without a composer", async () => {
    render(<AppShell><div data-testid="page" /></AppShell>);
    await screen.findByTestId("page");
    expect(screen.queryByRole("link", { name: "Skip to message input" })).toBeNull();
  });
});
