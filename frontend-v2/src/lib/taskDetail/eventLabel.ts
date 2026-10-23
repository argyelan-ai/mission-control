import { humanizeEventType } from "./timelineGroups";

/**
 * UI label for a machine event / approval type ("task.blocked_reminder",
 * "blocker.escalated_to_operator", "visual_review"). Labels are UI text, so
 * they come from i18n (`tasks.detail.event.<slug>`); an unknown type falls
 * back to the humanized key so a new backend type never renders blank.
 */

type Translator = ((key: string) => string) & { has: (key: string) => boolean };

/** "task.blocked_reminder" → "blocked_reminder", "blocker.escalated_to_operator" → "blocker_escalated_to_operator". */
export function eventSlug(eventType: string): string {
  return eventType.replace(/^task\./, "").replace(/\./g, "_");
}

export function eventLabel(t: Translator, eventType: string): string {
  const key = `detail.event.${eventSlug(eventType)}`;
  return t.has(key) ? t(key) : humanizeEventType(eventType);
}

/**
 * The reason of a status step in words (round-7 re-check t1 NICE-2: the
 * timeline read "review_rejection_dispatch_blocked"). A reason code comes
 * from i18n (`tasks.detail.statusReason.<code>`); an unknown code is
 * humanized, never shown raw; free text (an operator's reason) stays.
 */
export function statusReasonText(t: Translator, reason: string): string {
  if (!/^[a-z][a-z0-9]*(_[a-z0-9]+)+$/.test(reason)) return reason;
  const key = `detail.statusReason.${reason}`;
  return t.has(key) ? t(key) : reason.replace(/_/g, " ");
}
