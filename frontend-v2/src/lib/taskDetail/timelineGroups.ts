import type { TaskTimelineEntry } from "@/lib/types";

/**
 * Timeline grouping, frontend only. The watchdog writes one activity event per
 * reminder ("Blocked-Reminder … 3556min"), which floods the timeline. Events of
 * the same type that repeat collapse into one row ("Blocked reminder ×27 ·
 * first … · last …"), placed where the latest one happened. Status changes are
 * never grouped — each one is part of the story. The backend is untouched.
 */

export type TimelineItem =
  | { type: "entry"; entry: TaskTimelineEntry }
  | {
      type: "group";
      eventType: string;
      /** Chronological, oldest first. */
      entries: TaskTimelineEntry[];
      first: string;
      last: string;
    };

function groupKey(entry: TaskTimelineEntry): string | null {
  if (entry.source !== "activity_event" || entry.kind === "status_change") return null;
  const eventType = entry.meta?.event_type;
  return typeof eventType === "string" && eventType ? eventType : null;
}

/** Input and output are chronological (oldest first), like the API. */
export function groupTimelineEntries(entries: TaskTimelineEntry[]): TimelineItem[] {
  const buckets = new Map<string, TaskTimelineEntry[]>();
  for (const e of entries) {
    const key = groupKey(e);
    if (!key) continue;
    const list = buckets.get(key) ?? [];
    list.push(e);
    buckets.set(key, list);
  }

  const items: TimelineItem[] = [];
  const seen = new Map<string, number>();
  for (const e of entries) {
    const key = groupKey(e);
    const bucket = key ? buckets.get(key) : undefined;
    if (!key || !bucket || bucket.length < 2) {
      items.push({ type: "entry", entry: e });
      continue;
    }
    const n = (seen.get(key) ?? 0) + 1;
    seen.set(key, n);
    if (n < bucket.length) continue; // emit once, at the latest occurrence
    items.push({
      type: "group",
      eventType: key,
      entries: bucket,
      first: bucket[0].ts,
      last: bucket[bucket.length - 1].ts,
    });
  }
  return items;
}

/** "task.blocked_reminder" → "Blocked reminder" (last dotted segment, spaced). */
export function humanizeEventType(eventType: string): string {
  const tail = eventType.split(".").pop() ?? eventType;
  const words = tail.replace(/_/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : eventType;
}
