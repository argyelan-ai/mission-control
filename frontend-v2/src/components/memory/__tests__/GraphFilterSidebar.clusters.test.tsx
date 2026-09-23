/**
 * Graph filter rail — no dead "Clusters" switch.
 *
 * The only caller wired it to `showClusters={false}` / `() => {}`: clicking
 * it did nothing and aria-pressed stayed false. The switch now renders only
 * when a caller actually handles it.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { GraphFilterSidebar } from "../GraphFilterSidebar";

beforeEach(() => {
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(),
  }));
});

const base = {
  filter: {},
  onFilterChange: vi.fn(),
  showHeatmap: false,
  onHeatmapToggle: vi.fn(),
  agents: [],
};

describe("GraphFilterSidebar overlays", () => {
  it("hides the Clusters switch when no handler is wired", () => {
    render(<GraphFilterSidebar {...base} />);
    expect(screen.getByText("Heatmap")).toBeInTheDocument();
    expect(screen.queryByText("Clusters")).toBeNull();
  });

  it("shows it when a caller handles the toggle", () => {
    render(<GraphFilterSidebar {...base} showClusters={false} onClustersToggle={vi.fn()} />);
    expect(screen.getByText("Clusters")).toBeInTheDocument();
  });
});
