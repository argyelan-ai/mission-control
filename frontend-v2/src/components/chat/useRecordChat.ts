"use client";

/**
 * A further chat of an agent (chats slice S2, ADR-103/ADR-105), read from
 * MC's chat record and shaped like `useChatStream`'s result, so `ChatView`
 * renders it with the same rows as the agent's own chat.
 *
 * - The URL names it: `/sessions?agent=<id>&chat=<record chat id>`.
 * - Its events come from `GET /chat-record/chats/{id}/history` (the record
 *   holds what the chat daemon wrote for that chat). There is no SSE for a
 *   further chat, so the page is read again on a short interval while the
 *   chat is open — faster right after a send.
 * - Sending goes to the agent's existing input route with `chatId`.
 * - The record answers 404 `chat_record_disabled` while `CHAT_RECORD_ENABLED`
 *   is off: then `disabled` is true and the caller shows the agent's own chat
 *   exactly as before. A 404 for a chat that is not there (archived, deleted,
 *   not visible) is `missing` instead — the caller drops `&chat=` so the URL
 *   never names a chat the screen does not show.
 * - No live state frame exists for a further chat, so "Sent" ends on the
 *   first sign the agent took the turn (any non-user row after the send),
 *   with a hard ceiling; echoes age to "unconfirmed" like in the agent's
 *   own chat (`markUnconfirmedEchoes`).
 *
 * Plain fetches, no react-query: ChatView renders without a QueryClient in
 * many tests, and one small poll does not need a cache.
 */
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { request, type RecordChat } from "@/lib/api";
import { chatUrl } from "@/lib/chatHistory";
import { buildChatList } from "@/lib/chatList";
import type { ChatEvent, StateEvent } from "@/lib/chatTypes";
import {
  chatReducer,
  createInitialChatState,
  ECHO_RETRY_DELAY_MS,
  markEchoRetried,
  markEchoStarting,
  markUnconfirmedEchoes,
  reconcilePendingEchoes,
  withdrawPendingEcho,
  type ChatReducerState,
  type PendingEcho,
  type UseChatStreamResult,
} from "@/hooks/useChatStream";

/** Read again this often while the chat is open and quiet. */
export const RECORD_CHAT_POLL_MS = 5_000;
/** …and this often from a send until the answer is in the record. */
export const RECORD_CHAT_FAST_POLL_MS = 1_500;
/** How much of the chat the first read brings (the backend allows up to 1000). */
export const RECORD_CHAT_PAGE = 500;
/** How much each poll after that brings: only the tail, merged by event id —
 *  a busy coding chat's 500 rows can be megabytes, every few seconds, on a
 *  phone. A poll page with nothing known in it is a gap: read the full page. */
export const RECORD_CHAT_POLL_PAGE = 50;
/** "Sent" never outlives this without any sign of the turn (the record has
 *  no live state to end it otherwise). */
export const RECORD_CHAT_AWAIT_CEILING_MS = 120_000;
/** A send made just before the first read may be in it; a user line in that
 *  read only matches an echo when it is not older than the send by more
 *  than this (clock skew between browser and backend). */
const SEED_MATCH_SKEW_MS = 60_000;
/** Rows that show the agent took the turn — same set as the agent's own chat
 *  (useChatStream), minus `usage`, which can belong to the previous turn. */
const TURN_SIGNS = new Set(["state", "tool", "thinking", "message", "preview", "tool_output"]);

export type RecordHistoryPage = {
  events: { id: string; seq: number; sessionId: string; event: ChatEvent }[];
  hasMore: boolean;
  chat?: { id: string; title: string; harness: string | null; model: string | null };
};

/** The URL of a further chat on the sessions page. */
export function furtherChatHref(agentId: string, chatId: string): string {
  return `${chatUrl("agent", agentId)}&chat=${encodeURIComponent(chatId)}`;
}

/** The record is off (404 `chat_record_disabled`): the caller shows the agent's own chat. */
export function isRecordOff(err: unknown): boolean {
  return err instanceof Error && err.message.startsWith("API 404") && err.message.includes("chat_record_disabled");
}

