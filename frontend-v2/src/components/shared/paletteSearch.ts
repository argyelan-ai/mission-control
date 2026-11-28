/**
 * The search box's logic (concept "Neuer Chat und Suche", P5): one box,
 * groups Chats → In messages → Commands → Pages. Pure: no React, no fetching
 * — `CommandPalette.tsx` loads and renders.
 *
 * Ranking (concept 5.2, the Raycast/T3 pattern): an exact or word-start
 * title match beats a hit on the agent's name, which beats a fuzzy title
 * match; equal scores keep the chat list's order (newest first).
 */
import type { RecordChat, RecordSearchHit } from "@/lib/api";
import { chatEntryHref } from "@/lib/chatHistory";
import type { ChatListEntry } from "@/lib/chatList";

/** The server search starts at three characters (concept 5.2) … */
export const MESSAGE_SEARCH_MIN_CHARS = 3;
/** … and only once the typing paused this long. */
export const MESSAGE_SEARCH_DEBOUNCE_MS = 250;
/** Rows shown with an empty box: the recent chats, pinned first. */
export const RECENT_MAX = 8;
/** Rows per group while typing. */
export const GROUP_MAX = 8;

export const SCORE_EXACT = 4;
export const SCORE_WORD_START = 3;
/** The query hits the chat's agent name, not its title. */
export const SCORE_AGENT = 2;
export const SCORE_INSIDE = 1.5;
export const SCORE_FUZZY = 1;

/** Lower case, accents and umlauts folded ("Kühl" ⇄ "kuhl"). */
export function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}+/gu, "").toLocaleLowerCase();
}

function words(text: string): string[] {
  return fold(text).split(/[^\p{L}\p{N}]+/u).filter(Boolean);
}

function isSubsequence(needle: string, hay: string): boolean {
  let i = 0;
  for (const ch of hay) {
    if (ch === needle[i]) i += 1;
    if (i === needle.length) return true;
  }
  return needle.length === 0;
}

/**
 * How well `query` matches `text`: exact (4) · every word of the query starts
 * a word of the text (3) · every word sits inside the text, from two letters
 * on (1.5) · the
 * letters appear in order, from three letters on (1) · no match (null).
 */
export function matchScore(query: string, text: string): number | null {
  const q = fold(query).trim();
  if (!q || !text) return null;
  const t = fold(text).trim();
  if (t === q) return SCORE_EXACT;
  const terms = words(query);
  if (terms.length === 0) return null;
  const textWords = words(text);
  if (terms.every((term) => textWords.some((w) => w.startsWith(term)))) return SCORE_WORD_START;
  // one letter only ever starts a word — "s" must not list everything with an s
  if (q.length >= 2 && terms.every((term) => t.includes(term))) return SCORE_INSIDE;
  const compact = terms.join("");
  if (compact.length >= 3 && isSubsequence(compact, textWords.join(""))) return SCORE_FUZZY;
  return null;
}

/** The name a row shows — the chat list's own wording for a further chat. */
export type ChatTitleOf = (entry: ChatListEntry) => string;

/**
 * The chats that match, best first. `agentNameOf` names the agent behind a
 * further chat that carries its own title, so "Alpha" finds it too; a hit
 * on that name ranks below a word-start title hit and above a fuzzy one.
 */
export function rankChats(
  entries: readonly ChatListEntry[],
  query: string,
  titleOf: ChatTitleOf,
  agentNameOf: (entry: ChatListEntry) => string | null,
): ChatListEntry[] {
  const scored: { entry: ChatListEntry; score: number; index: number }[] = [];
  entries.forEach((entry, index) => {
    const title = matchScore(query, titleOf(entry));
    const agentName = agentNameOf(entry);
    const agent = agentName ? matchScore(query, agentName) : null;
    const viaAgent = agent !== null && agent >= SCORE_WORD_START ? SCORE_AGENT : null;
    const score = Math.max(title ?? -1, viaAgent ?? -1);
    if (score >= 0) scored.push({ entry, score, index });
  });
  return scored
    .sort((a, b) => b.score - a.score || recency(b.entry) - recency(a.entry) || a.index - b.index)
    .map((s) => s.entry);
}

