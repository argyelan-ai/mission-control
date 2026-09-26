/**
 * Maps the watchdog component of /system/status to what the System Health
 * row shows. The backend reports "running" (source "local" = API process,
 * "worker" = the separate worker container), "stale" (heartbeat too old)
 * or "stopped" (no heartbeat) — see backend app/services/service_heartbeat.py.
 */
import type { SystemStatus } from "@/lib/types";

type WatchdogComponent = SystemStatus["components"]["watchdog"];

export interface WatchdogView {
  /** status string for ServiceDot */
  dot: "ok" | "warning" | "down" | "unknown";
  /** i18n key in the "home" namespace */
  key: "checks" | "watchdogRunningWorker" | "watchdogStale" | "watchdogStopped" | "watchdogUnknown";
  values?: { count: number };
  lastSeen?: string | null;
  isError: boolean;
}

export function watchdogView(wd: WatchdogComponent | undefined): WatchdogView {
  switch (wd?.status) {
    case "running":
      return {
        dot: "ok",
        key: wd.source === "worker" ? "watchdogRunningWorker" : "checks",
        values: { count: wd.checks_total ?? 0 },
        isError: false,
      };
    case "stale":
      return { dot: "warning", key: "watchdogStale", lastSeen: wd.last_seen ?? null, isError: false };
    case "stopped":
      return { dot: "down", key: "watchdogStopped", isError: true };
    default:
      return { dot: "unknown", key: "watchdogUnknown", isError: false };
  }
}
