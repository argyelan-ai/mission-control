"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "@/lib/api";
import type { VaultNote, VaultNoteType, VaultNotesListResponse } from "@/lib/types";
import type { VaultScope } from "./useVaultSearch";

/** Types that belong to each scope. Used for client-side filtering. */
const SCOPE_TYPES: Record<VaultScope, VaultNoteType[]> = {
  episodic: ["journal", "weekly_review"],
  semantic: ["knowledge", "reference"],
  agents: ["lesson"],
};

const PAGE_SIZE = 50;

interface UseVaultListParams {
  scope?: VaultScope;
  agent?: string;
  type?: VaultNoteType;
}

/**
 * Paginated vault note list (used when there's no search query).
 * Scope → type mapping is applied client-side after the backend returns results
 * so that multi-type scopes ("episodic" = journal | weekly_review) work without
 * requiring a backend multi-type filter API.
 *
 * Note: the backend /vault/notes supports a single `type` param.  When scope
 * selects multiple types we fetch without a type filter and filter client-side.
 */
export function useVaultList({ scope, agent, type }: UseVaultListParams = {}) {
  // If caller passed a single explicit type, send it to the backend.
  // If scope maps to exactly one type (agents → lesson), also send it.
  const backendType = useMemo<string | undefined>(() => {
    if (type) return type;
    if (scope && SCOPE_TYPES[scope].length === 1) return SCOPE_TYPES[scope][0];
    return undefined;
  }, [type, scope]);

  const result = useInfiniteQuery<VaultNotesListResponse>({
    queryKey: ["vault", "list", agent, backendType],
    queryFn: async ({ pageParam = 0 }) => {
      const offset = (pageParam as number) * PAGE_SIZE;
      const page = await api.vault.list({
        ...(agent ? { agent } : {}),
        ...(backendType ? { type: backendType } : {}),
        limit: PAGE_SIZE,
        offset,
      });
      return { count: page.count, notes: page.notes, agents: page.agents };
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const fetched = allPages.reduce((sum, p) => sum + p.notes.length, 0);
      return fetched < lastPage.count ? allPages.length : undefined;
    },
    staleTime: 30_000,
  });

  // Flatten pages and apply client-side scope filter (multi-type scopes).
  const notes = useMemo<VaultNote[]>(() => {
    const flat = result.data?.pages.flatMap((p) => p.notes) ?? [];
    if (scope && !type) {
      const allowedTypes = SCOPE_TYPES[scope];
      return flat.filter((n) => allowedTypes.includes(n.type as VaultNoteType));
    }
    return flat;
  }, [result.data, scope, type]);

  const totalCount = result.data?.pages[0]?.count ?? 0;

  // The server's agent facet covers the whole vault (not just the loaded
  // pages): the header counts it and the agent filter offers it (round-4
  // user test t4 SHOULD-1 — "4 agents" in a vault with 19). A backend from
  // before the facet sends none: then the loaded pages are all we know.
  const agents = useMemo<string[]>(() => {
    const facet = result.data?.pages[0]?.agents;
    if (facet) return facet.map((a) => a.agent);
    const slugs = new Set<string>();
    for (const p of result.data?.pages ?? []) for (const n of p.notes) if (n.agent) slugs.add(n.agent);
    return Array.from(slugs).sort();
  }, [result.data]);

  return {
    notes,
    totalCount,
    agents,
    isLoading: result.isLoading,
    isError: result.isError,
    error: result.error,
    fetchNextPage: result.fetchNextPage,
    hasNextPage: result.hasNextPage,
    isFetchingNextPage: result.isFetchingNextPage,
  };
}

export { SCOPE_TYPES };
