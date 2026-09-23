/**
 * TaskTimeline — the "Task Flight Recorder" tab. Renders the merged,
 * chronological entries returned by GET .../tasks/{id}/timeline.
 */
import { describe, it, expect } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
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
    expect(screen.getByText("Loading timeline…")).toBeInTheDocument();
  });

  it("shows an empty state when there are no entries", () => {
    render(<TaskTimeline entries={[]} isLoading={false} />);
    expect(screen.getByText("No events yet.")).toBeInTheDocument();
  });

  it("renders entries newest-first with title, detail and actor", () => {
    const entries = [
      mkEntry({ ts: "2026-07-01T12:00:00Z", kind: "created", title: "Task created" }),
      mkEntry({
        ts: "2026-07-01T12:05:00Z",
        source: "task_event",
        kind: "status_change",
        title: "inbox → in progress",
        actor: "alpha",
      }),
      mkEntry({
        ts: "2026-07-01T12:10:00Z",
        source: "comment",
        kind: "progress",
        title: "Progress",
        detail: "Wrote the endpoint, running tests now.",
        actor: "alpha",
      }),
    ];

    render(<TaskTimeline entries={entries} isLoading={false} />);

    const titles = screen.getAllByText(/Task created|inbox → in progress|Progress/);
    // Newest (progress comment) rendered before the oldest (created milestone).
    expect(titles[0]).toHaveTextContent("Progress");
    expect(titles[titles.length - 1]).toHaveTextContent("Task created");

    expect(screen.getByText("Wrote the endpoint, running tests now.")).toBeInTheDocument();
    expect(screen.getAllByText("alpha").length).toBe(2);
  });

  it("shows a cap notice when the response was truncated", () => {
    render(
      <TaskTimeline
        entries={[mkEntry()]}
        isLoading={false}
        truncated={true}
      />
    );
    expect(screen.getByText(/older ones are hidden/)).toBeInTheDocument();
  });

  it("groups repeated reminders into one row: 'Blocked reminder ×3' with first/last", () => {
    const reminder = (ts: string) =>
      mkEntry({
        ts,
        source: "activity_event",
        kind: "blocked",
        title: `Blocked-Reminder: 'Card' (alpha) — ${ts.slice(14, 16)}min — Approval pending`,
        meta: { event_type: "task.blocked_reminder", severity: "info" },
      });
    const entries = [
      mkEntry({ ts: "2026-07-01T12:00:00Z" }),
      reminder("2026-07-01T12:10:00Z"),
      reminder("2026-07-01T12:20:00Z"),
      reminder("2026-07-01T12:30:00Z"),
    ];
    render(<TaskTimeline entries={entries} isLoading={false} />);

    expect(screen.getByText("Blocked reminder ×3")).toBeInTheDocument();
    expect(screen.getAllByTestId("timeline-group")).toHaveLength(1);
    // Individual reminder rows are hidden until asked for.
    expect(screen.queryByText(/Blocked-Reminder:/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show each" }));
    expect(screen.getAllByText(/Blocked-Reminder:/)).toHaveLength(3);
  });

  it("has no inner scroll box (the panel scrolls, not the list)", () => {
    const { container } = render(<TaskTimeline entries={[mkEntry()]} isLoading={false} />);
    const scrollers = Array.from(container.querySelectorAll<HTMLElement>("div")).filter((d) => d.style.maxHeight);
    expect(scrollers).toHaveLength(0);
  });
});
