"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useSSE } from "@/lib/sse";
import type {
  ChatCapabilities,
  ChatEvent,
  ChatSession,
  StateEvent,
  TimelineChatEvent,
  UsageEvent,
} from "@/lib/chatTypes";

// ── Optimistic echo ──────────────────────────────────────────────────────────
//
// A sent message used to appear only once the tailer had polled the transcript
// (1s interval) and the CLI had written the line — so the operator's own words
// took over a second to show up, which is what "fühlt sich nicht snappy an"
// was about. The echo renders the bubble locally the instant it is sent and
// steps aside when the real transcript event lands.
//
// It is deliberately NOT pushed through `chatReducer`: that reducer is a pure
// projection of the transcript, and mixing a local guess into it would make
// "what the agent actually recorded" unanswerable.

/** How long an echo may stay unconfirmed before it says so. Generous on
 *  purpose — the floor is the tailer's 1s poll plus however long the CLI takes
 *  to flush the line, and a busy agent can be slower than that. */
export const ECHO_CONFIRM_TIMEOUT_MS = 10_000;

export interface PendingEcho {
  /** Local id — never a transcript uuid, so it can't collide with one. */
  id: string;
  text: string;
  sentAt: number;
  /** `unconfirmed` = the timeout passed while the stream was healthy, so the
   *  message may never have reached the CLI. Truthful, not a spinner. */
  status: "pending" | "unconfirmed";
}

/** Whitespace-insensitive compare: the CLI echoes back what it received, but a
 *  trailing newline or a re-wrapped paste must still count as the same message. */
function sameMessage(a: string, b: string): boolean {
  const norm = (s: string) => s.replace(/\s+/g, " ").trim();
  return norm(a) === norm(b);
}

/**
 * Retires the echo that an incoming transcript user-message corresponds to.
 *
 * Prefers a text match. Falls back to the OLDEST echo when nothing matches,
 * because the two failure modes are not symmetric: retiring an echo early only
 * removes a local copy while the real message is on screen anyway, whereas
 * keeping it would show the same message twice — visible, and it looks broken.
 * (The operator typing directly in the terminal is the case that hits the
 * fallback; losing a still-pending echo there is harmless.)
 *
 * Pure so the rule can be tested without React.
 */
export function reconcilePendingEchoes(echoes: PendingEcho[], incomingText: string): PendingEcho[] {
  if (echoes.length === 0) return echoes;
  const matched = echoes.findIndex((e) => sameMessage(e.text, incomingText));
  const drop = matched >= 0 ? matched : 0;
  return echoes.filter((_, i) => i !== drop);
}

/** Removes the newest echo with this text — a failed send is always the most
 *  recent one, and nothing was delivered, so the bubble must go. */
export function withdrawPendingEcho(echoes: PendingEcho[], text: string): PendingEcho[] {
  const reversedIdx = [...echoes].reverse().findIndex((e) => sameMessage(e.text, text));
  if (reversedIdx < 0) return echoes;
  const idx = echoes.length - 1 - reversedIdx;
  return echoes.filter((_, i) => i !== idx);
}

/** Flips echoes that have gone unacknowledged past the timeout. Returns the
 *  same array when nothing changed, so callers can skip a re-render. */
export function markUnconfirmedEchoes(echoes: PendingEcho[], now: number): PendingEcho[] {
  let changed = false;
  const next = echoes.map((e) => {
    if (e.status === "pending" && now - e.sentAt >= ECHO_CONFIRM_TIMEOUT_MS) {
      changed = true;
      return { ...e, status: "unconfirmed" as const };
    }
    return e;
  });
  return changed ? next : echoes;
}

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
  /** Server-derived harness capabilities (effort levels). `null` while history
   *  is still loading, or on a backend that predates the field. */
  capabilities: ChatCapabilities | null;
  /** Locally-rendered messages awaiting their transcript confirmation, oldest
   *  first. Render these AFTER `events`. */
  pendingEchoes: PendingEcho[];
  /** Call the moment a send is dispatched — before the request resolves. */
  echoSent: (text: string) => void;
  /** Call when that send failed: the echo is removed again, since nothing was
   *  delivered and a lingering bubble would claim otherwise. */
  echoFailed: (text: string) => void;
  /** True from a send until the transcript shows any sign of life (a state
   *  change, a tool call, a message). Drives the honest "Gesendet…" line. */
  awaitingResponse: boolean;
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
  const [pendingEchoes, setPendingEchoes] = useState<PendingEcho[]>([]);
  const [awaitingResponse, setAwaitingResponse] = useState(false);
  const echoSeqRef = useRef(0);
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

  const echoSent = useCallback((text: string) => {
    echoSeqRef.current += 1;
    setPendingEchoes((prev) => [
      ...prev,
      { id: `echo-${echoSeqRef.current}`, text, sentAt: Date.now(), status: "pending" },
    ]);
    setAwaitingResponse(true);
  }, []);

  const echoFailed = useCallback((text: string) => {
    setPendingEchoes((prev) => withdrawPendingEcho(prev, text));
    setAwaitingResponse(false);
  }, []);

  /** Retires the echo this transcript message corresponds to. Prefers a text
   *  match; falls back to the oldest echo, because a visible duplicate bubble is
   *  a worse failure than retiring one echo early (the real message is on screen
   *  either way — only the local copy disappears). */
  const reconcileEcho = useCallback((incomingText: string) => {
    setPendingEchoes((prev) => reconcilePendingEchoes(prev, incomingText));
  }, []);

  // Retire echoes that history brings in (a refetch after session rollover can
  // carry a message that was still pending locally).
  useEffect(() => {
    const data = historyQuery.data;
    if (!data) return;
    for (const ev of data.events) {
      if (ev.kind === "message" && ev.role === "user") reconcileEcho(ev.text);
    }
  }, [historyQuery.data, reconcileEcho]);

  // Flip long-unconfirmed echoes rather than leaving them looking delivered.
  // Only while the stream is healthy: on a dead stream we genuinely don't know,
  // and StatusLine already says the status is unclear.
  useEffect(() => {
    if (pendingEchoes.length === 0 || !connected) return;
    const timer = setInterval(() => {
      const now = Date.now();
      setPendingEchoes((prev) => markUnconfirmedEchoes(prev, now));
    }, 1_000);
    return () => clearInterval(timer);
  }, [pendingEchoes.length, connected]);

  const streamUrl = agentId ? api.chat.streamUrl(agentId) : "";

  const onSSEEvent = useCallback(
    (eventName: string, data: Record<string, unknown>) => {
      if (eventName !== "chat_event") return;
      setConnected(true);
      const ev = data as unknown as ChatEvent;
      dispatch(ev);
      if (ev.kind === "message" && ev.role === "user") reconcileEcho(ev.text);
      // Any sign the agent processed the turn ends the "Gesendet…" line. A
      // `usage` frame alone doesn't count — it can arrive for the previous turn.
      if (ev.kind === "state" || ev.kind === "tool" || ev.kind === "thinking" || ev.kind === "message") {
        setAwaitingResponse(false);
      }
      if (ev.kind === "session_changed") {
        // The new session has different history — force a re-seed once the
        // refetch resolves instead of trusting the (now stale) sessionId.
        seededSessionIdRef.current = null;
        historyQuery.refetch();
      }
    },
    [historyQuery, reconcileEcho],
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
    capabilities: historyQuery.data?.capabilities ?? null,
    pendingEchoes,
    echoSent,
    echoFailed,
    awaitingResponse,
  };
}
