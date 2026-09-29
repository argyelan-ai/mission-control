"use client";

/**
 * Is the head launcher switched on? One cheap probe (GET /heads/occupancy),
 * shared by every heads query and cached for 5 minutes. While the launcher
 * is off (404 heads_disabled) the task detail and /runtimes ask nothing
 * else — no 404 per task opening.
 *
 * Returns true / false, or null while the probe is still running.
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { isHeadsDisabled } from "@/lib/heads";

export const HEADS_ENABLED_KEY = ["heads", "enabled"] as const;

export function useHeadsEnabled(): boolean | null {
  const q = useQuery({
    queryKey: HEADS_ENABLED_KEY,
    queryFn: async () => {
      try {
        await api.heads.occupancy();
        return true;
      } catch (err) {
        if (isHeadsDisabled(err)) return false;
        // any other error (network, 5xx): don't hide the launcher for good
        return true;
      }
    },
    retry: false,
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000,
  });
  return q.data ?? null;
}
