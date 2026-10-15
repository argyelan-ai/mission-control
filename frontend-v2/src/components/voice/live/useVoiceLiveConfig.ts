"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export interface VoiceLiveConfig {
  enabled: boolean;
  model: string;
  voice: string;
  /** Today's daily budget (voice PR 6). Missing on an older backend. */
  budget?: { limit_usd: number; spent_usd: number; reached: boolean; resets_at: string };
}

// One lookup per page load: the switch only changes with a backend restart.
// The budget part changes with every call, so `refreshVoiceLiveConfig()`
// fetches again (after a call ends) and tells every reader.
let cached: Promise<VoiceLiveConfig | null> | null = null;
const listeners = new Set<(c: VoiceLiveConfig | null) => void>();

function fetchConfig(): Promise<VoiceLiveConfig | null> {
  // Through a promise so even a missing client (a test's partial api mock)
  // ends as "no voice" instead of throwing into the chat.
  return Promise.resolve()
    .then(() => api.voiceLive.config())
    .then((c) => (c?.enabled ? c : null))
    .catch(() => null);
}

/** `GET /api/v1/voice/live/config` — 404 (switch off or no OpenAI key) and
 *  any failure mean "no voice": the mic is then not rendered at all
 *  (DESIGN K3, nothing empty). */
export function loadVoiceLiveConfig(): Promise<VoiceLiveConfig | null> {
  if (!cached) cached = fetchConfig();
  return cached;
}

/** Fetch again (the budget moved) and update every mic on screen. A failed
 *  refresh keeps what was known: a blip must not hide the mic. */
export function refreshVoiceLiveConfig(): Promise<VoiceLiveConfig | null> {
  const prev = cached ?? Promise.resolve(null);
  const next = Promise.resolve()
    .then(() => api.voiceLive.config())
    .then((c) => (c?.enabled ? c : null))
    .catch(() => prev);
  cached = next;
  void next.then((c) => listeners.forEach((l) => l(c)));
  return next;
}

/** Test seam. */
export function resetVoiceLiveConfigCache(): void {
  cached = null;
}

/** The whole config (null = no voice), kept fresh by `refreshVoiceLiveConfig`. */
export function useVoiceLiveConfig(active = true): VoiceLiveConfig | null {
  const [config, setConfig] = useState<VoiceLiveConfig | null>(null);
  useEffect(() => {
    if (!active) {
      setConfig(null);
      return;
    }
    let alive = true;
    const listener = (c: VoiceLiveConfig | null) => {
      if (alive) setConfig(c);
    };
    listeners.add(listener);
    void loadVoiceLiveConfig().then(listener);
    return () => {
      alive = false;
      listeners.delete(listener);
    };
  }, [active]);
  return config;
}

export function useVoiceLiveEnabled(active = true): boolean {
  return useVoiceLiveConfig(active) !== null;
}
