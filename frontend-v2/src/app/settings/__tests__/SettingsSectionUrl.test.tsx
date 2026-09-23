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
});
