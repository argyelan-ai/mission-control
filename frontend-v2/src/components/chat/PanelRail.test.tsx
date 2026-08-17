/**
 * PanelRail — Task B6 vitest (revised: Diff + Browser only — Terminal moved
 * to ChatView's own center-view toggle, see ChatView.test.tsx).
 *
 * Coverage: one button per panel, aria-pressed reflects `active`, clicking a
 * different panel selects it, and clicking the already-active panel
 * collapses it (active → null) — the "collapsible" behaviour, no separate
 * chevron control.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PanelRail } from "./PanelRail";

describe("PanelRail", () => {
  it("renders one button per panel — Diff and Browser only, no Terminal", () => {
    render(<PanelRail active={null} onSelect={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Diff" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Browser" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Terminal" })).not.toBeInTheDocument();
  });

  it("marks the active panel as pressed", () => {
    render(<PanelRail active="diff" onSelect={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Diff" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Browser" })).toHaveAttribute("aria-pressed", "false");
  });

  it("selecting a different panel calls onSelect with that panel", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<PanelRail active="diff" onSelect={onSelect} />);

    await user.click(screen.getByRole("button", { name: "Browser" }));
    expect(onSelect).toHaveBeenCalledWith("browser");
  });

  it("clicking the already-active panel collapses it (onSelect(null))", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<PanelRail active="browser" onSelect={onSelect} />);

    await user.click(screen.getByRole("button", { name: "Browser" }));
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it("is desktop-only and never a fixed bar (it used to cover the app's bottom nav)", () => {
    render(<PanelRail active={null} onSelect={vi.fn()} />);
    const rail = screen.getByRole("toolbar", { name: "Panels" });
    expect(rail.className).toContain("hidden");
    expect(rail.className).toContain("md:flex");
    expect(rail.className).not.toContain("fixed");
    expect(rail.className).not.toContain("bottom-0");
  });
});
