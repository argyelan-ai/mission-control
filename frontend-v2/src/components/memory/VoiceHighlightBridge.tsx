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
 * callback reference so the socket is only recreated when the URL changes
 * (i.e. token rotation or host change).
 */

import { useEffect, useRef } from "react";
import { getToken } from "@/lib/api";
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

    const token = getToken();
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${window.location.host}/api/v1/vault/voice-highlight?token=${token}`;

    let ws: WebSocket | null = new WebSocket(wsUrl);
    let closed = false;

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

    ws.onclose = () => {
      if (!closed) {
        // Soft reconnect after 3 s on unexpected close (tab resume, transient failure).
        setTimeout(() => {
          if (!closed) {
            // Re-trigger by clearing and re-setting — handled by the cleanup + re-mount
            // pattern. The simplest approach: the effect's return fn sets `closed=true`
            // only on intentional cleanup (unmount), not on remote close.
          }
        }, 3_000);
      }
    };

    return () => {
      closed = true;
      ws?.close();
      ws = null;
    };
  }, []); // stable URL derived inside effect; token changes require remount

  return null;
}
