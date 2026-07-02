"use client";

import { useEffect, useRef } from "react";
import { getToken } from "./api";

interface SSEOptions {
  onEvent?: (event: string, data: Record<string, unknown>) => void;
  onError?: (error: Event) => void;
  enabled?: boolean;
}

// Named SSE events forwarded to consumers
const NAMED_EVENTS = [
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
  "workflow.run.started", "workflow.run.paused", "workflow.run.stopped",
  "workflow.run.completed", "workflow.run.failed", "workflow.run.partial",
  "workflow.run.force_stopped",
  "meeting.scheduled", "meeting.started", "meeting.completed", "meeting.failed",
  "meeting.cancelled", "meeting.topic_started", "meeting.agent_thinking", "meeting.message_received",
  "chat.message", "memory.created", "project.updated", "system.alert",
  "system.rpc_disconnected", "system.rpc_reconnected", "system.slow_response", "system.component_down",
] as const;

// Backoff constants for reconnect (M14 — iOS kills SSE on app/tab switch)
const BACKOFF_BASE_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
const STALE_THRESHOLD_MS = 10_000; // reconnect if no message within this window after becoming visible

export function useSSE(url: string, options: SSEOptions = {}) {
  const { onEvent, onError, enabled = true } = options;
  const esRef = useRef<EventSource | null>(null);
  const onEventRef = useRef(onEvent);
  const onErrorRef = useRef(onError);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastMessageAtRef = useRef<number>(Date.now());
  const destroyedRef = useRef(false);

  useEffect(() => {
    onEventRef.current = onEvent;
    onErrorRef.current = onError;
  });

  useEffect(() => {
    if (!enabled || !url) return;

    destroyedRef.current = false;

    function buildUrl() {
      const token = getToken();
      return `${url}${url.includes("?") ? "&" : "?"}token=${token}`;
    }

    function attachHandlers(es: EventSource) {
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
        onErrorRef.current?.(e);
        // EventSource natively reconnects for transient errors, but iOS often
        // hard-kills the connection when the app goes to background — in that
        // case readyState stays CLOSED and no reconnect fires. Detect and retry
        // with exponential backoff.
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
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
      const es = new EventSource(buildUrl(), { withCredentials: true });
      esRef.current = es;
      attachHandlers(es);
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
      document.removeEventListener("visibilitychange", onVisibilityChange);
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      esRef.current?.close();
      esRef.current = null;
    };
  }, [url, enabled]);
}

export function useAgentStream(onEvent: SSEOptions["onEvent"]) {
  const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  useSSE(`${BASE_URL}/api/v1/agents/stream`, { onEvent });
}

export function useActivityStream(onEvent: SSEOptions["onEvent"]) {
  const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  useSSE(`${BASE_URL}/api/v1/activity/stream`, { onEvent });
}

export function useApprovalStream(onEvent: SSEOptions["onEvent"]) {
  const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  useSSE(`${BASE_URL}/api/v1/approvals/stream`, { onEvent });
}
