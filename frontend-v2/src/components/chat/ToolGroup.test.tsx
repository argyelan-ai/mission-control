/**
 * ToolGroup vitest — the summary label (singular/plural, thinking-only runs,
 * error aggregation) and the collapse/expand behaviour incl. its reaction to
 * the detail level changing under a mounted group.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ToolGroup, summarizeActivity, type ActivityEvent } from "./ToolGroup";
import type { ThinkingEvent, ToolEvent } from "@/lib/chatTypes";

function tool(overrides: Partial<ToolEvent> = {}): ToolEvent {
  return {
    kind: "tool",
    uuid: `t-${Math.random()}`,
    ts: "2026-08-17T10:00:00Z",
    name: "Read",
    title: "Read foo.py",
    detail: { file_path: "/foo.py" },
    toolUseId: `tu-${Math.random()}`,
    result: null,
    status: "done",
    stats: null,
    sidechain: false,
    ...overrides,
  };
}

function thinking(overrides: Partial<ThinkingEvent> = {}): ThinkingEvent {
  return {
    kind: "thinking",
    uuid: `th-${Math.random()}`,
    ts: "2026-08-17T10:00:00Z",
    text: "Hmm…",
    sidechain: false,
    ...overrides,
  };
}

describe("summarizeActivity", () => {
  it("counts Bash as Befehle and everything else as Tools", () => {
    const s = summarizeActivity([
      tool({ name: "Bash", title: "npm test" }),
      tool({ name: "Bash", title: "git status" }),
      tool({ name: "Read" }),
    ]);
    expect(s.commands).toBe(2);
    expect(s.tools).toBe(1);
    expect(s.label).toBe("2 Befehle ausgeführt, 1 Tool verwendet");
  });

  it("uses the singular for a single command", () => {
    expect(summarizeActivity([tool({ name: "Bash" })]).label).toBe("1 Befehl ausgeführt");
  });

  it("labels a thinking-only run without a count when there is just one", () => {
    expect(summarizeActivity([thinking()]).label).toBe("Nachgedacht");
  });

  it("counts repeated thinking blocks", () => {
    expect(summarizeActivity([thinking(), thinking(), thinking()]).label).toBe("3× nachgedacht");
  });

  it("keeps the thinking segment lowercase when it follows another segment", () => {
    const s = summarizeActivity([tool({ name: "Read" }), thinking()]);
    expect(s.label).toBe("1 Tool verwendet, nachgedacht");
  });

  it("aggregates an error from any member of the run", () => {
    expect(summarizeActivity([tool(), tool({ status: "error" })]).hasError).toBe(true);
    expect(summarizeActivity([tool(), tool()]).hasError).toBe(false);
  });

  it("ignores thinking events when deciding whether the run failed", () => {
    expect(summarizeActivity([thinking(), thinking()]).hasError).toBe(false);
  });

  // Marks Screenshot 04.09.2026: „84 Tools verwendet, 2× nachgedacht" mit
  // rotem ⚠ — das las sich als „der ganze Lauf ist gescheitert". Tatsaechlich
  // war EIN mc-Aufruf mit 400 zurueckgekommen und wurde wiederholt. Die Zeile
  // muss sagen, wie viele fehlschlugen, sonst traegt das Icon eine Alarmstufe,
  // die die Zahl nicht hergibt.
  it("names how many tools failed in the visible line", () => {
    const s = summarizeActivity([tool(), tool({ status: "error" }), tool(), thinking()]);
    expect(s.failed).toBe(1);
    expect(s.label).toBe("3 Tools verwendet, 1 fehlgeschlagen, nachgedacht");
  });

  it("pluralises the failed count", () => {
    const s = summarizeActivity([tool({ status: "error" }), tool({ status: "error" })]);
    expect(s.label).toBe("2 Tools verwendet, 2 fehlgeschlagen");
  });

  it("counts a failed command as failed too", () => {
    const s = summarizeActivity([tool({ name: "Bash", status: "error" })]);
    expect(s.failed).toBe(1);
  });
});

describe("ToolGroup", () => {
  const RUN: ActivityEvent[] = [
    tool({ name: "Bash", title: "npm test" }),
    tool({ name: "Read", title: "Read foo.py" }),
  ];

  it("renders the summary chip and hides the rows until tapped", async () => {
    const user = userEvent.setup();
    render(<ToolGroup events={RUN} detailLevel="normal" />);

    const chip = screen.getByRole("button", { name: /1 Befehl ausgeführt, 1 Tool verwendet/ });
    expect(chip).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Read foo.py")).not.toBeInTheDocument();

    await user.click(chip);
    expect(chip).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Read foo.py")).toBeInTheDocument();
    expect(screen.getByText("npm test")).toBeInTheDocument();
  });

  it("shows the row count next to the label", () => {
    render(<ToolGroup events={RUN} detailLevel="normal" />);
    expect(screen.getByTestId("tool-group")).toHaveTextContent("2");
  });

  it("shows a warning icon when the run contains a failed tool", () => {
    render(<ToolGroup events={[tool(), tool({ status: "error" })]} detailLevel="normal" />);
    expect(screen.getByTestId("tool-group-error-icon")).toBeInTheDocument();
  });

  it("paints a partial failure as a warning, not as the run's failure", () => {
    // Ein fehlgeschlagenes Tool unter vielen ist eine Warnung (Bernstein) —
    // Rot ist dem Lauf vorbehalten, der wirklich gescheitert ist.
    render(<ToolGroup events={[tool(), tool({ status: "error" })]} detailLevel="normal" />);
    expect(screen.getByTestId("tool-group-error-icon").style.color).toBe("rgb(185, 143, 77)");
  });

  it("announces the failure to screen readers, not just in colour", () => {
    // The icon is aria-hidden and the colour is invisible to a screen reader,
    // so without this the group's failure was sighted-only information.
    render(<ToolGroup events={[tool(), tool({ status: "error" })]} detailLevel="normal" />);
    expect(screen.getByRole("button", { name: /Fehler enthalten/ })).toBeInTheDocument();
  });

  it("adds no failure wording to a run that succeeded", () => {
    render(<ToolGroup events={RUN} detailLevel="normal" />);
    expect(screen.queryByRole("button", { name: /Fehler enthalten/ })).not.toBeInTheDocument();
  });

  it("keeps the failure wording out of the visible line", () => {
    render(<ToolGroup events={[tool(), tool({ status: "error" })]} detailLevel="normal" />);
    expect(screen.getByText("— Fehler enthalten", { exact: false })).toHaveClass("sr-only");
  });

  it("shows the neutral icon when nothing failed", () => {
    render(<ToolGroup events={RUN} detailLevel="normal" />);
    expect(screen.getByTestId("tool-group-icon")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-group-error-icon")).not.toBeInTheDocument();
  });

  it("starts expanded at detailLevel 'verbose'", () => {
    render(<ToolGroup events={RUN} detailLevel="verbose" />);
    expect(screen.getByRole("button", { name: /Befehl/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Read foo.py")).toBeInTheDocument();
  });

  it("re-syncs an already-mounted group when the detail level changes", () => {
    const { rerender } = render(<ToolGroup events={RUN} detailLevel="normal" />);
    expect(screen.queryByTestId("tool-group-children")).not.toBeInTheDocument();

    rerender(<ToolGroup events={RUN} detailLevel="verbose" />);
    expect(screen.getByTestId("tool-group-children")).toBeInTheDocument();
  });

  it("renders nothing for an empty run", () => {
    const { container } = render(<ToolGroup events={[]} detailLevel="normal" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("passes the detail level down so child rows open with the group", () => {
    // "Ausführlich" must reach all the way through: the group opens AND
    // ToolRow's own detail block is already rendered — no second click.
    render(<ToolGroup events={RUN} detailLevel="verbose" />);
    // Both rows in the run open, hence getAllByText.
    expect(screen.getAllByText(/file_path/)).toHaveLength(RUN.length);
  });
});
