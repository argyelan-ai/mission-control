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
 * - The record answers 404 while `CHAT_RECORD_ENABLED` is off (and for a
 *   chat that is not there): then `disabled` is true and the caller shows
 *   the agent's own chat exactly as before.
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
/** How much of the chat one read brings (the backend allows up to 1000). */
export const RECORD_CHAT_PAGE = 500;

export type RecordHistoryPage = {
  events: { id: string; seq: number; sessionId: string; event: ChatEvent }[];
  hasMore: boolean;
  chat?: { id: string; title: string; harness: string | null; model: string | null };
};

/** The URL of a further chat on the sessions page. */
export function furtherChatHref(agentId: string, chatId: string): string {
  return `${chatUrl("agent", agentId)}&chat=${encodeURIComponent(chatId)}`;
}

/** The record is off (or the chat is gone): the caller shows the agent's own chat. */
export function isRecordOff(err: unknown): boolean {
  return err instanceof Error && err.message.startsWith("API 404");
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

export function fetchRecordHistory(chatId: string): Promise<RecordHistoryPage> {
  return request<RecordHistoryPage>(
    `/api/v1/chat-record/chats/${encodeURIComponent(chatId)}/history?limit=${RECORD_CHAT_PAGE}`,
  );
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
  /** The record said 404: flag off or no such chat — show the agent's own chat. */
  disabled: boolean;
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
  const [echoes, setEchoes] = useState<PendingEcho[]>([]);
  const [awaiting, setAwaiting] = useState(false);
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
    dispatch({ kind: "session_changed" } as ChatEvent);
  }

  const apply = useCallback((page: RecordHistoryPage) => {
    const fresh: ChatEvent[] = [];
    for (const row of page.events ?? []) {
      if (!row?.event) continue;
      if (!seen.current.has(row.id)) fresh.push(row.event);
      seen.current.add(row.id);
      // Every row again: a tool card that got its result later is the same
      // key with a fuller payload — the reducer replaces it in place.
      dispatch(row.event);
    }
    setHasMore(!!page.hasMore);
    if (seeded.current && fresh.length > 0) {
      // Only what arrived after the first read retires an echo — the first
      // page is old conversation and must never claim a fresh delivery.
      setEchoes((prev) => {
        let next = prev;
        for (const ev of fresh) {
          if (ev.kind === "message" && ev.role === "user") next = reconcilePendingEchoes(next, ev.text);
        }
        return next;
      });
      // The answer is in once the assistant has written a message; tool
      // rows before it are the turn still running ("Sent" stays up).
      if (fresh.some((ev) => ev.kind === "message" && ev.role === "assistant")) setAwaiting(false);
    }
    seeded.current = true;
  }, []);

  const read = useCallback(async () => {
    if (!chatId) return;
    try {
      const page = await fetchRecordHistory(chatId);
      if (prevChat.current !== chatId) return;
      setError(null);
      apply(page);
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
  const pollMs = awaiting || echoes.length > 0 ? RECORD_CHAT_FAST_POLL_MS : RECORD_CHAT_POLL_MS;
  useEffect(() => {
    if (!chatId || !active || disabled) return;
    const id = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void read();
    }, pollMs);
    return () => clearInterval(id);
  }, [chatId, active, disabled, pollMs, read]);

  const echoSent = useCallback(
    (text: string) => {
      setEchoes((prev) => [...prev, { id: `rec-${Date.now()}-${prev.length}`, text, sentAt: Date.now(), status: "pending" }]);
      setAwaiting(true);
    },
    [],
  );
  const echoFailed = useCallback((text: string) => {
    setEchoes((prev) => withdrawPendingEcho(prev, text));
    setAwaiting(false);
  }, []);
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
  }, []);

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
    recordChats,
  };
}
