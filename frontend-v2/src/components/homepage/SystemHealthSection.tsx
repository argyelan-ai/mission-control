"use client";

/**
 * SystemHealthSection — Flat, serious, no glass, no glow.
 */

import { useQuery } from "@tanstack/react-query";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";
import type { ActivityEvent, MetricsSnapshot, MetricsHistoryResponse, SystemStatus } from "@/lib/types";
import { timeAgo } from "@/lib/utils";
import { C, latencyColor } from "./colors";
import { SectionHeading, ServiceDot, SparklineChart } from "./primitives";

function SystemActivityFeed() {
  const { activeBoardId } = useAppStore();
  const { data: events = [] } = useQuery({
    queryKey: ["activity", activeBoardId, "compact"],
    queryFn: () => api.activity.list({ board_id: activeBoardId!, limit: 15 }),
    enabled: !!activeBoardId,
    refetchInterval: 15_000,
  });

  if (events.length === 0) {
    return (
      <div className="flex items-center justify-center h-full">
        <span className="text-[11px]" style={{ color: C.textMuted }}>Keine Aktivitaet</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-0.5">
      {events.slice(0, 15).map((event: ActivityEvent) => (
        <div key={event.id} className="flex items-start gap-2 py-1">
          <span
            className="w-1.5 h-1.5 rounded-full shrink-0 mt-1"
            style={{
              backgroundColor:
                event.event_type?.includes("fail") || event.event_type?.includes("error")
                  ? C.error
                  : event.event_type?.includes("done") || event.event_type?.includes("complete")
                    ? C.online
                    : C.textDim,
            }}
          />
          <div className="min-w-0 flex-1">
            <div className="text-[11px] truncate" style={{ color: C.textSecondary }}>{event.title}</div>
            <div className="text-[10px]" style={{ color: C.textMuted }}>{timeAgo(event.created_at)}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

interface SystemHealthSectionProps {
  status?: SystemStatus;
  loading: boolean;
  onOpenActivity: () => void;
}

export function SystemHealthSection({ status, loading, onOpenActivity }: SystemHealthSectionProps) {
  const { data: historyData } = useQuery({
    queryKey: ["system", "metrics-history"],
    queryFn: () => api.system.metricsHistory(),
    refetchInterval: 60_000,
  });

  if (loading || !status) {
    return (
      <div className="p-4 rounded-lg" style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}>
        <SectionHeading title="System Health" />
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-8 rounded-sm animate-pulse" style={{ backgroundColor: C.bgElevated }} />
          ))}
        </div>
      </div>
    );
  }

  // Phase 29 sunset: `gateway` component dropped from /system/status response.
  // Optional-chain defensive access until Phase 31 frontend rebuild finishes.
  const { database, redis, watchdog: wd } = status.components;
  const res = status.resources;

  const cpuPct = res?.cpu_pct ?? 0;
  const memUsed = res?.memory_used_gb ?? 0;
  const memTotal = res?.memory_total_gb ?? 0;
  const memPct = res?.memory_pct ?? 0;
  const diskUsed = res?.disk_used_gb ?? 0;
  const diskTotal = res?.disk_total_gb ?? 0;
  const diskPct = res?.disk_pct ?? 0;

  const dbDetail = database.latency_ms !== undefined ? `${database.latency_ms.toFixed(1)}ms` : database.error ?? database.status;
  const redisDetail = redis.latency_ms !== undefined ? `${redis.latency_ms.toFixed(1)}ms` : redis.error ?? redis.status;
  const watchdogDetail = wd ? (wd.status === "running" ? `${wd.checks_total ?? 0} checks` : wd.status) : "unknown";

  const wdStatus = wd?.status === "running" ? "ok" : wd?.status ?? "unknown";
  const hasError = database.status === "error" || redis.status === "error" || wdStatus === "error";

  const cpuHistory = (historyData as MetricsHistoryResponse | undefined)?.snapshots?.map((s: MetricsSnapshot) => s.cpu_pct ?? 0) ?? [];
  const memHistory = (historyData as MetricsHistoryResponse | undefined)?.snapshots?.map((s: MetricsSnapshot) => s.memory_pct ?? 0) ?? [];
  const diskHistory = (historyData as MetricsHistoryResponse | undefined)?.snapshots?.map((s: MetricsSnapshot) => s.disk_pct ?? 0) ?? [];

  return (
    <div className="p-4 rounded-lg" style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}>
      <div className="flex items-center gap-4 mb-3 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: hasError ? C.error : C.online }} />
          <span className="text-[10px] font-semibold uppercase tracking-[0.12em]" style={{ color: C.textMuted }}>System Health</span>
        </div>
        <ServiceDot label="DB" status={database.status} detail={dbDetail} detailColor={database.latency_ms !== undefined ? latencyColor(database.latency_ms) : undefined} />
        <ServiceDot label="Redis" status={redis.status} detail={redisDetail} detailColor={redis.latency_ms !== undefined ? latencyColor(redis.latency_ms) : undefined} />
        <ServiceDot label="Watchdog" status={wdStatus} detail={watchdogDetail} />
        <div className="flex items-center gap-2 ml-auto">
          <span className="text-[10px] font-semibold uppercase tracking-[0.08em]" style={{ color: C.textMuted }}>Events</span>
          <button
            onClick={onOpenActivity}
            aria-label="Alle Aktivitäten anzeigen"
            className="inline-flex items-center min-h-touch px-2 -my-2 text-[10px] hover:opacity-70 transition-opacity cursor-pointer"
            style={{ color: C.accent, background: "none", border: "none" }}
          >
            Alle <span aria-hidden="true">→</span>
          </button>
        </div>
      </div>

      <div className="flex flex-col sm:flex-row gap-3">
        <div className="flex-[3] grid grid-cols-3 gap-3 min-w-0">
          <SparklineChart data={cpuHistory.length > 1 ? cpuHistory : [cpuPct * 0.8, cpuPct * 0.9, cpuPct]} color={C.chart.cpu} label="CPU" value={`${cpuPct.toFixed(1)}%`} height={40} />
          <SparklineChart data={memHistory.length > 1 ? memHistory : [memPct * 0.9, memPct * 0.95, memPct]} color={C.chart.ram} label="RAM" value={`${memUsed}/${memTotal} GB`} height={40} />
          <SparklineChart data={diskHistory.length > 1 ? diskHistory : [diskPct * 0.98, diskPct * 0.99, diskPct]} color={C.chart.disk} label="Disk" value={`${diskUsed}/${diskTotal} GB`} height={40} />
        </div>
        <div
          className="flex-[1] min-w-0 overflow-y-auto border-t pt-3 sm:border-t-0 sm:pt-0 sm:border-l sm:pl-3"
          style={{ borderColor: C.border, maxHeight: 80 }}
          tabIndex={0}
          role="region"
          aria-label="Aktivitäts-Verlauf"
        >
          <SystemActivityFeed />
        </div>
      </div>
    </div>
  );
}
