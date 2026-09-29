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

import { useSSE } from "@/lib/sse";

export interface TerminalRemountPayload {
  reason?: string;
  image_changed?: boolean;
  ts?: number;
}

const BASE_URL = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");

/** Built on useSSE: every (re)connect fetches its own single-use stream
 *  ticket (lib/streamTicket.ts) — the login token never goes into the URL,
 *  and a dropped stream reconnects with backoff instead of dying on a spent
 *  ticket. */
export function useTerminalRemountSignal(
  agentId: string | null | undefined,
  onSignal: (payload: TerminalRemountPayload) => void,
) {
  useSSE(agentId ? `${BASE_URL}/api/v1/agents/${agentId}/terminal-events/stream` : "", {
    onEvent: (event, data) => {
      // Backend emits a named event "terminal_remount". Default `message`
      // catches the bare-message fallback if the dispatcher ever shifts shape.
      if (event === "terminal_remount" || event === "message") {
        onSignal((data ?? {}) as TerminalRemountPayload);
      }
    },
  });
}
