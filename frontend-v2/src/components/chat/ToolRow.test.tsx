import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ToolRow } from "./ToolRow";
import type { ToolEvent } from "@/lib/chatTypes";

function mkEvent(overrides: Partial<ToolEvent> = {}): ToolEvent {
  return {
    kind: "tool",
    uuid: "u1",
    ts: "2026-08-15T10:00:00Z",
    name: "Read",
    title: "Read backend/app/main.py",
    detail: { file_path: "backend/app/main.py" },
    toolUseId: "tool-1",
    result: "line 1\nline 2",
    status: "done",
    stats: null,
    sidechain: false,
    ...overrides,
  };
}

describe("ToolRow", () => {
  it("always shows the tool title", () => {
    render(<ToolRow ev={mkEvent()} detailLevel="normal" />);
    expect(screen.getByText("Read backend/app/main.py")).toBeInTheDocument();
  });

  it("does not show the result before expansion", () => {
    render(<ToolRow ev={mkEvent()} detailLevel="normal" />);
    expect(screen.queryByText(/line 1/)).not.toBeInTheDocument();
  });

  it("expands to show detail + result on click", async () => {
    const user = userEvent.setup();
    render(<ToolRow ev={mkEvent()} detailLevel="normal" />);
    await user.click(screen.getByText("Read backend/app/main.py"));
    expect(screen.getByText(/line 1/)).toBeInTheDocument();
    expect(screen.getAllByText(/backend\/app\/main\.py/).length).toBeGreaterThan(1);
  });

  it("renders expanded by default when detailLevel is verbose", () => {
    render(<ToolRow ev={mkEvent()} detailLevel="verbose" />);
    expect(screen.getByText(/line 1/)).toBeInTheDocument();
  });

  it("shows an error indicator when status is error", () => {
    render(<ToolRow ev={mkEvent({ status: "error", result: "boom" })} detailLevel="normal" />);
    expect(screen.getByTestId("tool-row-error-dot")).toBeInTheDocument();
  });

  it("shows a stats chip with additions/deletions", () => {
    render(<ToolRow ev={mkEvent({ stats: { additions: 3, deletions: 1 } })} detailLevel="normal" />);
    expect(screen.getByText("+3")).toBeInTheDocument();
    expect(screen.getByText(/-1|−1/)).toBeInTheDocument();
  });

  it("toggles closed again on a second click", async () => {
    const user = userEvent.setup();
    render(<ToolRow ev={mkEvent()} detailLevel="normal" />);
    const title = screen.getByText("Read backend/app/main.py");
    await user.click(title);
    expect(screen.getByText(/line 1/)).toBeInTheDocument();
    await user.click(title);
    expect(screen.queryByText(/line 1/)).not.toBeInTheDocument();
  });
});
