/**
 * PanelRail — Task B6 vitest.
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
  it("renders one button per panel", () => {
    render(<PanelRail active={null} onSelect={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Terminal" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Diff" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Browser" })).toBeInTheDocument();
  });

  it("marks the active panel as pressed", () => {
    render(<PanelRail active="terminal" onSelect={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Terminal" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Diff" })).toHaveAttribute("aria-pressed", "false");
  });

  it("selecting a different panel calls onSelect with that panel", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<PanelRail active="terminal" onSelect={onSelect} />);

    await user.click(screen.getByRole("button", { name: "Diff" }));
    expect(onSelect).toHaveBeenCalledWith("diff");
  });

  it("clicking the already-active panel collapses it (onSelect(null))", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<PanelRail active="browser" onSelect={onSelect} />);

    await user.click(screen.getByRole("button", { name: "Browser" }));
    expect(onSelect).toHaveBeenCalledWith(null);
  });
});
