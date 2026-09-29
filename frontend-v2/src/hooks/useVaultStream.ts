import { useEffect, useRef } from "react";
import { getToken } from "@/lib/authToken";
import { openTicketedWebSocket } from "@/lib/streamTicket";

/**
 * useVaultStream — WebSocket hook for live vault note updates.
 *
 * Connects to /api/v1/vault/stream (wired in M.2 backend).
 * Fires onMessage for every non-ping message so the caller can react
 * (e.g. invalidate the vault graph query on "modified" / "compacted").
 *
 * Reconnect strategy: the effect re-runs whenever `enabled` changes.
 * The WebSocket is closed on unmount or when `enabled` becomes false.
 * Auth: a single-use stream ticket per connection (lib/streamTicket.ts) —
 * the login token never goes into the URL.
 *
 * Used by VaultGraphPage (T10).
 */

type VaultStreamMessage =
  | { type: "modified" | "compacted" | "conflict" | "deleted" | "restored"; path: string; [k: string]: unknown }
  | { type: "trash_purged"; filename: string; [k: string]: unknown }
  | { type: "ping"; ts: string };

interface UseVaultStreamOptions {
  enabled?: boolean;
  onMessage?: (msg: VaultStreamMessage) => void;
}

export function useVaultStream({ enabled = true, onMessage }: UseVaultStreamOptions = {}) {
  const onMessageRef = useRef(onMessage);

  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

  useEffect(() => {
    if (!enabled) return;
    const token = getToken();
    if (!token) return; // not authenticated; skip silently

    return openTicketedWebSocket("/api/v1/vault/stream", (ws) => {
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as VaultStreamMessage;
          if (msg.type === "ping") return; // heartbeat — ignore
          onMessageRef.current?.(msg);
        } catch (err) {
          console.error("[useVaultStream] parse error:", err);
        }
      };

      ws.onerror = (err) => {
        console.warn("[useVaultStream] WebSocket error:", err);
      };
    });
  }, [enabled]);
}
