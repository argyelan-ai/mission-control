import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { RoundDivider } from "./RoundDivider";

/** Dieselbe Formatierung wie die Komponente — so bleibt der Test unabhängig
 *  von der Zeitzone der Maschine, prüft aber weiterhin echte Ausgabe. */
function expectedClock(iso: string): string {
  return new Date(iso).toLocaleTimeString("de-CH", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

describe("RoundDivider", () => {
  it("names the round it separates", () => {
    render(<RoundDivider round={3} />);
    expect(screen.getByText("Round 3")).toBeInTheDocument();
  });

  it("exposes the round name to screen readers on the separator itself", () => {
    render(<RoundDivider round={3} />);
    expect(screen.getByRole("separator", { name: "Round 3" })).toBeInTheDocument();
  });

  it("shows the round's time as 24h HH:MM", () => {
    const iso = "2026-08-20T14:05:00Z";
    render(<RoundDivider round={2} time={iso} />);
    const clock = expectedClock(iso);
    expect(clock).toMatch(/^\d{2}:\d{2}$/);
    expect(screen.getByText(clock)).toBeInTheDocument();
  });

  it("renders without a time when none is given", () => {
    render(<RoundDivider round={1} />);
    expect(screen.getByText("Round 1")).toBeInTheDocument();
    expect(screen.queryByText(/\d{2}:\d{2}/)).not.toBeInTheDocument();
  });

  it("drops an unparsable timestamp instead of showing 'Invalid Date'", () => {
    render(<RoundDivider round={4} time="nicht-ein-datum" />);
    expect(screen.getByText("Round 4")).toBeInTheDocument();
    expect(screen.queryByText(/Invalid/i)).not.toBeInTheDocument();
  });

  it("stays achromatic — the accent is reserved for the unread marker", () => {
    const { container } = render(<RoundDivider round={5} time="2026-08-20T09:00:00Z" />);
    const html = container.innerHTML.toUpperCase();
    expect(html).not.toContain("EBE8DE");
  });
});
