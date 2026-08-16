"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useSSE } from "@/lib/sse";
import type {
  ChatEvent,
  ChatSession,
  StateEvent,
  TimelineChatEvent,
  UsageEvent,
} from "@/lib/chatTypes";

// ── Reducer ──────────────────────────────────────────────────────────────────
//
// Pure — no I/O, no React. `useChatStream` below feeds it both the initial
// history page (one dispatch per event) and every live "chat_event" SSE
// frame; both paths go through the exact same reducer, so replaying history
// and tailing live traffic behave identically.

export const MAX_CHAT_EVENTS = 1500;

export interface ChatReducerState {
  events: TimelineChatEvent[];
  /** Dedup/replace lookup key -> index into `events`. See `eventKey` for why
   *  this isn't simply the event's `uuid`. */
  index: Map<string, number>;
  state: StateEvent | null;
  usage: UsageEvent | null;
  /** Bumped every time a `session_changed` event is processed. The hook
   *  watches this to know a history refetch is needed — it can't rely on
   *  `events` being empty as the signal, since a second rollover could land
   *  before the first refetch re-seeds anything. */
  sessionChangedAt: number;
}

export function createInitialChatState(): ChatReducerState {
  return { events: [], index: new Map(), state: null, usage: null, sessionChangedAt: 0 };
}

/**
 * Claude Code stamps every content block within one assistant turn with the
 * same top-level entry `uuid` — a turn with two `tool_use` blocks (parallel
 * tool calls) produces two ToolEvents that share that `uuid` but carry
 * distinct `toolUseId`s (see `transcript_chat.py`'s module docstring).
 * Deduping tool events on `uuid` alone would collapse the second call into
 * the first and silently drop it. `toolUseId` is the part that's actually
 * unique per tool call, and it stays stable when the live tailer republishes
 * the same event (mutated in place) once its `tool_result` lands — which is
 * exactly the "same uuid REPLACES" case this needs to resolve correctly.
 */
function eventKey(ev: TimelineChatEvent): string {
  if (ev.kind === "tool" && ev.toolUseId) return `tool:${ev.toolUseId}`;
  return ev.uuid;
}

function reindex(events: TimelineChatEvent[]): Map<string, number> {
  const index = new Map<string, number>();
  events.forEach((ev, i) => index.set(eventKey(ev), i));
  return index;
}

function pushOrReplace(state: ChatReducerState, ev: TimelineChatEvent): ChatReducerState {
  const key = eventKey(ev);
  const existingIndex = state.index.get(key);

  if (existingIndex !== undefined) {
    if (ev.kind !== "tool") {
      // Non-tool duplicate (Claude Code can repeat a line verbatim across a
      // resumed session) — ignored, first write wins.
      return state;
    }
    const events = state.events.slice();
    events[existingIndex] = ev;
    return { ...state, events };
  }

  let events = state.events.concat(ev);
  let index: Map<string, number>;
  if (events.length > MAX_CHAT_EVENTS) {
    events = events.slice(events.length - MAX_CHAT_EVENTS);
    index = reindex(events);
  } else {
    index = new Map(state.index);
    index.set(key, events.length - 1);
  }
  return { ...state, events, index };
}

export function chatReducer(state: ChatReducerState, event: ChatEvent): ChatReducerState {
  switch (event.kind) {
    case "state":
      return { ...state, state: event };
    case "usage":
      return { ...state, usage: event };
    case "session_changed":
      return { ...state, events: [], index: new Map(), sessionChangedAt: state.sessionChangedAt + 1 };
    case "message":
    case "tool":
    case "thinking":
    case "command":
      return pushOrReplace(state, event);
    default:
      return state;
  }
}

// ── Hook ─────────────────────────────────────────────────────────────────────

export interface UseChatStreamResult {
  events: TimelineChatEvent[];
  state: StateEvent | null;
  usage: UsageEvent | null;
  session: ChatSession | null;
  hasMore: boolean;
  /** True once at least one frame (history load or live SSE event) has been
   *  observed since the last (re)connect attempt. There's no `onopen` signal
   *  wired into `useSSE` today, so this is an activity-based approximation,
   *  not a raw EventSource.readyState mirror. */
  connected: boolean;
  loading: boolean;
  error: Error | null;
}

/**
 * Loads an agent's chat history (TanStack Query) and tails its live
 * transcript (`useSSE` on the `chat_event` channel), reducing both into one
 * timeline via `chatReducer`. A `session_changed` frame — live tail rolled
 * onto a new transcript file — clears the timeline and triggers a history
 * refetch so the new session's own history re-seeds it, rather than trying
 * to reconcile two unrelated transcripts' events.
 */
export function useChatStream(agentId: string | null, enabled = true): UseChatStreamResult {
  const [chatState, dispatch] = useReducer(chatReducer, undefined, createInitialChatState);
  const [connected, setConnected] = useState(false);
  // Tracks which session's history has already been dispatched into the
  // reducer, so a query re-render with the same data doesn't re-seed (which
  // would be harmless — pushOrReplace is idempotent for non-tool dupes and
  // merges for tool dupes — but would still spam replace-work for nothing).
  const seededSessionIdRef = useRef<string | null>(null);

  const queryEnabled = enabled && !!agentId;

  const historyQuery = useQuery({
    queryKey: ["chat-history", agentId],
    queryFn: () => api.chat.history(agentId as string),
    enabled: queryEnabled,
  });

  useEffect(() => {
    const data = historyQuery.data;
    if (!data) return;
    if (seededSessionIdRef.current === data.session.sessionId) return;
    seededSessionIdRef.current = data.session.sessionId;
    for (const ev of data.events) dispatch(ev);
  }, [historyQuery.data]);

  const streamUrl = agentId ? api.chat.streamUrl(agentId) : "";

  const onSSEEvent = useCallback(
    (eventName: string, data: Record<string, unknown>) => {
      if (eventName !== "chat_event") return;
      setConnected(true);
      const ev = data as unknown as ChatEvent;
      dispatch(ev);
      if (ev.kind === "session_changed") {
        // The new session has different history — force a re-seed once the
        // refetch resolves instead of trusting the (now stale) sessionId.
        seededSessionIdRef.current = null;
        historyQuery.refetch();
      }
    },
    [historyQuery],
  );

  const onSSEError = useCallback(() => setConnected(false), []);

  useSSE(streamUrl, {
    enabled: queryEnabled && !!streamUrl,
    onEvent: onSSEEvent,
    onError: onSSEError,
  });

  return {
    events: chatState.events,
    state: chatState.state,
    usage: chatState.usage,
    session: historyQuery.data?.session ?? null,
    hasMore: historyQuery.data?.hasMore ?? false,
    connected,
    loading: historyQuery.isLoading,
    error: historyQuery.error as Error | null,
  };
}
