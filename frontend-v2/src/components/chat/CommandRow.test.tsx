/**
 * CommandRow vitest — the half that matters is the OUTPUT: `/context`,
 * `/status` and every skill command exist only for what they print, and the
 * timeline used to drop it.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CommandRow } from "./CommandRow";
import type { CommandEvent } from "@/lib/chatTypes";

function mkCommand(overrides: Partial<CommandEvent> = {}): CommandEvent {
  return {
    kind: "command",
    uuid: "c1",
    ts: "2026-08-17T10:00:00Z",
    command: "/context",
    ...overrides,
  };
}

describe("CommandRow", () => {
  it("stays a plain one-liner when no output was merged", () => {
    render(<CommandRow ev={mkCommand()} detailLevel="normal" />);
    expect(screen.getByText("/context")).toBeInTheDocument();
    // No chevron that would reveal nothing.
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("treats whitespace-only output as no output", () => {
    render(<CommandRow ev={mkCommand({ result: "   \n  " })} detailLevel="normal" />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("offers the output behind a chevron and reveals it on click", async () => {
    const user = userEvent.setup();
    render(<CommandRow ev={mkCommand({ result: "Context: 42% used" })} detailLevel="normal" />);

    const toggle = screen.getByRole("button", { name: /\/context/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("command-row-result")).not.toBeInTheDocument();

    await user.click(toggle);
    expect(screen.getByTestId("command-row-result")).toHaveTextContent("Context: 42% used");
  });

  it("opens the output at detailLevel 'verbose'", () => {
    render(<CommandRow ev={mkCommand({ result: "Context: 42% used" })} detailLevel="verbose" />);
    expect(screen.getByTestId("command-row-result")).toBeInTheDocument();
  });

  it("re-syncs an already-mounted row when the detail level changes", () => {
    const ev = mkCommand({ result: "Context: 42% used" });
    const { rerender } = render(<CommandRow ev={ev} detailLevel="normal" />);
    expect(screen.queryByTestId("command-row-result")).not.toBeInTheDocument();

    rerender(<CommandRow ev={ev} detailLevel="verbose" />);
    expect(screen.getByTestId("command-row-result")).toBeInTheDocument();
  });
});
