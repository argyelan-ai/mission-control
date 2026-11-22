/**
 * Which activity events the operator sees on Home.
 *
 * Some events are pure plumbing — a dispatch that was silently retried, an
 * agent update with an outdated attempt id. The backend writes them as ready
 * sentences in one fixed language with internal words ("Silent retry:
 * dispatch_attempt_id rotiert …"). They help debugging, not the operator, so
 * the Home stream and its history panel leave them out. The full log keeps
 * them (Activity API, task detail).
 *
 * Follow-up (not here): typed events with catalog words instead of finished
 * backend sentences, so every event can be shown in the operator's language.
 */
import type { ActivityEvent } from "@/lib/types";

export const TECHNICAL_EVENT_TYPES: ReadonlySet<string> = new Set([
  "task.dispatch_attempt_rotated",
  "task.missing_dispatch_attempt_id",
  "task.stale_update_rejected",
  "task.stale_update_warning",
]);

/** Internal words that mark an event as plumbing even when its type is new. */
const TECHNICAL_WORDS = /\bdispatch_attempt_id\b|\bsilent retry\b/i;

export function isOperatorEvent(event: Pick<ActivityEvent, "event_type" | "title">): boolean {
  if (event.event_type && TECHNICAL_EVENT_TYPES.has(event.event_type)) return false;
  if (event.title && TECHNICAL_WORDS.test(event.title)) return false;
  return true;
}