/** Any other 404 of the history: this chat is not there (or not for this user). */
export function isChatMissing(err: unknown): boolean {
  return err instanceof Error && err.message.startsWith("API 404") && !isRecordOff(err);
}

/** The id of the chat a `/new` opened — the input route's 201 `{"newChat": …}`. */
export function newChatIdOf(res: unknown): string | null {
  if (!res || typeof res !== "object") return null;
  const chat = (res as { newChat?: unknown }).newChat;
  if (!chat || typeof chat !== "object") return null;
  const id = (chat as { id?: unknown }).id;
  return typeof id === "string" && id.length > 0 ? id : null;
}

/** Text into a further chat: the agent's input route with `chatId`. Same
 *  CRLF normalising as `api.chat.sendText`. Resolves to the response body
 *  (`{"newChat": …}` after a `/new`), or undefined on 204. */
export function sendToChat(agentId: string, chatId: string, text: string): Promise<unknown> {
  return request<unknown>(`/api/v1/agents/${agentId}/chat/input`, {
    method: "POST",
    body: JSON.stringify({ text: text.replace(/\r\n/g, "\n").replace(/\r/g, "\n"), chatId }),
  });
}

export function fetchRecordHistory(chatId: string, limit = RECORD_CHAT_PAGE): Promise<RecordHistoryPage> {
  return request<RecordHistoryPage>(
    `/api/v1/chat-record/chats/${encodeURIComponent(chatId)}/history?limit=${limit}`,
  );
}

function tsMs(ev: ChatEvent): number | null {
  const ts = (ev as { ts?: unknown }).ts;
  if (typeof ts !== "string") return null;
  const ms = Date.parse(ts);
  return Number.isFinite(ms) ? ms : null;
}

/** An echo still on its way (counts for the fast poll); "unconfirmed" is not. */
function inFlight(e: PendingEcho): boolean {
  return e.status !== "unconfirmed";
}

/**
 * The header name of a further chat — the same words its row in the chat
 * list carries ("Cody · chat 2", or its own title), so the open chat and its
 * row never disagree. null when the record does not list it (yet).
 */
export function furtherChatLabel(
  chats: RecordChat[],
  agent: { id: string; name: string },
  chatId: string,
  format: (name: string, n: number) => string,
): string | null {
  const entry = buildChatList({
    agents: [{ id: agent.id, name: agent.name, status: "idle", last_task_activity_at: null, updated_at: "" }],
    groups: [],
    headRuns: [],
    approvals: [],
    recordChats: chats,
  }).find((e) => e.chatId === chatId);
  if (!entry) return null;
  return entry.ordinal ? format(entry.title, entry.ordinal) : entry.title;
}

const NO_OP_WITHDRAW = (): string[] => [];
const NO_EVENTS: UseChatStreamResult["events"] = [];
const NO_OUTPUTS: UseChatStreamResult["toolOutputs"] = {};
const NO_RESULTS: UseChatStreamResult["elicitationResults"] = {};
/** The record carries no live state frame. Read and quiet = ready; the
 *  "Sent" line covers the turn after a send (`awaitingResponse`). Never
 *  "unknown": nothing is wrong, there is just no probe for this chat. */
const RECORD_IDLE: StateEvent = { kind: "state", status: "idle", prompt: null };

export type RecordChatResult = UseChatStreamResult & {
  /** The record is off (404 `chat_record_disabled`) — show the agent's own chat. */
  disabled: boolean;
  /** The chat is not there (any other 404) — the caller drops `&chat=`. */
  missing: boolean;
  /** The chats of the record, for the header name (empty until loaded). */
  recordChats: RecordChat[];
};

