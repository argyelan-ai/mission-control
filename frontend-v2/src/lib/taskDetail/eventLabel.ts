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
