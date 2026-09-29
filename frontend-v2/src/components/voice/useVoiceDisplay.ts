/**
 * useVoiceDisplay — WebSocket hook that accumulates display cards from
 * Jarvis's show_* function-tools.
 *
 * Pattern matches VoiceHighlightBridge: open WS to
 * /api/v1/vault/voice-display, parse incoming JSON, append to state.
 *
 * - `enabled` gates the connection — pass `active` from VoiceContext so
 *   we only hold a socket while there's an active voice session.
 * - Cards are capped at MAX_CARDS so the drawer doesn't grow unbounded
 *   during long calls (oldest fall off the bottom).
 * - `clear()` resets the stack (called on endSession in VoiceProvider).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { getToken } from "@/lib/authToken";
import { openTicketedWebSocket } from "@/lib/streamTicket";
import type { DisplayCard, DisplayCardMessage } from "./cards/types";

const MAX_CARDS = 8;

export function useVoiceDisplay(enabled: boolean) {
  const [cards, setCards] = useState<DisplayCard[]>([]);
  const closingRef = useRef(false);

  const clear = useCallback(() => setCards([]), []);

  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;

    const token = getToken();
    if (!token) return;

    closingRef.current = false;
    // Single-use stream ticket instead of the login token in the URL.
    const cleanup = openTicketedWebSocket("/api/v1/vault/voice-display", (ws) => {
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data as string) as Partial<DisplayCardMessage> & {
            type?: string;
          };
          if (msg.type === "ping") return;
          if (!msg.kind || !msg.data) return;
          const card: DisplayCard = {
            ...(msg as DisplayCardMessage),
            // Stable id for AnimatePresence keying — backend doesn't provide
            // one, the timestamp + kind + a short random suffix is unique enough.
            id: `${msg.kind}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
          };
          setCards((prev) => {
            const next = [card, ...prev];
            return next.length > MAX_CARDS ? next.slice(0, MAX_CARDS) : next;
          });
        } catch (err) {
          console.error("[useVoiceDisplay] parse error:", err);
        }
      };

      ws.onerror = (err) => {
        console.warn("[useVoiceDisplay] WS error:", err);
      };
    });

    return () => {
      closingRef.current = true;
      cleanup();
    };
  }, [enabled]);

  return { cards, clear };
}