export function useRecordChat(chatId: string | null, active = true): RecordChatResult {
  // Lazy on purpose: without a chat id nothing of the reducer is touched —
  // the agent's own chat must not depend on this hook at all.
  const [chatState, dispatch] = useReducer(
    (state: ChatReducerState | null, event: ChatEvent) => chatReducer(state ?? createInitialChatState(), event),
    chatId,
    (id: string | null) => (id ? createInitialChatState() : null),
  );
  const [loading, setLoading] = useState(chatId != null);
  const [error, setError] = useState<Error | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [echoes, setEchoesState] = useState<PendingEcho[]>([]);
  // Mirror of `echoes` for `apply`, which must decide synchronously whether a
  // row matched a send (the seed read only counts rows after a matched line).
  const echoesRef = useRef<PendingEcho[]>([]);
  const setEchoes = useCallback((update: (prev: PendingEcho[]) => PendingEcho[]) => {
    const next = update(echoesRef.current);
    if (next === echoesRef.current) return;
    echoesRef.current = next;
    setEchoesState(next);
  }, []);
  const [awaiting, setAwaiting] = useState(false);
  // Bumped on every send: restarts the "Sent" ceiling.
  const [sendSeq, setSendSeq] = useState(0);
  const [recordChats, setRecordChats] = useState<RecordChat[]>([]);
  const seen = useRef<Set<string>>(new Set());
  const seeded = useRef(false);

  // A new chat id starts from nothing (the sessions page remounts ChatView
  // on a chat change; this keeps the hook honest on its own too).
  const prevChat = useRef<string | null>(chatId);
  if (prevChat.current !== chatId) {
    prevChat.current = chatId;
    seen.current = new Set();
    seeded.current = false;
    echoesRef.current = [];
    dispatch({ kind: "session_changed" } as ChatEvent);
  }

  const apply = useCallback((page: RecordHistoryPage, full: boolean) => {
    const fresh: ChatEvent[] = [];
    for (const row of page.events ?? []) {
      if (!row?.event) continue;
      if (!seen.current.has(row.id)) fresh.push(row.event);
      seen.current.add(row.id);
      // Every row again: a tool card that got its result later is the same
      // key with a fuller payload — the reducer replaces it in place.
      dispatch(row.event);
    }
    // Only the full read knows whether older history exists.
    if (full) setHasMore(!!page.hasMore);
    const wasSeeded = seeded.current;
    seeded.current = true;
    if (fresh.length === 0 || (!wasSeeded && echoesRef.current.length === 0)) return;

    // After the first read every new row is the live tail. The first read
    // itself is old conversation — except for a send made before it landed:
    // only a user line no older than that send may match it (no fallback
    // to the oldest echo), and only rows after such a match end "Sent".
    let next = echoesRef.current;
    let matched = wasSeeded;
    let answered = false;
    const oldestSend = next.reduce((min, e) => Math.min(min, e.sentAt), Infinity);
    for (const ev of fresh) {
      if (ev.kind === "message" && ev.role === "user") {
        if (!wasSeeded) {
          const at = tsMs(ev);
          if (at != null && at < oldestSend - SEED_MATCH_SKEW_MS) continue;
        }
        const after = reconcilePendingEchoes(next, ev.text, { allowOldestFallback: wasSeeded });
        if (after !== next) matched = true;
        next = after;
        continue;
      }
      // Any sign the agent took the turn ends "Sent" — the same rule as the
      // agent's own chat. A turn may end with only a tool or error card.
      if (matched && TURN_SIGNS.has(ev.kind)) answered = true;
    }
    if (next !== echoesRef.current) setEchoes(() => next);
    if (answered) setAwaiting(false);
  }, [setEchoes]);

  const read = useCallback(async (tail = false) => {
    if (!chatId) return;
    try {
      let full = !tail || !seeded.current;
      let page = await fetchRecordHistory(chatId, full ? RECORD_CHAT_PAGE : RECORD_CHAT_POLL_PAGE);
      if (prevChat.current !== chatId) return;
      const rows = page.events ?? [];
      if (!full && page.hasMore && rows.length > 0 && rows.every((r) => !seen.current.has(r.id))) {
        // More arrived since the last read than one poll page holds.
        full = true;
        page = await fetchRecordHistory(chatId, RECORD_CHAT_PAGE);
        if (prevChat.current !== chatId) return;
      }
      setError(null);
      apply(page, full);
    } catch (err) {
      if (prevChat.current !== chatId) return;
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      if (prevChat.current === chatId) setLoading(false);
    }
  }, [chatId, apply]);

  // First read, and the record's chat list once for the header name.
  useEffect(() => {
    if (!chatId) return;
    setLoading(true);
    void read();
    let gone = false;
    request<{ chats: RecordChat[] }>("/api/v1/chat-record/chats")
      .then((res) => {
        if (!gone && Array.isArray(res?.chats)) setRecordChats(res.chats);
      })
      .catch(() => {});
    return () => {
      gone = true;
    };
  }, [chatId, read]);

  const disabled = chatId != null && isRecordOff(error);
  const missing = chatId != null && isChatMissing(error);
  const echoesInFlight = echoes.some(inFlight);
  const pollMs = awaiting || echoesInFlight ? RECORD_CHAT_FAST_POLL_MS : RECORD_CHAT_POLL_MS;
  useEffect(() => {
    if (!chatId || !active || disabled || missing) return;
    const id = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void read(true);
    }, pollMs);
    return () => clearInterval(id);
  }, [chatId, active, disabled, missing, pollMs, read]);

  // Echoes the record does not confirm in time say so — the same clock as
  // the agent's own chat. There is no live "working" state here, so the
  // clock always runs (a line the daemon holds behind a running reply can
  // read "unconfirmed" until it lands; it retires as soon as it does).
  const echoesPending = echoes.some((e) => e.status === "pending" || e.status === "queued");
  useEffect(() => {
    if (!echoesPending) return;
    const id = setInterval(() => setEchoes((prev) => markUnconfirmedEchoes(prev, Date.now(), false)), 1_000);
    return () => clearInterval(id);
  }, [echoesPending, setEchoes]);

  // "Sent" has a hard ceiling: with no state frame, a turn that writes
  // nothing (refused, cancelled, lost) would otherwise hold it for good.
  useEffect(() => {
    if (!awaiting) return;
    const id = setTimeout(() => setAwaiting(false), RECORD_CHAT_AWAIT_CEILING_MS);
    return () => clearTimeout(id);
  }, [awaiting, sendSeq]);

  const echoSent = useCallback(
    (text: string) => {
      setEchoes((prev) => [...prev, { id: `rec-${Date.now()}-${prev.length}`, text, sentAt: Date.now(), status: "pending" }]);
      setAwaiting(true);
      setSendSeq((n) => n + 1);
    },
    [setEchoes],
  );
  const echoFailed = useCallback((text: string) => {
    setEchoes((prev) => withdrawPendingEcho(prev, text));
    setAwaiting(false);
  }, [setEchoes]);
  const echoAgentStarting = useCallback((text: string, retry: () => void) => {
    let retried = false;
    setEchoes((prev) => {
      const echo = [...prev].reverse().find((e) => e.text === text);
      retried = !!echo?.retried;
      return markEchoStarting(prev, text);
    });
    setTimeout(() => {
      if (retried) return;
      setEchoes((prev) => markEchoRetried(prev, text));
      retry();
    }, ECHO_RETRY_DELAY_MS);
  }, [setEchoes]);

  return {
    events: chatState?.events ?? NO_EVENTS,
    subagentRuns: [],
    state: loading ? null : RECORD_IDLE,
    usage: null,
    session: null,
    hasMore,
    connected: !error,
    loading,
    error,
    capabilities: null,
    pendingEchoes: echoes,
    echoSent,
    echoFailed,
    echoAgentStarting,
    withdrawQueued: NO_OP_WITHDRAW,
    awaitingResponse: awaiting,
    preview: null,
    todos: chatState?.todos ?? null,
    toolOutputs: chatState?.toolOutputs ?? NO_OUTPUTS,
    elicitationResults: chatState?.elicitationResults ?? NO_RESULTS,
    mode: null,
    disabled,
    missing,
    recordChats,
  };
}
