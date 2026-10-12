"use client";

/**
 * Live tool output, delivered to exactly the rows that need it.
 *
 * Why a store and not a plain context value: a running command sends a new
 * snapshot about twice a second, and a long chat holds hundreds of tool
 * rows. With the map as a context value every row would re-render on every
 * snapshot. Here a row subscribes to ITS tool call only
 * (`useSyncExternalStore` with a per-id selector), so one snapshot re-renders
 * one row — and the group it sits in only when a tool starts or stops
 * streaming.
 */
import { createContext, useContext, useSyncExternalStore } from "react";
import type { ToolOutputEvent } from "@/lib/chatTypes";

type Outputs = Record<string, ToolOutputEvent>;

export interface ToolOutputStore {
  get(): Outputs;
  set(next: Outputs): void;
  subscribe(listener: () => void): () => void;
}

const EMPTY: Outputs = {};

export function createToolOutputStore(): ToolOutputStore {
  let current: Outputs = EMPTY;
  const listeners = new Set<() => void>();
  return {
    get: () => current,
    set(next) {
      if (next === current) return;
      current = next;
      listeners.forEach((l) => l());
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** Rows outside a chat (AgentCard, tests) get a store that never streams. */
const IDLE_STORE = createToolOutputStore();

export const ToolOutputContext = createContext<ToolOutputStore>(IDLE_STORE);

/** The newest live output of one tool call, or `null`. */
export function useToolOutput(toolUseId: string | null): ToolOutputEvent | null {
  const store = useContext(ToolOutputContext);
  return useSyncExternalStore(
    store.subscribe,
    () => (toolUseId ? (store.get()[toolUseId] ?? null) : null),
    () => null,
  );
}

/** Which of these tool calls are streaming right now — as one stable string,
 *  so the caller re-renders only when the SET changes, not on every line. */
export function useStreamingToolIds(toolUseIds: (string | null)[]): Set<string> {
  const store = useContext(ToolOutputContext);
  const key = useSyncExternalStore(
    store.subscribe,
    () => {
      const outputs = store.get();
      return toolUseIds.filter((id): id is string => !!id && id in outputs).join("\n");
    },
    () => "",
  );
  return new Set(key ? key.split("\n") : []);
}
