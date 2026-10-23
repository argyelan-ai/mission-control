/**
 * Round-4 review r2 N1: on the phone a half grey circle stuck to the left
 * edge of the graph card. It was the rail's open button: with the rail
 * collapsed (width 0) it hung at `-right-3`, i.e. mostly outside the card,
 * clipped, its chevron invisible. Collapsed, it now sits fully inside.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { GraphFilterSidebar } from "../GraphFilterSidebar";

function stubPhone(phone: boolean) {
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: phone && q.includes("max-width"), media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(),
  }));
}

const base = { filter: {}, onFilterChange: vi.fn(), showHeatmap: false, onHeatmapToggle: vi.fn(), agents: [] };

describe("GraphFilterSidebar — the open/close button stays on the card", () => {
  it("collapsed (phone default): the open button sits inside the left edge", () => {
    stubPhone(true);
    render(<GraphFilterSidebar {...base} />);
    const btn = screen.getByRole("button", { name: "Expand filter sidebar" });
    expect(btn.className).toMatch(/(^|\s)left-2(\s|$)/);
    expect(btn.className).not.toMatch(/-right-3/);
  });

  it("open (desktop default): the close button rides the rail's edge", () => {
    stubPhone(false);
    render(<GraphFilterSidebar {...base} />);
    const btn = screen.getByRole("button", { name: "Collapse filter sidebar" });
    expect(btn.className).toMatch(/-right-3/);
  });
});
