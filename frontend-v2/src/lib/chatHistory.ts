/**
 * Chat history on the phone — back from an open chat lands on the chats list.
 *
 * The phone's edge swipe is the browser's history back, so the list has to be
 * the history entry directly behind every open chat:
 *
 * - A link that opens a chat from anywhere else carries `enter=1`
 *   (`chatEntryHref`). The sessions page turns that one entry into two —
 *   the list, then the chat — and drops the marker on the way. Entries the
 *   browser comes back to later (back/forward, reload) never carry it, so
 *   they are never doubled.
 * - Opening a chat from the list pushes the chat's URL on top of the list.
 *
 * Desktop shows list and chat side by side; it keeps one entry per link.
 */

export type ChatKind = "agent" | "group" | "head";

/** Marker on links that open a chat from outside the chats list. */
export const CHAT_ENTRY_PARAM = "enter";

/** URL of one chat on the sessions page, as it sits in history. */
export function chatUrl(kind: ChatKind, id: string): string {
  return `/sessions?${kind}=${encodeURIComponent(id)}`;
}

/** Link that opens one chat from outside the chats list. */
export function chatEntryHref(kind: ChatKind, id: string): string {
  return `${chatUrl(kind, id)}&${CHAT_ENTRY_PARAM}=1`;
}

/** Below Tailwind's `md` the sessions page is a list/chat stack. */
export function isPhoneViewport(): boolean {
  try {
    return window.matchMedia("(max-width: 767px)").matches;
  } catch {
    return false;
  }
}

let directLoadTaken = false;

/**
 * True once per document, and only if this document was loaded straight at
 * the current URL (typed, bookmarked, opened from outside the app) — such a
 * chat link cannot carry the marker, yet nothing of the app is behind it.
 * A reload or a back/forward load is not a direct load: its history is
 * already in place.
 */
export function takeDirectLoad(): boolean {
  if (directLoadTaken) return false;
  directLoadTaken = true;
  try {
    const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
    return nav?.type === "navigate" && nav.name === window.location.href;
  } catch {
    return false;
  }
}
