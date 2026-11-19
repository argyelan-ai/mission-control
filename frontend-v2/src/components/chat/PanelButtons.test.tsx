/**
 * HeaderPanelButtons — the changes / browser buttons in the desktop chat
 * header (ADR-099; they replace the old PanelRail strip right of the chat).
 *
 * Coverage: one button per panel, aria-pressed reflects `active`, clicking a
 * different panel selects it, clicking the active one closes it, and the
 * group is desktop-only.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HeaderPanelButtons } from "./PanelButtons";

describe("HeaderPanelButtons", () => {
  it("renders Changes and Browser by default — no Terminal, no result doc", () => {
    render(<HeaderPanelButtons active={null} onToggle={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Changes" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Browser" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Terminal" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(2);
  });

  it("marks the active panel as pressed", () => {
    render(<HeaderPanelButtons active="diff" onToggle={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Changes" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Browser" })).toHaveAttribute("aria-pressed", "false");
  });

  it("selecting a different panel calls onToggle with that panel", async () => {
    const onToggle = vi.fn();
    render(<HeaderPanelButtons active="diff" onToggle={onToggle} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Browser" }));
    expect(onToggle).toHaveBeenCalledWith("browser");
  });

  it("clicking the active panel closes it (onToggle(null))", async () => {
    const onToggle = vi.fn();
    render(<HeaderPanelButtons active="browser" onToggle={onToggle} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Browser" }));
    expect(onToggle).toHaveBeenCalledWith(null);
  });

  it("is desktop-only — the phone reaches the panels through the ⋯ sheet", () => {
    render(<HeaderPanelButtons active={null} onToggle={vi.fn()} />);
    const group = screen.getByRole("group", { name: "Panels" });
    expect(group.className).toContain("hidden");
    expect(group.className).toContain("md:flex");
  });
});
