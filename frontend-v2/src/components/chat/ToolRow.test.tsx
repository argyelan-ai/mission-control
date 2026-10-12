import { describe, it, expect } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ToolRow } from "./ToolRow";
import type { ToolEvent, ToolOutputEvent } from "@/lib/chatTypes";
import { ToolOutputContext, createToolOutputStore } from "./toolOutputStore";

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

  it("expands an already-mounted row when detailLevel changes Normal -> Ausführlich (I-3)", () => {
    const { rerender } = render(<ToolRow ev={mkEvent()} detailLevel="normal" />);
    expect(screen.queryByText(/line 1/)).not.toBeInTheDocument();

    rerender(<ToolRow ev={mkEvent()} detailLevel="verbose" />);
    expect(screen.getByText(/line 1/)).toBeInTheDocument();
  });

  it("collapses an already-mounted row when detailLevel changes Ausführlich -> Normal (I-3)", () => {
    const { rerender } = render(<ToolRow ev={mkEvent()} detailLevel="verbose" />);
    expect(screen.getByText(/line 1/)).toBeInTheDocument();

    rerender(<ToolRow ev={mkEvent()} detailLevel="normal" />);
    expect(screen.queryByText(/line 1/)).not.toBeInTheDocument();
  });

  it("still supports manual toggle after a detailLevel-driven sync", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ToolRow ev={mkEvent()} detailLevel="normal" />);
    rerender(<ToolRow ev={mkEvent()} detailLevel="verbose" />);
    expect(screen.getByText(/line 1/)).toBeInTheDocument();

    await user.click(screen.getByText("Read backend/app/main.py"));
    expect(screen.queryByText(/line 1/)).not.toBeInTheDocument();
  });
});

describe("ToolRow — chat package P1 (live output, diff, locations)", () => {
  async function withStore(ui: React.ReactElement, outputs: Record<string, ToolOutputEvent>) {
    const store = createToolOutputStore();
    store.set(outputs);
    const view = render(<ToolOutputContext.Provider value={store}>{ui}</ToolOutputContext.Provider>);
    return { store, ...view };
  }
  const live = (text: string): ToolOutputEvent => ({
    kind: "tool_output", uuid: null, ts: "2026-10-12T16:18:08Z", toolUseId: "call_bash_1", text,
  });

  it("a RUNNING tool shows its live output without a tap", async () => {
    await withStore(
      <ToolRow ev={mkEvent({ name: "bash", title: "$ make", toolUseId: "call_bash_1", result: null })} detailLevel="normal" />,
      { call_bash_1: live("step 1\nstep 2") },
    );
    expect(screen.getByTestId("tool-live-output")).toHaveTextContent("step 1 step 2");
    expect(screen.getByText("running")).toBeInTheDocument();
  });

  it("the result replaces the live output", async () => {
    await withStore(
      <ToolRow ev={mkEvent({ toolUseId: "call_bash_1", result: "done" })} detailLevel="normal" />,
      { call_bash_1: live("step 1") },
    );
    expect(screen.queryByTestId("tool-live-output")).toBeNull();
    expect(screen.queryByText("running")).toBeNull();
  });

  it("a new snapshot re-renders the running row in place", async () => {
    const { store } = await withStore(
      <ToolRow ev={mkEvent({ toolUseId: "call_bash_1", result: null })} detailLevel="normal" />,
      { call_bash_1: live("step 1") },
    );
    act(() => store.set({ call_bash_1: live("step 1\nstep 2\nstep 3") }));
    expect(screen.getByTestId("tool-live-output")).toHaveTextContent("step 3");
  });

  it("an edit shows its diff and file chip instead of the raw strings", async () => {
    const user = userEvent.setup();
    render(
      <ToolRow
        ev={mkEvent({
          name: "edit",
          title: "edit: /workspace/demo.py",
          detail: { path: "/workspace/demo.py", old_string: "OLD-RAW", new_string: "NEW-RAW" },
          result: "[/workspace/demo.py]\n1:def value():",
          stats: { additions: 2, deletions: 1 },
          locations: ["/workspace/demo.py"],
          diffs: [{
            path: "/workspace/demo.py", kind: "edit", additions: 2, deletions: 1, truncated: false,
            lines: [{ t: "ctx", text: "def value():" }, { t: "del", text: "    return 1" },
                    { t: "add", text: "    return 2" }],
          }],
        })}
        detailLevel="normal"
      />,
    );
    await user.click(screen.getByText("edit: /workspace/demo.py"));
    expect(screen.getByTestId("tool-diff")).toBeInTheDocument();
    expect(screen.getByText("return 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy path /workspace/demo.py" })).toBeInTheDocument();
    expect(screen.queryByText(/OLD-RAW/)).toBeNull();
    expect(screen.queryByText(/1:def value/)).toBeNull();
  });

  it("a tool without diffs keeps the old expanded view (no regression)", async () => {
    const user = userEvent.setup();
    render(<ToolRow ev={mkEvent()} detailLevel="normal" />);
    await user.click(screen.getByText("Read backend/app/main.py"));
    expect(screen.queryByTestId("tool-diff")).toBeNull();
    expect(screen.getByText(/line 1/)).toBeInTheDocument();
  });
});
