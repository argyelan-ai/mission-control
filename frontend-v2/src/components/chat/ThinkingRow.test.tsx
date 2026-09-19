import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThinkingRow } from "./ThinkingRow";
import type { ThinkingEvent } from "@/lib/chatTypes";

function mkEvent(overrides: Partial<ThinkingEvent> = {}): ThinkingEvent {
  return {
    kind: "thinking",
    uuid: "u1",
    ts: "2026-08-15T10:00:00Z",
    text: "Considering the tradeoffs between approach A and approach B in detail.",
    sidechain: false,
    ...overrides,
  };
}

describe("ThinkingRow", () => {
  it("is collapsed by default and hides the thinking text", () => {
    render(<ThinkingRow ev={mkEvent()} />);
    expect(screen.getByText("Denkt nach…")).toBeInTheDocument();
    expect(screen.queryByText(/tradeoffs/)).not.toBeInTheDocument();
  });

  it("expands to show the full text on click", async () => {
    const user = userEvent.setup();
    render(<ThinkingRow ev={mkEvent()} />);
    await user.click(screen.getByText("Denkt nach…"));
    expect(screen.getByText(/tradeoffs/)).toBeInTheDocument();
  });

  it("collapses again on a second click", async () => {
    const user = userEvent.setup();
    render(<ThinkingRow ev={mkEvent()} />);
    const label = screen.getByText("Denkt nach…");
    await user.click(label);
    expect(screen.getByText(/tradeoffs/)).toBeInTheDocument();
    await user.click(label);
    expect(screen.queryByText(/tradeoffs/)).not.toBeInTheDocument();
  });

  it("renders expanded by default when detailLevel is verbose", () => {
    render(<ThinkingRow ev={mkEvent()} detailLevel="verbose" />);
    expect(screen.getByText(/tradeoffs/)).toBeInTheDocument();
  });

  it("expands an already-mounted row when detailLevel changes Normal -> Ausführlich (I-3)", () => {
    const { rerender } = render(<ThinkingRow ev={mkEvent()} detailLevel="normal" />);
    expect(screen.queryByText(/tradeoffs/)).not.toBeInTheDocument();

    rerender(<ThinkingRow ev={mkEvent()} detailLevel="verbose" />);
    expect(screen.getByText(/tradeoffs/)).toBeInTheDocument();
  });

  it("collapses an already-mounted row when detailLevel changes Ausführlich -> Normal (I-3)", () => {
    const { rerender } = render(<ThinkingRow ev={mkEvent()} detailLevel="verbose" />);
    expect(screen.getByText(/tradeoffs/)).toBeInTheDocument();

    rerender(<ThinkingRow ev={mkEvent()} detailLevel="normal" />);
    expect(screen.queryByText(/tradeoffs/)).not.toBeInTheDocument();
  });

  it("still supports manual toggle after a detailLevel-driven sync", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ThinkingRow ev={mkEvent()} detailLevel="normal" />);
    rerender(<ThinkingRow ev={mkEvent()} detailLevel="verbose" />);
    expect(screen.getByText(/tradeoffs/)).toBeInTheDocument();

    await user.click(screen.getByText("Denkt nach…"));
    expect(screen.queryByText(/tradeoffs/)).not.toBeInTheDocument();
  });
});
