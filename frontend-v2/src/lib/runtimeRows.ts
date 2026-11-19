import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Runtime } from "@/lib/types";

/** Query key of the unprobed runtime rows. Under the `["runtimes"]` prefix on
 *  purpose: every `invalidateQueries({ queryKey: ["runtimes"] })` after a
 *  switch, start or edit refreshes these rows too. */
export const RUNTIME_ROWS_QUERY_KEY = ["runtimes", "rows"] as const;

/** The runtime rows without live probes (`GET /runtimes?probe=false`).
 *
 *  Round-2 user test (23.10.2026): the agent page waited 5.1 s for the
 *  live-probed list — one sleeping box held it up — and said "no runtime"
 *  meanwhile. The model, box and window of a row need no probe; the DB
 *  answers in milliseconds. Never decide on `state` from these rows: it is
 *  "unknown" by design (see `isRuntimeUnprobed`). */
export function useRuntimeRows(enabled = true) {
  return useQuery({
    queryKey: RUNTIME_ROWS_QUERY_KEY,
    queryFn: () => api.runtimes.listRows(),
    staleTime: 60_000,
    enabled,
  });
}

/** A row from the unprobed list (or an equally unasked one) — its state is
 *  not known yet, which is different from "asked, no answer" (probe_timeout). */
export function isRuntimeUnprobed(rt: Pick<Runtime, "container_status">): boolean {
  return rt.container_status === "not_probed";
}
