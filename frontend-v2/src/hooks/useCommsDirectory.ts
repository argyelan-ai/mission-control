import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * The directory of who can be talked to across harnesses (ADR-100 §4) —
 * `GET /api/v1/comms/directory`, the one presence source's own rows.
 * Operator only; a viewer gets a 403 and the header then shows nothing.
 */
export function useCommsDirectory(enabled: boolean) {
  return useQuery({
    queryKey: ["comms-directory"],
    queryFn: () => api.comms.directory(),
    enabled,
    // "alive" lapses after 90 s without a sign of life — refresh well inside.
    refetchInterval: 30_000,
    retry: false,
  });
}
