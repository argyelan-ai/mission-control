"use client";

import { useEffect, useRef } from "react";
import { sseUrls } from "./api";
import { withStreamTicket } from "./streamTicket";

interface SSEOptions {
  onEvent?: (event: string, data: Record<string, unknown>) => void;
  onError?: (error: Event) => void;
  /** Fired every time `connect()` runs with a connection ALREADY in flight —
   *  i.e. after a drop, a mobile resume, or a stale-stream reconnect, never on
   *  the first mount. This is the only honest "the stream may have skipped
   *  events" signal: the backend seeds a fresh tailer at EOF and never replays
   *  what happened while we were away, so consumers must refetch. Do NOT turn
   *  this into "every focus event": see the note on the chat-history query. */
  onReconnect?: () => void;
  enabled?: boolean;
}

// Named SSE events forwarded to consumers
const NAMED_EVENTS = [
  // Keepalive des Backends (services/sse.py). Traegt keine Nutzlast, ist auf
  // einem ruhenden Agenten aber der einzige Beweis, dass die Verbindung
  // steht — ohne ihn meldete die Chat-Statuszeile dauerhaft "Status unklar".
  "ping",
  "task.created", "task.updated", "task.deleted",
  "task.status_changed", "task.assigned", "task.commented",
  "agent.status_changed", "agent.context_warning", "agent.metrics_updated", "agent.reset", "agent.chat.reply",
  "agent.went_offline", "agent.session_lost", "agent.skills_updated", "agent.restart_failed",
  "task.reminder_sent", "task.agent_unresponsive", "task.stale_progress", "task.auto_dispatched",
  "task.pending_dispatch_delivered", "task.dispatch_queued", "task.dispatch_fallback",
  "planner.started", "planner.finalized", "planner.reply",
  "research.started", "research.reply", "research.completed",
  "content.created", "content.stage_changed", "content.published",
  "approval.created", "approval.resolved",
  "job.started", "job.completed",
  "meeting.scheduled", "meeting.started", "meeting.completed", "meeting.failed",
  "meeting.cancelled", "meeting.topic_started", "meeting.agent_thinking", "meeting.message_received",
  // Gruppenchat (ADR-075) — Kanal mc:events:group:{id}
  "group.message_posted", "group.round_started", "group.turn_started",
  "group.round_completed", "group.doc_updated", "group.gate_requested",
  "group.status_changed", "group.member_changed",
  "chat.message", "chat_event", "memory.created", "project.updated", "system.alert",
  "system.rpc_disconnected", "system.rpc_reconnected", "system.slow_response", "system.component_down",
  // /agents/{id}/terminal-events/stream (useTerminalRemountSignal)
  "terminal_remount",
] as const;

// Backoff constants for reconnect (M14 — iOS kills SSE on app/tab switch)
const BACKOFF_BASE_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
const STALE_THRESHOLD_MS = 10_000; // reconnect if no message within this window after becoming visible

