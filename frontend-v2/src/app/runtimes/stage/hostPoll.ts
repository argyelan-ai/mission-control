import type { QueryClient } from "@tanstack/react-query";
import type { HostMetrics } from "@/lib/types";

/**
 * How often the stage asks a box for its metrics and pulse. An answering box
 * every 5 s (live telemetry); a sleeping or unreachable one has nothing new
 * to say, so once a minute is enough to notice it came back — a wake from
 * MC refreshes at once anyway (WakeBoxButton invalidates the metrics).
 * Test team 21.11.2026: with one box asleep, /runtimes made 85 requests a
 * minute, most of them metrics and pulse of that box.
 */
export const HOST_POLL_AWAKE_MS = 5_000;
export const HOST_POLL_ASLEEP_MS = 60_000;

export function hostPollMs(metrics: HostMetrics | undefined): number {
  if (!metrics) return HOST_POLL_AWAKE_MS;
  return metrics.reachable === false || metrics.status === "asleep" ? HOST_POLL_ASLEEP_MS : HOST_POLL_AWAKE_MS;
}

/** `refetchInterval` for a `["hosts", id, "metrics"]` query. */
export function metricsPoll(query: { state: { data?: HostMetrics } }): number {
  return hostPollMs(query.state.data);
}

/** `refetchInterval` for a box's pulse: follows that box's metrics in the cache. */
export function pulsePoll(qc: QueryClient, hostId: string | undefined): () => number {
  return () => hostPollMs(hostId ? qc.getQueryData<HostMetrics>(["hosts", hostId, "metrics"]) : undefined);
}
