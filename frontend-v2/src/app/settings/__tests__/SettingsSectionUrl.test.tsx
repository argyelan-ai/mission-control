/**
 * /settings — the chosen section lives in the URL, and only the content
 * column scrolls (the section nav stays in view on long sections).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  usePathname: () => "/settings",
  useSearchParams: () => new URLSearchParams("section=shortcuts"),
}));

const shellProps = vi.hoisted(() => ({ last: {} as Record<string, unknown> }));
vi.mock("@/components/layout/AppShell", () => ({
  __esModule: true,
  default: ({ children, ...props }: { children: React.ReactNode } & Record<string, unknown>) => {
    shellProps.last = props;
    return <>{children}</>;
  },
}));

const state = {
  currentUser: { id: "u1", email: "a@b.com", name: "Operator", role: "member" },
};
vi.mock("@/lib/store", () => ({
  useAppStore: (selector?: (s: typeof state) => unknown) => (selector ? selector(state) : state),
}));

import SettingsPage from "../page";

beforeEach(() => replace.mockReset());

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsPage />
    </QueryClientProvider>,
  );
}

describe("Settings section navigation", () => {
  it("writes the clicked section into the URL so a reload lands there", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: "About" }));
    expect(replace).toHaveBeenCalledWith("/settings?section=about", { scroll: false });
  });

  it("runs full-height so only the content column scrolls and the nav stays put", () => {
    renderPage();
    expect(shellProps.last.fullHeight).toBe(true);
  });

  // On a phone the app bar and the bottom tab bar already take height, so the
  // whole page scrolls as one (bare overflow-y-auto); only from md up does it
  // switch to "content column scrolls, nav stays put".
  it("scrolls the phone screen as one; pins the nav from md up", () => {
    renderPage();
    const heading = screen.getByRole("heading", { level: 1 });
    let scroller: HTMLElement | null = heading.parentElement;
    while (scroller && !scroller.className.split(/\s+/).includes("overflow-y-auto")) {
      scroller = scroller.parentElement;
    }
    expect(scroller).not.toBeNull();
    expect(scroller!.className.split(/\s+/)).toContain("md:overflow-hidden");
    expect(scroller!.contains(screen.getByRole("button", { name: "About" }))).toBe(true);
    expect(document.querySelector(".md\\:overflow-y-auto.p-4")).not.toBeNull();
  });

  // The side nav is the desktop's; the phone gets the section list instead
  // (SettingsPhoneSections.test.tsx). Without `hidden md:block` the old
  // wrapping button cloud would come back on the phone.
  it("shows the side nav from md up only", () => {
    renderPage();
    const nav = screen.getByRole("button", { name: "About" }).closest("nav")!;
    expect(nav.className.split(/\s+/)).toEqual(expect.arrayContaining(["hidden", "md:block"]));
  });
});
