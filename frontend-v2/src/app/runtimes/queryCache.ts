"use client";

/**
 * Last-known-data cache for the runtimes page.
 *
 * GET /runtimes live-enriches every runtime on request (~6 s against the real
 * fleet), so a cold page load stares at a spinner. This module persists the
 * last successful response per query to localStorage and feeds it back as
 * TanStack `placeholderData`: the page paints instantly with the previous
 * fleet state (marked stale by TanStack) while the fresh fetch replaces it.
 *
 * Deliberately NOT a global query persister: only the keys this page owns,
 * no new dependencies, and cached payloads never outlive SCHEMA_VERSION.
 */

const PREFIX = "mc-runtimes-cache:";
// Bump when a cached payload's shape changes incompatibly.
const SCHEMA_VERSION = 1;
const MAX_AGE_MS = 24 * 60 * 60 * 1000; // stale-paint horizon: 1 day

interface Envelope<T> {
  v: number;
  at: number;
  data: T;
}

export function loadCached<T>(key: string): T | undefined {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (!raw) return undefined;
    const env = JSON.parse(raw) as Envelope<T>;
    if (env.v !== SCHEMA_VERSION) return undefined;
    if (Date.now() - env.at > MAX_AGE_MS) return undefined;
    return env.data;
  } catch {
    return undefined;
  }
}

export function saveCached<T>(key: string, data: T): void {
  try {
    const env: Envelope<T> = { v: SCHEMA_VERSION, at: Date.now(), data };
    localStorage.setItem(PREFIX + key, JSON.stringify(env));
  } catch {
    // Quota/serialization failures must never break the page — cache is best-effort.
  }
}

/** queryFn wrapper: serve cache as placeholder via loadCached, persist fresh results. */
export async function fetchWithCache<T>(key: string, fetcher: () => Promise<T>): Promise<T> {
  const data = await fetcher();
  saveCached(key, data);
  return data;
}
