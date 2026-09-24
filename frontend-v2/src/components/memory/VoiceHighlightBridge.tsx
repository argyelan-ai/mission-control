"use client";

/**
 * VoiceHighlightBridge — headless WebSocket subscriber for voice-driven graph highlights.
 *
 * Connects to /api/v1/vault/voice-highlight (wired in M.4 T2 backend).
 * When a valid filter message arrives it calls `onHighlight` so the parent page
 * can update the graph filter state.
 *
 * Renders nothing — purely a side-effect coordinator.
 *
 * Reconnect strategy: on close / error the effect re-runs (dependency array
 * includes wsUrl). The parent's useVoiceHighlight hook always provides a stable
 * callback reference so the socket is only recreated on remount. Auth: a
 * single-use stream ticket (lib/streamTicket.ts), never the login token.
 */

import { useEffect, useRef } from "react";
import { openTicketedWebSocket } from "@/lib/streamTicket";
import type { GraphFilter } from "@/lib/types";

export interface VoiceHighlightBridgeProps {
  onHighlight: (filter: GraphFilter) => void;
}

export function VoiceHighlightBridge({ onHighlight }: VoiceHighlightBridgeProps) {
  // Keep a stable ref so the effect closure always calls the latest callback
  // without needing it in the dependency array (avoids reconnect on every render).
  const onHighlightRef = useRef(onHighlight);
  useEffect(() => {
    onHighlightRef.current = onHighlight;
  });

  useEffect(() => {
    // SSR guard — window is not available during Next.js static rendering.
    if (typeof window === "undefined") return;

    const cleanup = openTicketedWebSocket("/api/v1/vault/voice-highlight", (ws) => {
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data as string) as {
            type?: string;
            filter?: GraphFilter;
          };
          if (msg.type === "ping") return; // backend heartbeat — ignore
          if (msg.filter) onHighlightRef.current(msg.filter);
        } catch (err) {
          console.error("[VoiceHighlightBridge] parse error:", err);
        }
      };

      ws.onerror = (err) => {
        console.warn("[VoiceHighlightBridge] WebSocket error:", err);
      };

      // Reconnect after an unexpected close is handled by
      // openTicketedWebSocket (fresh ticket, backoff).
    });

    return cleanup;
  }, []); // stable URL derived inside effect; token changes require remount

  return null;
}
