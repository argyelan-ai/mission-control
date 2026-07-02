import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { VaultGraphResponse } from "@/lib/types";

/**
 * useVaultGraph — TanStack Query wrapper for GET /api/v1/vault/graph.
 *
 * Graph data changes infrequently (only when new vault notes land), so we
 * cache aggressively at 60 s staleTime.  Live updates come via useVaultStream
 * (WebSocket) which invalidates this query when a "modified" event arrives.
 *
 * Used by VaultGraphPage (T10).
 */
export function useVaultGraph(opts?: { cluster?: boolean; heatmap?: string; similarity_edges?: boolean }) {
  // similarity_edges=true (backend default): adds Qdrant "ghost" edges so
  // notes without explicit wikilinks still cluster by semantic similarity.
  // Without them many isolated notes drift to the canvas edge unconnected
  // (operator feedback 2026-05-19). Cold build is 1-4 s — accept that latency
  // in exchange for a graph that actually reads.
  const similarity_edges = opts?.similarity_edges ?? true;
  return useQuery<VaultGraphResponse>({
    queryKey: ["vault", "graph", opts?.cluster, opts?.heatmap, similarity_edges],
    queryFn: () => api.vault.graph({ ...opts, similarity_edges }),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}
