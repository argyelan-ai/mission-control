"use client";

import { useState, useEffect } from "react";
import type { GraphFilter } from "@/lib/types";

/**
 * useVoiceHighlight — state container for voice-driven graph filter commands.
 *
 * Pairs with VoiceHighlightBridge (which subscribes to the backend WebSocket).
 * The parent page passes `onVoiceHighlight` to the bridge, and reads back
 * `voiceFilter` to apply to the graph.
 *
 * Auto-clears after 30 s so the highlight doesn't persist forever.
 */
export function useVoiceHighlight() {
  const [activeFilter, setActiveFilter] = useState<GraphFilter | null>(null);

  // Auto-clear 30 s after a voice command lands.
  useEffect(() => {
    if (!activeFilter) return;
    const t = setTimeout(() => setActiveFilter(null), 30_000);
    return () => clearTimeout(t);
  }, [activeFilter]);

  return {
    /** Current voice-driven filter (null = no active highlight). */
    voiceFilter: activeFilter,
    /** Pass this as `onHighlight` to VoiceHighlightBridge. */
    onVoiceHighlight: setActiveFilter,
    /** Manually dismiss the highlight (e.g. when user clicks Reset). */
    clearVoiceHighlight: () => setActiveFilter(null),
  };
}
