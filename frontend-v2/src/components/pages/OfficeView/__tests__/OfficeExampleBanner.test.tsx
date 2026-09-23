/**
 * /office shows an EXAMPLE crew (static data, see org-chart-data.ts). It must
 * not read like the operator's live fleet: a banner says so, and the status
 * dots are neutral grey without the "working" pulse.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { OrgChartNode } from "../OrgChart/OrgChartNode";
import { ORG_CHART } from "../OrgChart/org-chart-data";
import { C } from "@/lib/colors";

vi.mock("@/components/layout/AppShell", () => ({
  __esModule: true,
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("../OrgChart", () => ({ OrgChart: () => <div data-testid="org-chart" /> }));

import OfficeView from "../index";

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});

describe("Office example crew", () => {
  it("labels the chart as an example, not the live fleet", () => {
    render(<OfficeView />);
    expect(screen.getByText("Example crew — not your live fleet")).toBeInTheDocument();
  });

  it("draws every status dot in neutral grey", () => {
    const worker = ORG_CHART.nodes.find((n) => n.status === "working" || n.status === "online");
    expect(worker).toBeDefined();
    const { container } = render(<OrgChartNode node={worker!} />);
    const dots = [...container.querySelectorAll<HTMLElement>("span.rounded-full")];
    expect(dots.length).toBeGreaterThan(0);
    // Normalise the palette the same way jsdom normalises inline styles.
    const norm = (c: string) => {
      const el = document.createElement("span");
      el.style.background = c;
      return el.style.background;
    };
    const live = [C.online, C.accent, C.warning, C.error].map(norm);
    for (const dot of dots) {
      expect(dot.style.background).not.toBe("");
      expect(live).not.toContain(dot.style.background);
    }
  });
});
