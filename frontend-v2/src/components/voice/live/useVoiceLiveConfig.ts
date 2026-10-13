"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export interface VoiceLiveConfig {
  enabled: boolean;
  model: string;
  voice: string;
}

// One lookup per page load: the switch only changes with a backend restart.
let cached: Promise<VoiceLiveConfig | null> | null = null;

/** `GET /api/v1/voice/live/config` — 404 (switch off or no OpenAI key) and
 *  any failure mean "no voice": the mic is then not rendered at all
 *  (DESIGN K3, nothing empty). */
export function loadVoiceLiveConfig(): Promise<VoiceLiveConfig | null> {
  if (!cached) {
    // Through a promise so even a missing client (a test's partial api mock)
    // ends as "no voice" instead of throwing into the chat.
    cached = Promise.resolve()
      .then(() => api.voiceLive.config())
      .then((c) => (c?.enabled ? c : null))
      .catch(() => null);
  }
  return cached;
}

/** Test seam. */
export function resetVoiceLiveConfigCache(): void {
  cached = null;
}

export function useVoiceLiveEnabled(active = true): boolean {
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    void loadVoiceLiveConfig().then((c) => {
      if (alive) setEnabled(c !== null);
    });
    return () => {
      alive = false;
    };
  }, [active]);
  return enabled;
}
