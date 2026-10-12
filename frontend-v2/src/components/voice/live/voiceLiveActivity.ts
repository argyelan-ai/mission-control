"use client";

/**
 * Which chats have a live voice call right now (voice PR 2). The chat header
 * reads it to hide the Jarvis mic while a call runs in that chat — two mic
 * buttons with two different meanings on one screen confused the operator.
 * Jarvis itself is untouched; only its button in that header steps aside.
 */

import { useSyncExternalStore } from "react";

const active = new Set<string>();
const listeners = new Set<() => void>();

export function setVoiceLiveActive(agentId: string, on: boolean): void {
  if (!agentId || active.has(agentId) === on) return;
  if (on) active.add(agentId);
  else active.delete(agentId);
  listeners.forEach((l) => l());
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

export function useVoiceLiveActive(agentId: string | null | undefined): boolean {
  return useSyncExternalStore(
    subscribe,
    () => (agentId ? active.has(agentId) : false),
    () => false,
  );
}
