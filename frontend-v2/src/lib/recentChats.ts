/**
 * Recent chats — which chats this device opened last, newest first.
 *
 * Feeds the phone navigation (mobile nav V2): the Chats tab opens the LAST
 * chat directly, and the ⊕ sheet offers the last few as "continue a chat"
 * chips. Per device on purpose, like the sessions page's own "last agent"
 * memory (`mc-sessions-last-agent`), which this reads as a fallback so the
 * first visit after the update already knows the last chat.
 *
 * Every storage access is wrapped: private mode or a full quota must never
 * break navigation.
 */

export type ChatRef = { kind: "agent" | "group"; id: string };

export const RECENT_CHATS_KEY = "mc-recent-chats";
const LEGACY_LAST_AGENT_KEY = "mc-sessions-last-agent";
export const RECENT_CHATS_MAX = 5;
/** Fired on window after every write, so open tab bars update in place. */
export const RECENT_CHATS_EVENT = "mc-recent-chats";

function isChatRef(v: unknown): v is ChatRef {
  if (!v || typeof v !== "object") return false;
  const r = v as Record<string, unknown>;
  return (r.kind === "agent" || r.kind === "group") && typeof r.id === "string" && r.id.length > 0;
}

export function loadRecentChats(): ChatRef[] {
  try {
    const raw = localStorage.getItem(RECENT_CHATS_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    const list = Array.isArray(parsed) ? parsed.filter(isChatRef) : [];
    if (list.length > 0) return list.slice(0, RECENT_CHATS_MAX);
    const legacy = localStorage.getItem(LEGACY_LAST_AGENT_KEY);
    return legacy ? [{ kind: "agent", id: legacy }] : [];
  } catch {
    return [];
  }
}

/** Put `ref` first (no duplicates), keep at most RECENT_CHATS_MAX. */
export function pushRecent(list: ChatRef[], ref: ChatRef): ChatRef[] {
  return [ref, ...list.filter((r) => !(r.kind === ref.kind && r.id === ref.id))].slice(0, RECENT_CHATS_MAX);
}

export function rememberChat(ref: ChatRef): void {
  try {
    const next = pushRecent(loadRecentChats(), ref);
    localStorage.setItem(RECENT_CHATS_KEY, JSON.stringify(next));
    window.dispatchEvent(new Event(RECENT_CHATS_EVENT));
  } catch {
    /* storage unavailable — navigation still works, just without memory */
  }
}

/** Deep link that opens one chat directly (sessions page reads ?agent / ?group). */
export function chatHref(ref: ChatRef): string {
  return `/sessions?${ref.kind}=${encodeURIComponent(ref.id)}`;
}

/**
 * Where the Chats tab leads: from anywhere else straight into the last chat;
 * on the chats page itself back to the list ("tap Chats again → the list").
 */
export function chatsTabHref(pathname: string, recent: ChatRef[]): string {
  if (pathname.startsWith("/sessions")) return "/sessions";
  return recent[0] ? chatHref(recent[0]) : "/sessions";
}
