import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HelperLine } from "./HelperLine";
import { helperReports } from "./helpers";
import type { ChatEvent, NotificationEvent, ToolEvent } from "@/lib/chatTypes";

vi.mock("@/lib/api", () => ({ api: { chat: { subagentHistory: vi.fn() } } }));

function spawn(id: string, description: string): ToolEvent {
  return {
    kind: "tool", uuid: `u-${id}`, ts: "2026-11-13T10:00:00Z", name: "Agent", title: `Agent: ${description}`,
    detail: { description }, toolUseId: id, result: "Async agent launched successfully.", status: "done",
    stats: null, sidechain: false,
    helpers: [{ id: `ag-${id}`, name: null, agent: null, state: "working", durationMs: null }],
  };
}

function done(id: string, durationMs: number): NotificationEvent {
  return {
    kind: "notification", uuid: `n-${id}`, ts: "2026-11-13T10:05:00Z", taskId: `ag-${id}`,
    toolUseId: id, status: "completed", summary: null, durationMs,
  };
}

function wrap(events: ToolEvent[], all: ChatEvent[]) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <HelperLine events={events} reports={helperReports(all)} runs={new Map()} agentId="a1" />
    </QueryClientProvider>,
  );
}

const SPAWNS = [spawn("t1", "Read the docs"), spawn("t2", "Check the tests"), spawn("t3", "Scan the repo"), spawn("t4", "Draft")];

describe("HelperLine", () => {
  it("says how many helpers there are and how many still work — launched is not done", () => {
    wrap(SPAWNS, [...SPAWNS, done("t1", 125_000)]);
    expect(screen.getByTestId("helper-line-label").textContent).toBe("4 helpers · 3 working");
  });

  it("is one line: the helpers stay folded until tapped", () => {
    wrap(SPAWNS, SPAWNS);
    expect(screen.queryByTestId("helper-list")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /show helpers/i }));
    expect(screen.getAllByTestId("helper-row")).toHaveLength(4);
  });

  it("each opened row names its helper, its state and its measured duration", () => {
    wrap(SPAWNS, [...SPAWNS, done("t1", 125_000)]);
    fireEvent.click(screen.getByRole("button", { name: /show helpers/i }));
    const [first, second] = screen.getAllByTestId("helper-row");
    expect(first.textContent).toContain("Read the docs");
    expect(first.querySelector('[data-testid="helper-state"]')?.textContent).toBe("done");
    expect(first.querySelector('[data-testid="helper-duration"]')?.textContent).toBe("2 min");
    expect(second.querySelector('[data-testid="helper-state"]')?.textContent).toBe("working");
  });

  it("once every helper reported it says done, with the group's whole duration", () => {
    const all = [...SPAWNS, ...SPAWNS.map((s, i) => done(s.toolUseId!, (i + 1) * 60_000))];
    wrap(SPAWNS, all);
    expect(screen.getByTestId("helper-line-label").textContent).toBe("4 helpers · done");
    expect(screen.getByTestId("helper-line-duration").textContent).toBe("4 min");
  });

  it("a failure is named, not only coloured", () => {
    const failed: NotificationEvent = { ...done("t2", 1000), status: "failed" };
    const all = [...SPAWNS, ...["t1", "t3", "t4"].map((id) => done(id, 1000)), failed];
    wrap(SPAWNS, all);
    expect(screen.getByTestId("helper-line-label").textContent).toBe("4 helpers · 3 done, 1 failed");
  });

  it("one helper reads in the singular", () => {
    wrap([SPAWNS[0]], [SPAWNS[0]]);
    expect(screen.getByTestId("helper-line-label").textContent).toBe("1 helper · 1 working");
  });
});
