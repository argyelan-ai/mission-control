/**
 * The pages of the new shell (operator decision 2026-11-18, ADR-102):
 * "/" lands in the last chat, Knowledge is a landing to Memory and Skills,
 * Settings › More reaches every page that left the main navigation.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";

const redirect = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({
  redirect,
  usePathname: () => "/knowledge",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/components/layout/AppShell", () => ({
  __esModule: true,
  default: ({ children }: { children: React.ReactNode }) => <div data-testid="shell">{children}</div>,
}));

import StartPage from "../page";
import KnowledgePage from "../knowledge/page";
import { MorePagesSection } from "@/components/settings/MorePagesSection";

const hrefs = (el: HTMLElement) => within(el).getAllByRole("link").map((a) => a.getAttribute("href"));

describe("start route", () => {
  it("/ goes to the chat page, which opens the last chat", () => {
    StartPage();
    expect(redirect).toHaveBeenCalledWith("/sessions");
  });
});

describe("Knowledge landing", () => {
  it("leads to the existing Memory and Skills pages, nothing else", () => {
    render(<KnowledgePage />);
    expect(screen.getByRole("heading", { level: 1, name: "Knowledge" })).toBeInTheDocument();
    expect(hrefs(screen.getByTestId("knowledge-links"))).toEqual(["/memory", "/skills"]);
  });
});

describe("Settings › More", () => {
  it("reaches every page that left the main navigation", () => {
    render(<MorePagesSection />);
    const links = hrefs(screen.getByTestId("settings-more-links"));
    for (const h of ["/home", "/tasks", "/inbox", "/office", "/loops", "/repos", "/schedule", "/agents", "/files"]) {
      expect(links).toContain(h);
    }
  });

  it("does not repeat the three tabs or Settings itself", () => {
    render(<MorePagesSection />);
    const links = hrefs(screen.getByTestId("settings-more-links"));
    for (const h of ["/sessions", "/runtimes", "/knowledge", "/settings"]) expect(links).not.toContain(h);
  });
});
