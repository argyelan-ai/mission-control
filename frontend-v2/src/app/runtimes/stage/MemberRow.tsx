"use client";

/**
 * MemberRow — Zone 3 „Mitglieder" (Spec §2). Je Box: Punkt + Name + Rolle,
 * GPU-/RAM-Balken (nur `transform: scaleX`, kein Layout-Thrash), der
 * bestehende kompakte Modus-Vierer (`DeviceModeStrip`, wiederverwendet statt
 * neu gebaut) und die Temperatur rechts.
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { C, STATUS } from "@/lib/colors";
import type { Device, Host, HostMetrics } from "@/lib/types";
import { RoleChip } from "../RoleField";
import { DeviceModeStrip } from "../DeviceControl";

function Bar({ pct, ram = false }: { pct: number; ram?: boolean }) {
  return (
    <span className="block h-[3px] relative overflow-hidden flex-1 min-w-[36px]" style={{ background: C.bgElevated }}>
      <span
        className="absolute inset-0 block"
        style={{
          background: ram ? C.textMuted : C.accent,
          transform: `scaleX(${Math.max(0, Math.min(1, pct / 100))})`,
          transformOrigin: "left",
        }}
      />
    </span>
  );
}

function Meter({ label, value, pct, ram = false }: { label: string; value: string; pct: number; ram?: boolean }) {
  return (
    <div className="grid items-center gap-2 font-mono text-[11px] tabular-nums" style={{ gridTemplateColumns: "30px 1fr auto", color: C.textMuted }}>
      <span>{label}</span>
      <Bar pct={pct} ram={ram} />
      <span style={{ color: C.textSecondary }} className="whitespace-nowrap">{value}</span>
    </div>
  );
}

export function MemberRow({
  name,
  role,
  online,
  metrics,
  device,
  canControlDevice,
}: {
  name: string;
  role: "head" | "worker" | null;
  online: boolean;
  metrics?: HostMetrics;
  device?: Device;
  canControlDevice: boolean;
}) {
  const gpuPct = metrics?.gpu_util_pct ?? 0;
  const hasVram = metrics?.vram_total_mb != null;
  const memUsed = hasVram ? metrics?.vram_used_mb : metrics?.ram_used_mb;
  const memTotal = hasVram ? metrics?.vram_total_mb : metrics?.ram_total_mb;
  const ramPct = memTotal && memUsed != null ? (memUsed / memTotal) * 100 : 0;
  const gpuValue = metrics?.gpu_util_pct != null ? `${metrics.gpu_util_pct} %` : "—";
  const ramValue =
    memUsed != null && memTotal != null ? `${Math.round(memUsed / 1024)}/${Math.round(memTotal / 1024)} GB` : "—";
  const tempValue = metrics?.gpu_temp_c != null ? `${metrics.gpu_temp_c} °C` : "—";

  return (
    <div className="p-3.5 flex flex-col gap-2 min-w-0" data-testid="member-row">
      <div className="flex items-center gap-2 text-sm font-medium" style={{ color: C.textPrimary }}>
        <span
          className="w-2 h-2 rounded-full shrink-0"
          style={{ background: online ? STATUS.online : C.textDim }}
        />
        {name}
        {role && <RoleChip role={role} />}
      </div>
      <div className="flex flex-col gap-1">
        <Meter label="GPU" value={gpuValue} pct={gpuPct} />
        <Meter label="RAM" value={ramValue} pct={ramPct} ram />
      </div>
      <div className="flex items-center justify-between gap-2 mt-0.5">
        {device ? (
          <div className="flex-1 min-w-0">
            <DeviceModeStrip device={device} canControl={canControlDevice} />
          </div>
        ) : (
          <span />
        )}
        <span className="font-mono shrink-0" style={{ fontSize: "11px", color: C.textMuted }}>{tempValue}</span>
      </div>
    </div>
  );
}

/**
 * MemberRowContainer — fetches this member's own host metrics (one useQuery
 * per mounted instance, keyed by host.id in the caller's .map — never a
 * variable-length hook array in one component). Mirrors SlotStage's
 * TelemetryColumn pattern: one query per box, shared across mounts via the
 * TanStack cache.
 */
export function MemberRowContainer({
  host,
  role,
  device,
  canControlDevice,
}: {
  host: Host;
  role: "head" | "worker" | null;
  device?: Device;
  canControlDevice: boolean;
}) {
  const { data: metrics } = useQuery({
    queryKey: ["hosts", host.id, "metrics"],
    queryFn: () => api.hosts.metrics(host.id),
    refetchInterval: 5_000,
  });
  return (
    <MemberRow
      name={host.display_name}
      role={role}
      online={metrics?.reachable === true}
      metrics={metrics}
      device={device}
      canControlDevice={canControlDevice}
    />
  );
}
