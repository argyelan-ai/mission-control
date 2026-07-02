"use client";

/**
 * ScheduleHeader — page header for /schedule with KPI row + "Neuer Job" CTA.
 */

import { useMemo } from "react";
import { Plus, Activity, Zap, Clock, AlertTriangle } from "lucide-react";
import { motion } from "framer-motion";
import { KPICard } from "@/components/shared/KPICard";
import type { ScheduledJob } from "@/lib/types";
import { C } from "@/lib/colors";

interface ScheduleHeaderProps {
  jobs: ScheduledJob[];
  onNewJob: () => void;
}

function formatNextRun(iso: string | null): string {
  if (!iso) return "—";
  const target = new Date(iso);
  const now = new Date();
  const diffMs = target.getTime() - now.getTime();
  if (diffMs <= 0) return "jetzt";

  const diffMin = Math.floor(diffMs / 60000);
  const sameDay =
    target.getFullYear() === now.getFullYear() &&
    target.getMonth() === now.getMonth() &&
    target.getDate() === now.getDate();
  const hh = String(target.getHours()).padStart(2, "0");
  const mm = String(target.getMinutes()).padStart(2, "0");

  if (diffMin < 60) return `in ${diffMin}m`;
  if (sameDay) return `heute ${hh}:${mm}`;

  const tomorrow = new Date(now);
  tomorrow.setDate(tomorrow.getDate() + 1);
  const isTomorrow =
    target.getFullYear() === tomorrow.getFullYear() &&
    target.getMonth() === tomorrow.getMonth() &&
    target.getDate() === tomorrow.getDate();
  if (isTomorrow) return `morgen ${hh}:${mm}`;

  const diffH = Math.round(diffMin / 60);
  if (diffH < 48) return `in ${diffH}h`;

  const dd = String(target.getDate()).padStart(2, "0");
  const mo = String(target.getMonth() + 1).padStart(2, "0");
  return `${dd}.${mo} ${hh}:${mm}`;
}

export function ScheduleHeader({ jobs, onNewJob }: ScheduleHeaderProps) {
  const stats = useMemo(() => {
    const enabled = jobs.filter((j) => j.enabled);
    const running = jobs.filter(
      (j) => (j.last_run_status as string | null) === "running",
    );

    let nextRun: string | null = null;
    for (const j of enabled) {
      if (!j.next_run_at) continue;
      if (!nextRun || new Date(j.next_run_at) < new Date(nextRun)) {
        nextRun = j.next_run_at;
      }
    }

    const sevenDaysAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
    const failed7d = jobs.filter((j) => {
      if (j.last_run_status !== "failed") return false;
      if (!j.last_run_at) return false;
      return new Date(j.last_run_at).getTime() >= sevenDaysAgo;
    });

    return {
      enabled: enabled.length,
      running: running.length,
      nextRun: formatNextRun(nextRun),
      failed: failed7d.length,
    };
  }, [jobs]);

  return (
    <motion.div
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex flex-col gap-4"
    >
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <p className="text-sm" style={{ color: C.textMuted }}>
          Geplante Jobs — Cron, Intervalle, eigene Wochentage.
        </p>
        <button
          type="button"
          onClick={onNewJob}
          className="flex items-center gap-1.5 rounded-lg px-3.5 py-2 text-sm font-medium transition active:scale-[0.98]"
          style={{
            background: C.accent,
            color: C.bgBase,
          }}
        >
          <Plus size={16} />
          Neuer Job
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <KPICard
          label="Aktive Jobs"
          value={stats.enabled}
          icon={Activity}
        />
        <KPICard
          label="Laeuft gerade"
          value={stats.running}
          icon={Zap}
          trend={stats.running > 0 ? "up" : undefined}
        />
        <KPICard label="Naechster Lauf" value={stats.nextRun} icon={Clock} />
        <KPICard
          label="Fehler (7d)"
          value={stats.failed}
          icon={AlertTriangle}
          trend={stats.failed > 0 ? "down" : undefined}
        />
      </div>
    </motion.div>
  );
}
