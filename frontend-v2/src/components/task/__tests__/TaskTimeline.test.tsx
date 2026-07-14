/**
 * TaskTimeline — the "Task Flight Recorder" tab. Renders the merged,
 * chronological entries returned by GET .../tasks/{id}/timeline.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { TaskTimeline } from "../TaskTimeline";
import type { TaskTimelineEntry } from "@/lib/types";

function mkEntry(overrides: Partial<TaskTimelineEntry> = {}): TaskTimelineEntry {
  return {
    ts: "2026-07-01T12:00:00Z",
    source: "milestone",
    kind: "created",
    title: "Task created",
    detail: null,
    actor: null,
    meta: null,
    ...overrides,
  };
}

describe("TaskTimeline", () => {
  it("shows a loading state", () => {
    render(<TaskTimeline entries={[]} isLoading={true} />);
    expect(screen.getByText("Lade Timeline…")).toBeInTheDocument();
  });

  it("shows an empty state when there are no entries", () => {
    render(<TaskTimeline entries={[]} isLoading={false} />);
    expect(screen.getByText("Noch keine Ereignisse.")).toBeInTheDocument();
  });

  it("renders entries newest-first with title, detail and actor", () => {
    const entries = [
      mkEntry({ ts: "2026-07-01T12:00:00Z", kind: "created", title: "Task created" }),
      mkEntry({
        ts: "2026-07-01T12:05:00Z",
        source: "task_event",
        kind: "status_change",
        title: "inbox → in progress",
        actor: "Cody",
      }),
      mkEntry({
        ts: "2026-07-01T12:10:00Z",
        source: "comment",
        kind: "progress",
        title: "Progress",
        detail: "Wrote the endpoint, running tests now.",
        actor: "Cody",
      }),
    ];

    render(<TaskTimeline entries={entries} isLoading={false} />);

    const titles = screen.getAllByText(/Task created|inbox → in progress|Progress/);
    // Newest (progress comment) rendered before the oldest (created milestone).
    expect(titles[0]).toHaveTextContent("Progress");
    expect(titles[titles.length - 1]).toHaveTextContent("Task created");

    expect(screen.getByText("Wrote the endpoint, running tests now.")).toBeInTheDocument();
    expect(screen.getAllByText("Cody").length).toBe(2);
  });

  it("shows a cap notice when the response was truncated", () => {
    render(
      <TaskTimeline
        entries={[mkEntry()]}
        isLoading={false}
        truncated={true}
      />
    );
    expect(screen.getByText(/ältere ausgeblendet/)).toBeInTheDocument();
  });
});
