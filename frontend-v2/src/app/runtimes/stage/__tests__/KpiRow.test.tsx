/**
 * KpiRow — Zone 2 „Instrumente" (Spec §2). Review #438 Fund 4 (06.09.2026):
 * the right-edge divider must live entirely in the `.stage-kpi` stylesheet
 * rule (globals.css), never as an inline style on the cell — an inline style
 * always outranks a stylesheet rule, so a hardcoded `borderRight` here would
 * silently defeat BOTH the 2- and 4-column nth-child overrides no matter
 * what they say. This test is the sabotage check for that regression class.
 */
import { describe, it, expect } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { KpiRow, type KpiCell } from "../KpiRow";

const cells: KpiCell[] = [
  { value: "262k", label: "Context" },
  { value: "46,0", unit: "tok/s", label: "Speed solo" },
  { value: "3", label: "Agents" },
  { value: ":8000", label: "Endpoint · slot" },
];

describe("KpiRow", () => {
  it("renders 4 cells with the stage-kpi grid class", () => {
    render(<KpiRow cells={cells} />);
    const row = screen.getByTestId("kpi-row");
    expect(row).toHaveClass("stage-kpi");
    expect(row.children).toHaveLength(4);
  });

  it("never sets an inline border-right — the divider is CSS-only so the nth-child breakpoints can actually override it", () => {
    render(<KpiRow cells={cells} />);
    const row = screen.getByTestId("kpi-row");
    for (const cell of Array.from(row.children)) {
      expect((cell as HTMLElement).style.borderRight).toBe("");
    }
  });
});

describe("KpiRow — tappable cell (review fix round 5: a bare `title` tooltip never fires on a phone)", () => {
  function renderWithPopover() {
    const withPopover: KpiCell[] = [
      ...cells,
      {
        value: "1", unit: "working", label: "+ 1 connected", title: "who summary",
        testId: "kpi-in-use", popoverContent: <div>Working: Alpha</div>, popoverLabel: "Who is working",
      },
    ];
    render(<KpiRow cells={withPopover} />);
    return screen.getByTestId("kpi-in-use");
  }

  it("is closed by default, keeps the usual title/value/label, and carries the popover's own test id", () => {
    const tile = renderWithPopover();
    expect(tile.tagName).toBe("DETAILS");
    expect(tile).not.toHaveAttribute("open");
    expect(tile).toHaveAttribute("title", "who summary");
    expect(tile).toHaveTextContent("1");
    expect(tile).toHaveTextContent("working");
    expect(screen.getByTestId("kpi-in-use-popover")).toHaveTextContent("Working: Alpha");
    expect(screen.getByLabelText("Who is working")).toBeInTheDocument();
  });

  it("tapping the summary opens the native disclosure (and tapping again closes it)", () => {
    const tile = renderWithPopover();
    const summary = tile.querySelector("summary")!;
    fireEvent.click(summary);
    expect(tile).toHaveAttribute("open");
    fireEvent.click(summary);
    expect(tile).not.toHaveAttribute("open");
  });

  it("a plain cell (no popoverContent) still renders as a div, not a details element", () => {
    render(<KpiRow cells={cells} />);
    const row = screen.getByTestId("kpi-row");
    for (const cell of Array.from(row.children)) {
      expect(cell.tagName).toBe("DIV");
    }
  });
});