function recency(e: ChatListEntry): number {
  const at = e.at ? Date.parse(e.at) : NaN;
  return Number.isNaN(at) ? 0 : at;
}

/** Items (commands, pages) that match, best first; names only, no fuzzy
 *  letter soup — "s" must not list every page with an s in its path. */
export function rankByName<T>(items: readonly T[], query: string, namesOf: (item: T) => string[]): T[] {
  const scored: { item: T; score: number; index: number }[] = [];
  items.forEach((item, index) => {
    let best = -1;
    for (const name of namesOf(item)) {
      const s = matchScore(query, name);
      if (s !== null && s >= SCORE_INSIDE && s > best) best = s;
    }
    if (best >= 0) scored.push({ item, score: best, index });
  });
  return scored.sort((a, b) => b.score - a.score || a.index - b.index).map((s) => s.item);
}

/** The key of the list row a record chat stands for: its own row, or the
 *  agent row for the agent's own (default) chat. */
export function rowKeyOfRecordChat(chat: Pick<RecordChat, "id" | "agentDefault" | "legacyAgentId">): string | null {
  if (chat.agentDefault) return chat.legacyAgentId ? `agent:${chat.legacyAgentId}` : null;
  return `chat:${chat.id}`;
}

/**
 * The empty box: recent chats, pinned first (the record's pin), then the
 * chat list's own order (waiting, today, earlier), at most `max`.
 */
export function recentChats(
  entries: readonly ChatListEntry[],
  recordChats: readonly Pick<RecordChat, "id" | "agentDefault" | "legacyAgentId" | "pinned">[],
  max: number = RECENT_MAX,
): ChatListEntry[] {
  const pinned = new Set(
    recordChats.filter((c) => c.pinned).map(rowKeyOfRecordChat).filter((k): k is string => !!k),
  );
  const first = entries.filter((e) => pinned.has(e.key));
  const rest = entries.filter((e) => !pinned.has(e.key));
  return [...first, ...rest].slice(0, max);
}

/**
 * Where a message hit leads: the row of its chat in the list (a further
 * chat → `&chat=`, the agent's own chat → the agent row). The chat view has
 * no message anchor yet, so the chat opens at its end. A hit of a chat the
 * list does not show (agent archived or not listed) leads nowhere → null.
 */
export function messageHitTarget(
  hit: Pick<RecordSearchHit, "chatId" | "agentId">,
  entries: readonly ChatListEntry[],
  recordChats: readonly Pick<RecordChat, "id" | "agentDefault" | "legacyAgentId">[],
): ChatListEntry | null {
  const byKey = new Map(entries.map((e) => [e.key, e] as const));
  const own = byKey.get(`chat:${hit.chatId}`);
  if (own) return own;
  const chat = recordChats.find((c) => c.id === hit.chatId);
  const agentId = chat?.legacyAgentId ?? hit.agentId;
  if (!agentId) return null;
  const agentRow = byKey.get(`agent:${agentId}`);
  if (!agentRow) return null;
  // A further chat the list has no row for yet (just made): open it by id.
  if (chat && !chat.agentDefault) {
    return { ...agentRow, key: `chat:${chat.id}`, chatId: chat.id, href: `${chatEntryHref("agent", agentId)}&chat=${encodeURIComponent(chat.id)}` };
  }
  return agentRow;
}

/** The snippet cut around the highlighted match (UTF-16 = JS indices); a
 *  range outside the text highlights nothing. */
export function splitHighlight(snippet: string, range: readonly number[] | null | undefined): {
  before: string;
  match: string;
  after: string;
} {
  const [a, b] = Array.isArray(range) ? range : [];
  if (typeof a !== "number" || typeof b !== "number" || a < 0 || b <= a || b > snippet.length) {
    return { before: snippet, match: "", after: "" };
  }
  return { before: snippet.slice(0, a), match: snippet.slice(a, b), after: snippet.slice(b) };
}

/** Ask the server? Only from three characters on (trimmed). */
export function wantsMessageSearch(query: string): boolean {
  return query.trim().length >= MESSAGE_SEARCH_MIN_CHARS;
}
