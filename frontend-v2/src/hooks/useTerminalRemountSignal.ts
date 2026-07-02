"use client";

/**
 * useTerminalRemountSignal — listens for backend-initiated terminal remount
 * events for a specific agent.
 *
 * The backend publishes on `mc:agent:{id}:terminal:remount` whenever the
 * underlying tmux container is recreated (e.g. after a runtime switch with
 * an image change in Phase 15). The Sessions page subscribes per visible
 * agent and re-mounts <TerminalPanel> when an event arrives so the user
 * doesn't see a frozen WebSocket pointing at the old container.
 */

import { useEffect, useRef } from "react";
import { getToken } from "@/lib/api";

export interface TerminalRemountPayload {
  reason?: string;
  image_changed?: boolean;
  ts?: number;
}

const BASE_URL = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");

export function useTerminalRemountSignal(
  agentId: string | null | undefined,
  onSignal: (payload: TerminalRemountPayload) => void,
) {
  const callbackRef = useRef(onSignal);

  useEffect(() => {
    callbackRef.current = onSignal;
  });

  useEffect(() => {
    if (!agentId) return;

    const token = getToken();
    const url = `${BASE_URL}/api/v1/agents/${agentId}/terminal-events/stream?token=${token}`;
    const es = new EventSource(url, { withCredentials: true });

    const handler = (e: Event) => {
      const msg = e as MessageEvent;
      try {
        const data = JSON.parse(msg.data) as TerminalRemountPayload;
        callbackRef.current(data ?? {});
      } catch {
        callbackRef.current({});
      }
    };

    // Backend emits a named event "terminal_remount". Default `message`
    // catches the bare-message fallback if the dispatcher ever shifts shape.
    es.addEventListener("terminal_remount", handler);
    es.onmessage = handler;

    return () => {
      es.removeEventListener("terminal_remount", handler);
      es.close();
    };
  }, [agentId]);
}
