/**
 * Round-2 Q5 (t4 S-neu-5): the operator's timeline showed the watchdog's
 * instructions to the agent word for word — "REVIEW-ENTSCHEIDUNG FEHLT … POST
 * /api/v1/agent/boards/…/review {"decision" …" — once per nudge. Those are
 * for the agent. The operator reads one line: a reminder went to the agent,
 * repeated ones collapse into one row.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { TaskTimeline } from "../TaskTimeline";
import type { TaskTimelineEntry } from "@/lib/types";

const RAW =
  'REVIEW-ENTSCHEIDUNG FEHLT: Du hast Task "X" kommentiert aber keine Review-Entscheidung getroffen.\n\n' +
  'POST /api/v1/agent/boards/b/tasks/t/review {"decision": "approve", "comment": "..."}';

function nudge(ts: string): TaskTimelineEntry {
  return { ts, source: "comment", kind: "watchdog_notify", title: "Watchdog notify", detail: RAW, actor: "System", meta: null };
}

function missing(ts: string): TaskTimelineEntry {
  return {
    ts, source: "activity_event", kind: "review", title: "Review-Entscheidung fehlt: 'X' (Beta) — Kommentar vor 30min",
    detail: null, actor: null, meta: { event_type: "task.review_decision_missing", severity: "warning", detail: null },
  };
}

describe("TaskTimeline — reminders meant for the agent", () => {
  it("one reminder reads as one plain line, without the agent's API instructions", () => {
    render(<TaskTimeline entries={[nudge("2026-10-22T19:45:00Z")]} isLoading={false} />);
    expect(screen.getByText("Reminder sent to the agent")).toBeInTheDocument();
    expect(screen.queryByText(/POST \/api\/v1/)).toBeNull();
    expect(screen.queryByText(/REVIEW-ENTSCHEIDUNG FEHLT/)).toBeNull();
  });

  it("repeated reminders and decision-missing events collapse into one row each", () => {
    const entries = [
      nudge("2026-10-22T19:45:00Z"), missing("2026-10-22T19:45:01Z"),
      nudge("2026-10-22T20:15:00Z"), missing("2026-10-22T20:15:01Z"),
      nudge("2026-10-22T20:45:00Z"), missing("2026-10-22T20:45:01Z"),
    ];
    render(<TaskTimeline entries={entries} isLoading={false} />);
    expect(screen.getAllByTestId("timeline-group")).toHaveLength(2);
    expect(screen.getByText("Reminder sent to the agent ×3")).toBeInTheDocument();
    expect(screen.getByText("Review decision missing ×3")).toBeInTheDocument();
    expect(screen.queryByText(/POST \/api\/v1/)).toBeNull();
  });
});
