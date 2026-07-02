"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState, useEffect } from "react";
import { api } from "@/lib/api";
import type { VaultNoteType, VaultSearchResponse } from "@/lib/types";

export type VaultScope = "episodic" | "semantic" | "agents";

interface UseVaultSearchParams {
  q: string;
  scope?: VaultScope;
  agent?: string;
  type?: VaultNoteType;
}

/** 300ms debounce hook. */
function useDebounced<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

/**
 * Searches vault notes with a 300ms debounce on `q`.
 * When `q` is empty returns { data: null, isLoading: false } so the caller
 * falls back to useVaultList.
 */
export function useVaultSearch({ q, scope: _scope, agent, type }: UseVaultSearchParams) {
  const debouncedQ = useDebounced(q.trim(), 300);
  const enabled = debouncedQ.length > 0;

  const result = useQuery<VaultSearchResponse>({
    queryKey: ["vault", "search", debouncedQ, agent, type],
    queryFn: () =>
      api.vault.search({
        q: debouncedQ,
        ...(agent ? { agent } : {}),
        ...(type ? { type } : {}),
        limit: 100,
      }),
    enabled,
    staleTime: 30_000,
  });

  return useMemo(
    () => ({
      data: enabled ? result.data : null,
      isLoading: enabled && result.isLoading,
      isError: result.isError,
      error: result.error,
      debouncedQ,
    }),
    [enabled, result.data, result.isLoading, result.isError, result.error, debouncedQ]
  );
}
