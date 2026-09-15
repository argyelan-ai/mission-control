import { describe, expect, it } from "vitest";
import { deriveCheckpoints, checkpointLine } from "../taskCheckpoints";
import type { TaskTimelineEntry } from "../types";

const e = (over: Partial<TaskTimelineEntry>): TaskTimelineEntry => ({
  ts: "2026-09-11T06:00:00Z",
  source: "comment",
  kind: "message",
  title: "Message",
  detail: null,
  actor: "Lead",
  meta: null,
  ...over,
});

describe("deriveCheckpoints", () => {
  it("keeps milestones, status changes and progress-type comments; drops chatter", () => {
    const entries: TaskTimelineEntry[] = [
      e({ ts: "2026-09-09T05:00:00Z", source: "milestone", kind: "dispatched", title: "Dispatched to agent", actor: null }),
      e({ ts: "2026-09-09T05:01:00Z", source: "milestone", kind: "acked", title: "Acknowledged by agent" }),
      e({ ts: "2026-09-10T10:00:00Z", kind: "progress", detail: "**Update** — Part C is done." }),
      e({ ts: "2026-09-10T11:00:00Z", kind: "message", detail: "probe" }),
      e({ ts: "2026-09-10T12:00:00Z", kind: "feedback", detail: "looks fine" }),
      e({ ts: "2026-09-11T06:02:00Z", kind: "subtask_completed", detail: "**Subtask abgeschlossen:** PR #500 Rework\n**Agent:** Worker" }),
      e({ ts: "2026-09-11T06:30:00Z", source: "task_event", kind: "status_change", title: "In Progress → Waiting", actor: "Lead" }),
      e({ ts: "2026-09-11T07:00:00Z", kind: "blocker", detail: "Deadlock in nested tx" }),
    ];
    const cps = deriveCheckpoints(entries);
    expect(cps.map((c) => c.kind)).toEqual([
      "blocker",
      "status_change",
      "subtask_completed",
      "progress",
      "acked",
      "dispatched",
    ]); // newest first
  });

  it("caps the list and keeps the newest", () => {
    const entries = Array.from({ length: 20 }, (_, i) =>
      e({ ts: `2026-09-01T${String(i).padStart(2, "0")}:00:00Z`, kind: "progress", detail: `step ${i}` }),
    );
    const cps = deriveCheckpoints(entries, { limit: 6 });
    expect(cps).toHaveLength(6);
    expect(cps[0].text).toBe("step 19");
  });

  it("assigns a tone per kind", () => {
    const cps = deriveCheckpoints([
      e({ kind: "blocker", detail: "x" }),
      e({ kind: "progress", detail: "y" }),
      e({ kind: "waiting_on_callback", detail: "z" }),
      e({ source: "milestone", kind: "dispatched", title: "Dispatched to agent" }),
    ]);
    // same ts → stable order: blocker, progress, waiting_on_callback, dispatched
    expect(cps.map((c) => c.tone)).toEqual(["error", "accent", "warning", "accent"]);
  });
});

describe("checkpointLine", () => {
  it("uses the first meaningful line, stripped of markdown and protocol prefixes", () => {
    expect(checkpointLine("**Update** — Part C is done, part E is in review.\n\nMore…")).toBe(
      "Part C is done, part E is in review.",
    );
    expect(checkpointLine("**Subtask abgeschlossen:** PR #500 Rework: 5 Blocker\n**Agent:** Worker")).toBe(
      "Subtask abgeschlossen: PR #500 Rework: 5 Blocker",
    );
    expect(checkpointLine("## Befund\n`mc reject` ohne TASK_ID trifft die falsche Karte")).toBe(
      "mc reject ohne TASK_ID trifft die falsche Karte",
    );
  });

  it("falls back to the entry title when there is no detail", () => {
    expect(checkpointLine(null, "Dispatched to agent")).toBe("Dispatched to agent");
  });

  it("caps at 110 characters on a word boundary", () => {
    const out = checkpointLine(Array(40).fill("word").join(" "));
    expect(out.length).toBeLessThanOrEqual(111);
    expect(out.endsWith("…")).toBe(true);
  });
});
