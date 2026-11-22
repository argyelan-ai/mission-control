/** /more — its own page: own title, two titled groups, no tab duplicates. */
import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";

vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children, mobileTitle }: { children: React.ReactNode; mobileTitle?: string }) => (
    <div data-mobile-title={mobileTitle}>{children}</div>
  ),
}));

import MorePage from "../page";

describe("MorePage", () => {
  it("is titled More, not Settings", () => {
    render(<MorePage />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("More");
    expect(screen.queryByText("Settings")).toBeNull();
    expect(document.querySelector("[data-mobile-title]")).toHaveAttribute("data-mobile-title", "More");
  });

  it("shows the two groups Used most and Others, each titled", () => {
    render(<MorePage />);
    const used = screen.getByRole("region", { name: "Used most" });
    const others = screen.getByRole("region", { name: "Others" });
    expect(within(used).getByRole("link", { name: /Agents/ })).toHaveAttribute("href", "/agents");
    expect(within(others).getByRole("link", { name: /Schedule/ })).toHaveAttribute("href", "/schedule");
  });

  it("offers no page the tabs already reach", () => {
    render(<MorePage />);
    const hrefs = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
    for (const h of ["/memory", "/skills", "/knowledge", "/runtimes", "/sessions", "/settings"]) {
      expect(hrefs).not.toContain(h);
    }
  });
});
