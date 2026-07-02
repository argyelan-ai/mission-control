"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api } from "@/lib/api";
import type { VaultNoteDetail } from "@/lib/types";

/**
 * Fetches a single vault note by path.
 * Fires vault.trackView(path) once on mount (fire-and-forget).
 * Uses TanStack Query so repeated opens of the same note are instant (cache hit).
 */
export function useVaultNote(path: string | null) {
  const trackedRef = useRef<string | null>(null);

  const result = useQuery<VaultNoteDetail>({
    queryKey: ["vault", "note", path],
    queryFn: () => api.vault.get(path!),
    enabled: path != null,
    staleTime: 60_000,
  });

  // Fire trackView once per unique path (not on every render).
  useEffect(() => {
    if (path && trackedRef.current !== path) {
      trackedRef.current = path;
      api.vault.trackView(path).catch(() => {
        // fire-and-forget — ignore errors (heatmap only)
      });
    }
  }, [path]);

  return {
    data: result.data,
    isLoading: result.isLoading,
    isError: result.isError,
    error: result.error,
  };
}

/**
 * Lightweight hook for wikilink hover preview.
 * Only fetches when `path` is non-null (on hover).
 * Results are cached by TanStack Query so repeated hovers are instant.
 */
export function useVaultNotePreview(path: string | null) {
  return useQuery<VaultNoteDetail>({
    queryKey: ["vault", "note", path],
    queryFn: () => api.vault.get(path!),
    enabled: path != null,
    staleTime: 120_000,
  });
}
