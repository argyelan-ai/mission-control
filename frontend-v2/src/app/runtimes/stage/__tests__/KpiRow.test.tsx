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
    // Review fix round 6, finding 6: an `aria-label` on the <summary> used
    // to REPLACE its accessible name entirely, so a screen reader announced
    // only "Who is working" and never the value ("1 working · + 1
    // connected") — the one thing a screen reader user actually needs here.
    // The hint rides along as part of the name now (a trailing `sr-only`
    // span), never overriding it.
    const summary = tile.querySelector("summary")!;
    expect(summary).not.toHaveAttribute("aria-label");
    expect(summary).toHaveTextContent("1");
    expect(summary).toHaveTextContent("working");
    expect(summary).toHaveTextContent("Who is working");
  });

  it("gives sighted phone users a visible open/close signal: a chevron that flips when the disclosure opens", () => {
    const tile = renderWithPopover();
    const chevron = tile.querySelector("summary svg");
    expect(chevron).toBeTruthy();
    expect(chevron).not.toHaveClass("rotate-180");
    expect(chevron).toHaveClass("group-open:rotate-180");
  });

  // Review fix round 6, finding 8: same regression class as the Inbox head
  // chip — no screenshot script set `hasTouch`, so a removed
  // `pointer-coarse:min-h-[44px]` would show up in no screenshot and pass
  // every existing test here too.
  it("carries the 44px coarse-pointer touch target (DESIGN.md K11)", () => {
    const tile = renderWithPopover();
    expect(tile.querySelector("summary")).toHaveClass("pointer-coarse:min-h-[44px]");
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
