/**
 * ToolRow changes for spec A (PR-3, test 14): 44 px rows everywhere (UI-8),
 * the failed-step tone as one token (K20), the i18n aria label (F7), and the
 * props a task section passes — a lazily loaded body, no live output, no
 * auto-open, muted diff numbers.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ToolRow } from "./ToolRow";
import { ToolOutputContext, createToolOutputStore } from "./toolOutputStore";
import { C, STATUS_TEXT, STEP_FAILED_TONE, stepFailedStyle } from "@/lib/colors";
import type { ToolEvent } from "@/lib/chatTypes";

function ev(over: Partial<ToolEvent> = {}): ToolEvent {
  return {
    kind: "tool", uuid: "u1", ts: "2026-10-21T23:00:00Z", name: "edit", title: "Edit index.html",
    detail: {}, toolUseId: "tu-1", result: "", status: "done", stats: null, sidechain: false, ...over,
  };
}

describe("ToolRow — spec A", () => {
  it("the whole row is a 44 px finger target (UI-8, every chat)", () => {
    render(<ToolRow ev={ev()} detailLevel="normal" />);
    const button = screen.getByRole("button");
    expect(button.className).toContain("min-h-11");
    // The wrapper no longer adds vertical padding around the button.
    expect(screen.getByTestId("tool-row").className).not.toMatch(/\bpy-/);
  });

  it("a failed step uses the STEP_FAILED_TONE token and an i18n label", () => {
    render(<ToolRow ev={ev({ status: "error", result: "boom" })} detailLevel="normal" />);
    const dot = screen.getByTestId("tool-row-error-dot");
    expect(dot).toHaveAttribute("aria-label", "failed");
    const style = stepFailedStyle(STEP_FAILED_TONE);
    expect(dot.style.background).toBe(style.dot);
    expect(screen.getByText("Edit index.html").style.color).toBe(style.title);
  });

  it("the recommended tone is ocker with a neutral title; the red look is one switch away", () => {
    expect(STEP_FAILED_TONE).toBe("warning");
    expect(stepFailedStyle("warning")).toMatchObject({ dot: C.warning, title: C.textSecondary });
    expect(stepFailedStyle("error")).toMatchObject({ dot: STATUS_TEXT.error, title: STATUS_TEXT.error });
  });

  it("lazyDetail: loads on open, shows a loading line meanwhile and never an empty {}", async () => {
    const load = vi.fn();
    const { rerender } = render(
      <ToolRow ev={ev()} detailLevel="normal" liveOutput={false} lazyDetail={{ event: null, loading: false, load }} />,
    );
    await userEvent.click(screen.getByRole("button"));
    expect(load).toHaveBeenCalledTimes(1);
    rerender(<ToolRow ev={ev()} detailLevel="normal" liveOutput={false} lazyDetail={{ event: null, loading: true, load }} />);
    expect(screen.getByTestId("tool-row-loading")).toBeInTheDocument();
    expect(screen.queryByText("{}")).not.toBeInTheDocument();
    const full = ev({ detail: { command: "sed -i s/a/b/ index.html" }, result: "done here" });
    rerender(<ToolRow ev={ev()} detailLevel="normal" liveOutput={false} lazyDetail={{ event: full, loading: false, load }} />);
    expect(screen.getByText(/done here/)).toBeInTheDocument();
    expect(screen.getByText(/sed -i/)).toBeInTheDocument();
  });

  it("liveOutput={false}: neither live output nor Ausführlich opens the row by itself", () => {
    const store = createToolOutputStore();
    store.set({ "tu-1": { kind: "tool_output", uuid: null, ts: "x", toolUseId: "tu-1", text: "streaming…" } });
    render(
      <ToolOutputContext.Provider value={store}>
        <ToolRow ev={ev({ result: null })} detailLevel="verbose" liveOutput={false} lazyDetail={{ event: null, loading: false, load: vi.fn() }} />
      </ToolOutputContext.Provider>,
    );
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("tool-live-output")).not.toBeInTheDocument();
  });

  it("statsTone=muted draws the diff numbers without status colour", () => {
    render(<ToolRow ev={ev({ stats: { additions: 3, deletions: 1 } })} detailLevel="normal" statsTone="muted" />);
    expect(screen.getByText("+3").style.color).toBe(C.textMuted);
  });

  it("runState running: a neutral pulse and 'running · for {span}' after a minute", () => {
    const since = new Date(Date.now() - 3 * 60_000).toISOString();
    render(<ToolRow ev={ev({ result: null })} detailLevel="normal" liveOutput={false} runState={{ kind: "running", since }} runningTone="neutral" />);
    expect(screen.getByText("running · for 3 min")).toBeInTheDocument();
  });

  it("runState noResult: a quiet 'no result'", () => {
    render(<ToolRow ev={ev({ result: null })} detailLevel="normal" liveOutput={false} runState={{ kind: "noResult" }} />);
    expect(screen.getByText("no result")).toBeInTheDocument();
  });
});
