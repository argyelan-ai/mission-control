import { describe, it, expect } from "vitest";
import { groupTimelineEntries, humanizeEventType } from "../timelineGroups";
import type { TaskTimelineEntry } from "@/lib/types";

function reminder(ts: string): TaskTimelineEntry {
  return {
    ts,
    source: "activity_event",
    kind: "blocked",
    title: `Blocked-Reminder: 'Card' (agent) — ${ts.slice(11, 13)}min — Approval pending`,
    meta: { event_type: "task.blocked_reminder", severity: "info" },
  };
}

const comment: TaskTimelineEntry = {
  ts: "2026-09-20T10:30:00Z",
  source: "comment",
  kind: "progress",
  title: "progress",
  detail: "working on it",
};
const statusA: TaskTimelineEntry = {
  ts: "2026-09-20T09:00:00Z",
  source: "task_event",
  kind: "status_change",
  title: "in progress → blocked",
};

describe("groupTimelineEntries", () => {
  it("collapses a repeated system event into one group with count, first and last", () => {
    const entries = [
      statusA,
      reminder("2026-09-20T10:00:00Z"),
      comment,
      reminder("2026-09-20T11:00:00Z"),
      reminder("2026-09-20T12:00:00Z"),
    ];
    const items = groupTimelineEntries(entries);
    // status + comment stay single, the three reminders become one group
    expect(items).toHaveLength(3);
    const group = items.find((i) => i.type === "group");
    expect(group).toMatchObject({
      type: "group",
      eventType: "task.blocked_reminder",
      first: "2026-09-20T10:00:00Z",
      last: "2026-09-20T12:00:00Z",
    });
    expect(group && group.type === "group" && group.entries).toHaveLength(3);
  });

  it("places the group where its latest occurrence happened (chronological order kept)", () => {
    const items = groupTimelineEntries([
      statusA,
      reminder("2026-09-20T10:00:00Z"),
      comment,
      reminder("2026-09-20T11:00:00Z"),
    ]);
    expect(items.map((i) => (i.type === "group" ? "group" : i.entry.source))).toEqual([
      "task_event",
      "comment",
      "group",
    ]);
  });

  it("leaves a single occurrence alone", () => {
    const items = groupTimelineEntries([statusA, reminder("2026-09-20T10:00:00Z")]);
    expect(items.every((i) => i.type === "entry")).toBe(true);
  });

  it("never groups status changes, even when repeated", () => {
    const again = { ...statusA, ts: "2026-09-20T09:30:00Z" };
    const items = groupTimelineEntries([statusA, again]);
    expect(items.every((i) => i.type === "entry")).toBe(true);
  });
});

describe("humanizeEventType", () => {
  it("turns a machine key into a readable label", () => {
    expect(humanizeEventType("task.blocked_reminder")).toBe("Blocked reminder");
    expect(humanizeEventType("blocker.escalated_to_operator")).toBe("Escalated to operator");
    expect(humanizeEventType("review_stuck")).toBe("Review stuck");
  });
});