export function useSSE(url: string, options: SSEOptions = {}) {
  const { onEvent, onError, onReconnect, enabled = true } = options;
  const esRef = useRef<EventSource | null>(null);
  const onEventRef = useRef(onEvent);
  const onErrorRef = useRef(onError);
  const onReconnectRef = useRef(onReconnect);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastMessageAtRef = useRef<number>(Date.now());
  const destroyedRef = useRef(false);
  // True once a stream was open at least once in this effect — survives the
  // gap while a reconnect is still fetching its ticket, so onReconnect fires
  // for that reconnect even though esRef is momentarily null.
  const hadConnectionRef = useRef(false);

  useEffect(() => {
    onEventRef.current = onEvent;
    onErrorRef.current = onError;
    onReconnectRef.current = onReconnect;
  });

  useEffect(() => {
    if (!enabled || !url) return;

    destroyedRef.current = false;
    hadConnectionRef.current = false;

    // Bumped by every connect() and by cleanup: a ticket fetch that resolves
    // after a newer connect (or after unmount) must not open a stale stream.
    let generation = 0;

    function attachHandlers(es: EventSource) {
      // A stream that opened is healthy again: start the next backoff from
      // the base delay. Quiet streams only get comment pings (no event), so
      // resetting on events alone let the delay creep up to the 30 s cap.
      es.onopen = () => {
        if (esRef.current === es) retryCountRef.current = 0;
      };
      es.onmessage = (e: MessageEvent) => {
        lastMessageAtRef.current = Date.now();
        retryCountRef.current = 0;
        try {
          const data = JSON.parse(e.data) as Record<string, unknown>;
          onEventRef.current?.("message", data);
        } catch {
          // ignore malformed events
        }
      };

      NAMED_EVENTS.forEach((eventType) => {
        es.addEventListener(eventType, (e: Event) => {
          lastMessageAtRef.current = Date.now();
          retryCountRef.current = 0;
          const msgEvent = e as MessageEvent;
          try {
            const data = JSON.parse(msgEvent.data) as Record<string, unknown>;
            onEventRef.current?.(eventType, data);
          } catch {
            // ignore
          }
        });
      });

      es.onerror = (e: Event) => {
        if (esRef.current !== es) return; // a stale, already replaced stream
        onErrorRef.current?.(e);
        // The native EventSource retry would replay the SAME URL — whose
        // single-use stream ticket is already spent, so it can only 401.
        // Close it and reconnect ourselves with a fresh ticket (exponential
        // backoff). This also covers iOS hard-killing the connection in the
        // background (readyState CLOSED, no native retry).
        es.close();
        scheduleReconnect();
      };
    }

    function scheduleReconnect() {
      if (destroyedRef.current) return;
      // Only reconnect when the ES is actually closed (avoids double-connect
      // when the native EventSource is already mid-retry).
      if (esRef.current && esRef.current.readyState !== EventSource.CLOSED) return;

      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      const delay = Math.min(
        BACKOFF_BASE_MS * Math.pow(2, retryCountRef.current),
        BACKOFF_MAX_MS
      );
      retryCountRef.current += 1;
      retryTimerRef.current = setTimeout(() => {
        if (destroyedRef.current) return;
        connect();
      }, delay);
    }

    function connect() {
      if (destroyedRef.current) return;
      // A connection already in flight at entry means this is NOT the first
      // mount: something dropped or was killed (iOS background, container
      // restart, silent stall). Everything the backend broadcast in the
      // meantime is gone — the tailer starts a fresh consumer at EOF and never
      // replays — so this is the exact moment consumers must refetch.
      const hadConnection = hadConnectionRef.current;
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
      const myGeneration = ++generation;
      // Every connect — first mount and every reconnect — gets its own
      // single-use ticket. The login token never goes into the URL.
      withStreamTicket(url).then(
        (ticketUrl) => {
          if (destroyedRef.current || myGeneration !== generation) return;
          const es = new EventSource(ticketUrl, { withCredentials: true });
          esRef.current = es;
          hadConnectionRef.current = true;
          attachHandlers(es);
          if (hadConnection) onReconnectRef.current?.();
        },
        () => {
          // Ticket fetch failed (network, backend restarting, session gone):
          // retry with backoff like any other dropped connection.
          if (destroyedRef.current || myGeneration !== generation) return;
          scheduleReconnect();
        },
      );
    }

    // iOS M14: reconnect when tab becomes visible again and the connection is
    // dead or has been silent too long (OS may have killed it without error).
    function onVisibilityChange() {
      if (document.visibilityState !== "visible") return;
      const isClosed = !esRef.current || esRef.current.readyState === EventSource.CLOSED;
      const isStale = Date.now() - lastMessageAtRef.current > STALE_THRESHOLD_MS;
      if (isClosed || isStale) {
        retryCountRef.current = 0; // reset backoff on manual/visibility reconnect
        connect();
      }
    }

    connect();
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      destroyedRef.current = true;
      generation += 1;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      esRef.current?.close();
      esRef.current = null;
    };
  }, [url, enabled]);
}

export function useAgentStream(onEvent: SSEOptions["onEvent"]) {
  useSSE(sseUrls.agents(), { onEvent });
}

export function useActivityStream(onEvent: SSEOptions["onEvent"]) {
  useSSE(sseUrls.activity(), { onEvent });
}

export function useApprovalStream(onEvent: SSEOptions["onEvent"]) {
  useSSE(sseUrls.approvals(), { onEvent });
}
