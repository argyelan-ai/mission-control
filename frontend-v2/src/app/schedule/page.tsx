"use client";

import { useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Activity, AlertTriangle, Clock, Heart } from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { GlassCard } from "@/components/shared/GlassCard";
import { JobsTable } from "@/components/schedule/JobsTable";
import { JobModal } from "@/components/schedule/JobModal";
import { ScheduleHeader } from "@/components/schedule/ScheduleHeader";
import { ScheduleHeatmap } from "@/components/schedule/ScheduleHeatmap";
import { api, sseUrls } from "@/lib/api";
import { useSSE } from "@/lib/sse";
import { useAppStore } from "@/lib/store";
import type { Agent, Board, ScheduledJob } from "@/lib/types";
import { cn, timeAgo } from "@/lib/utils";
import { C } from "@/lib/colors";

type Tab = "overview" | "day" | "week" | "health";

// ── Helpers ───────────────────────────────────────────────────────────────────

function chipColors(job: ScheduledJob, runningJobs: Set<string>) {
  if (!job.enabled)
    return {
      bg: "rgba(255,255,255,0.03)",
      border: "rgba(255,255,255,0.07)",
      text: C.textMuted,
    };
  if (runningJobs.has(job.id))
    return {
      bg: C.accentSubtle,
      border: C.borderAccent,
      text: C.accent,
    };
  if (job.last_run_status === "failed")
    return {
      bg: `${C.error}14`,
      border: `${C.error}59`,
      text: C.error,
    };
  return {
    bg: `${C.online}0F`,
    border: `${C.online}4D`,
    text: C.online,
  };
}

// ── Day Timeline ──────────────────────────────────────────────────────────────

const HOURS = [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23];
const HOUR_HEIGHT = 64;

function DayTimeline({
  jobs,
  runningJobs,
  onJobSelect,
}: {
  jobs: ScheduledJob[];
  runningJobs: Set<string>;
  onJobSelect: (id: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const timedJobs = jobs.filter(
    (j) =>
      (j.schedule_type === "daily" || j.schedule_type === "weekdays") &&
      j.schedule_time
  );
  const now = new Date();
  const nowTop =
    now.getHours() >= 6 && now.getHours() <= 23
      ? (now.getHours() - 6) * HOUR_HEIGHT +
        (now.getMinutes() / 60) * HOUR_HEIGHT
      : null;

  return (
    <div className="flex flex-col h-full">
      <div
        className="px-6 py-3 border-b flex-shrink-0"
        style={{ borderColor: C.border }}
      >
        <p className="text-sm" style={{ color: C.textSecondary }}>
          Today —{" "}
          {new Date().toLocaleDateString("en-GB", {
            weekday: "long",
            day: "numeric",
            month: "long",
          })}
        </p>
      </div>
      <div ref={containerRef} className="flex-1 overflow-y-auto">
        <div className="relative" style={{ height: HOURS.length * HOUR_HEIGHT }}>
          {HOURS.map((hour, i) => (
            <div
              key={hour}
              className="absolute left-0 right-0 flex items-start"
              style={{ top: i * HOUR_HEIGHT }}
            >
              <span
                className="w-14 text-right pr-3 text-[10px] pt-0.5 flex-shrink-0"
                style={{ color: C.textMuted }}
              >
                {String(hour).padStart(2, "0")}:00
              </span>
              <div
                className="flex-1 border-t"
                style={{ borderColor: C.borderSubtle }}
              />
            </div>
          ))}

          {nowTop !== null && (
            <div
              className="absolute left-14 right-0 flex items-center z-10 pointer-events-none"
              style={{ top: nowTop }}
            >
              <div
                className="w-2 h-2 rounded-full flex-shrink-0 -ml-1"
                style={{ backgroundColor: C.accent }}
              />
              <div
                className="flex-1 border-t"
                style={{ borderColor: C.accent }}
              />
            </div>
          )}

          {timedJobs.map((job) => {
            const [h, m] = (job.schedule_time ?? "08:00").split(":").map(Number);
            if (h < 6 || h > 23) return null;
            const top = (h - 6) * HOUR_HEIGHT + (m / 60) * HOUR_HEIGHT;
            const c = chipColors(job, runningJobs);
            return (
              <motion.button
                key={job.id}
                initial={{ opacity: 0, x: -12 }}
                animate={{ opacity: 1, x: 0 }}
                onClick={() => onJobSelect(job.id)}
                className="absolute left-16 right-4 px-2.5 py-1.5 rounded-lg border text-left transition-all hover:scale-[1.01] hover:z-10 cursor-pointer"
                style={{
                  top,
                  height: 30,
                  background: c.bg,
                  borderColor: c.border,
                }}
              >
                <div className="flex items-center gap-1.5 overflow-hidden">
                  <span
                    className="text-[10px] font-medium flex-shrink-0"
                    style={{ color: c.text }}
                  >
                    {job.schedule_time}
                  </span>
                  <span
                    className="text-[10px] truncate"
                    style={{ color: c.text, opacity: 0.8 }}
                  >
                    {job.name}
                  </span>
                </div>
              </motion.button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── Week Calendar ─────────────────────────────────────────────────────────────

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function jobRunsOnDay(job: ScheduledJob, dayIndex: number): boolean {
  if (!job.enabled) return false;
  if (job.schedule_type === "interval" || job.schedule_type === "daily")
    return true;
  if (job.schedule_type === "weekdays") return dayIndex < 5;
  if (job.schedule_type === "weekly_custom" && job.schedule_weekdays)
    return job.schedule_weekdays.includes(dayIndex);
  return false;
}

function WeekCalendar({
  jobs,
  runningJobs,
  onJobSelect,
}: {
  jobs: ScheduledJob[];
  runningJobs: Set<string>;
  onJobSelect: (id: string) => void;
}) {
  const today = new Date();
  const todayDow = (today.getDay() + 6) % 7;
  const weekStart = new Date(today);
  weekStart.setDate(today.getDate() - todayDow);
  const timedJobs = jobs.filter(
    (j) => j.schedule_time || j.schedule_type === "interval"
  );

  return (
    <div className="flex flex-col h-full">
      <div
        className="px-6 py-3 border-b flex-shrink-0"
        style={{ borderColor: C.border }}
      >
        <p className="text-sm" style={{ color: C.textSecondary }}>
          This week —{" "}
          {weekStart.toLocaleDateString("en-GB", {
            day: "numeric",
            month: "short",
          })}{" "}
          to{" "}
          {new Date(weekStart.getTime() + 6 * 86400000).toLocaleDateString(
            "en-GB",
            { day: "numeric", month: "short" }
          )}
        </p>
      </div>
      <div className="flex-1 overflow-auto">
        <div
          className="grid grid-cols-7 gap-px min-h-full min-w-[560px]"
          style={{ background: C.borderSubtle }}
        >
          {WEEKDAY_LABELS.map((label, dayIdx) => {
            const dayDate = new Date(weekStart.getTime() + dayIdx * 86400000);
            const isToday = dayIdx === todayDow;
            const dayJobs = timedJobs
              .filter((j) => jobRunsOnDay(j, dayIdx))
              .sort((a, b) =>
                (a.schedule_time ?? "").localeCompare(b.schedule_time ?? "")
              );

            return (
              <div
                key={dayIdx}
                className="flex flex-col min-h-full"
                style={{
                  background: isToday ? C.accentSubtle : C.bgBase,
                }}
              >
                <div
                  className="px-2 py-2.5 text-center border-b flex-shrink-0"
                  style={{
                    borderColor: isToday ? C.borderAccent : C.border,
                  }}
                >
                  <div
                    className="text-[10px] font-medium uppercase tracking-wider"
                    style={{
                      color: isToday ? C.accent : C.textMuted,
                    }}
                  >
                    {label}
                  </div>
                  <div
                    className="text-base font-semibold mt-0.5 tabular-nums"
                    style={{
                      color: isToday ? C.accent : C.textSecondary,
                    }}
                  >
                    {dayDate.getDate()}
                  </div>
                </div>

                <div className="flex-1 p-1.5 space-y-1 overflow-y-auto">
                  {dayJobs.map((job) => {
                    const c = chipColors(job, runningJobs);
                    return (
                      <motion.button
                        key={job.id}
                        initial={{ opacity: 0, scale: 0.95 }}
                        animate={{ opacity: 1, scale: 1 }}
                        onClick={() => onJobSelect(job.id)}
                        className="w-full text-left px-1.5 py-1 rounded border text-[10px] transition-all hover:scale-[1.02] cursor-pointer"
                        style={{
                          background: c.bg,
                          borderColor: c.border,
                          color: c.text,
                        }}
                      >
                        <div className="font-medium leading-tight">
                          {job.schedule_time ?? "—"}
                        </div>
                        <div
                          className="truncate leading-tight"
                          style={{ opacity: 0.8 }}
                        >
                          {job.name}
                        </div>
                      </motion.button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── Health Tab ────────────────────────────────────────────────────────────────

function HealthTab({ jobs }: { jobs: ScheduledJob[] }) {
  const unreliable = useMemo(() => {
    return [...jobs]
      .filter(
        (j) =>
          (j.consecutive_failures ?? 0) > 0 || j.last_run_status === "failed"
      )
      .sort(
        (a, b) =>
          (b.consecutive_failures ?? 0) - (a.consecutive_failures ?? 0)
      )
      .slice(0, 10);
  }, [jobs]);

  return (
    <div className="flex flex-col gap-4 px-6 py-5 overflow-y-auto">
      <ScheduleHeatmap
        data={[]}
        title="Job Activity (30 days)"
      />
      <p
        className="text-[11px] -mt-2"
        style={{ color: C.textMuted }}
      >
        Select a job for detailed heatmap data.
      </p>

      <GlassCard className="p-5">
        <div className="flex items-center gap-2 mb-4">
          <AlertTriangle
            size={16}
            style={{ color: C.error }}
          />
          <h3
            className="text-sm font-medium"
            style={{ color: C.textPrimary }}
          >
            Top unreliable jobs
          </h3>
        </div>
        {unreliable.length === 0 ? (
          <p
            className="text-xs py-6 text-center"
            style={{ color: C.textMuted }}
          >
            All jobs healthy — no errors detected.
          </p>
        ) : (
          <div className="flex flex-col gap-2">
            {unreliable.map((j) => (
              <div
                key={j.id}
                className="flex items-start gap-3 p-3 rounded-lg border"
                style={{
                  borderColor: `${C.error}33`,
                  background: `${C.error}0A`,
                }}
              >
                <div className="flex-1 min-w-0">
                  <div
                    className="text-sm font-medium truncate"
                    style={{ color: C.textPrimary }}
                  >
                    {j.name}
                  </div>
                  {j.last_run_error && (
                    <div
                      className="text-[11px] mt-1 line-clamp-2"
                      style={{ color: C.error }}
                    >
                      {j.last_run_error}
                    </div>
                  )}
                  {j.last_run_at && (
                    <div
                      className="text-[10px] mt-0.5"
                      style={{ color: C.textMuted }}
                    >
                      Last run {timeAgo(j.last_run_at)}
                    </div>
                  )}
                </div>
                <div
                  className="text-xs font-mono px-2 py-1 rounded flex-shrink-0"
                  style={{
                    background: `${C.error}1F`,
                    color: C.error,
                  }}
                >
                  {j.consecutive_failures ?? 0}× errors
                </div>
              </div>
            ))}
          </div>
        )}
      </GlassCard>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function SchedulePage() {
  const qc = useQueryClient();
  const activeBoardId = useAppStore((s) => s.activeBoardId);
  const [activeTab, setActiveTab] = useState<Tab>("overview");
  const [runningJobs, setRunningJobs] = useState<Set<string>>(new Set());
  const [modal, setModal] = useState<{
    open: boolean;
    job: ScheduledJob | null;
  }>({ open: false, job: null });

  // SSE
  useSSE(sseUrls.schedule(), {
    onEvent: (event, data) => {
      const jobId = data?.job_id as string | undefined;
      if (!jobId) return;
      if (event === "job.started") {
        setRunningJobs((p) => new Set([...p, jobId]));
      }
      if (event === "job.completed") {
        setRunningJobs((p) => {
          const n = new Set(p);
          n.delete(jobId);
          return n;
        });
        qc.invalidateQueries({ queryKey: ["schedule-jobs"] });
        qc.invalidateQueries({ queryKey: ["schedule-runs", jobId] });
      }
    },
  });

  // Data
  const { data: jobs = [] } = useQuery({
    queryKey: ["schedule-jobs"],
    queryFn: () => api.schedule.listJobs(),
    refetchInterval: 30_000,
  });

  const { data: agents = [] } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
  });

  const { data: boards = [] } = useQuery({
    queryKey: ["boards"],
    queryFn: () => api.boards.list(),
  });

  // Mutations
  const updateMutation = useMutation({
    mutationFn: ({
      id,
      data,
    }: {
      id: string;
      data: Partial<import("@/lib/types").ScheduledJobCreate> & { enabled?: boolean };
    }) => api.schedule.updateJob(id, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["schedule-jobs"] }),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.schedule.deleteJob(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["schedule-jobs"] }),
  });

  const triggerMutation = useMutation({
    mutationFn: (id: string) => api.schedule.triggerJob(id),
  });

  const snoozeMutation = useMutation({
    mutationFn: ({ id, hours }: { id: string; hours: number }) =>
      api.schedule.snoozeJob(id, hours),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["schedule-jobs"] }),
  });

  const duplicateMutation = useMutation({
    mutationFn: (id: string) => api.schedule.duplicateJob(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["schedule-jobs"] }),
  });

  // Handlers
  const handleDelete = (id: string) => {
    const job = jobs.find((j) => j.id === id);
    if (!job) return;
    if (confirm(`Delete "${job.name}"?`)) deleteMutation.mutate(id);
  };

  const handleTrigger = (id: string) => {
    setRunningJobs((p) => new Set([...p, id]));
    triggerMutation.mutate(id, {
      onError: () => {
        setRunningJobs((p) => {
          const n = new Set(p);
          n.delete(id);
          return n;
        });
      },
    });
    setTimeout(() => {
      setRunningJobs((p) => {
        const n = new Set(p);
        n.delete(id);
        return n;
      });
    }, 30_000);
  };

  const handleToggleEnabled = (id: string, enabled: boolean) => {
    updateMutation.mutate({ id, data: { enabled } });
  };

  const handleSnooze = (id: string) => {
    const input = prompt("Snooze for how many hours?", "1");
    if (!input) return;
    const hours = Number(input);
    if (!Number.isFinite(hours) || hours <= 0) return;
    snoozeMutation.mutate({ id, hours });
  };

  const handleDuplicate = (id: string) => {
    duplicateMutation.mutate(id);
  };

  const tabs: { id: Tab; label: string; icon: typeof Activity }[] = [
    { id: "overview", label: "Overview", icon: Activity },
    { id: "day", label: "Today", icon: Clock },
    { id: "week", label: "Week", icon: Clock },
    { id: "health", label: "Health", icon: Heart },
  ];

  return (
    <AppShell>
      <div className="flex flex-col h-full overflow-hidden">
        {/* Tab switcher header */}
        <div
          className="flex items-center gap-3 px-6 py-3 border-b flex-shrink-0"
          style={{ borderColor: C.border }}
        >
          <Clock
            size={16}
            style={{ color: C.accent }}
          />
          <h1
            className="text-sm font-semibold"
            style={{ color: C.textPrimary }}
          >
            Schedule
          </h1>
          {/* tab-strip: mobile horizontal scroll + edge-fade (MOBILE-SPEC M17) */}
          <div
            className="ml-3 flex items-center rounded-lg p-0.5 gap-0.5 tab-strip"
            style={{
              background: C.borderSubtle,
              border: `1px solid ${C.border}`,
            }}
          >
            {tabs.map((tab) => {
              const Icon = tab.icon;
              const isActive = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={cn(
                    "flex items-center gap-1.5 px-3 py-1 rounded-md text-xs transition-colors cursor-pointer min-h-touch"
                  )}
                  style={
                    isActive
                      ? {
                          background: C.accentSubtle,
                          color: C.textPrimary,
                        }
                      : { color: C.textMuted }
                  }
                >
                  <Icon size={12} />
                  {tab.label}
                </button>
              );
            })}
          </div>
        </div>

        {/* Tab content */}
        {activeTab === "overview" && (
          <div className="flex flex-col flex-1 overflow-y-auto">
            <div className="px-6 pt-5 pb-4">
              <ScheduleHeader
                jobs={jobs}
                onNewJob={() => setModal({ open: true, job: null })}
              />
            </div>
            <div className="px-6 pb-6">
              <JobsTable
                jobs={jobs}
                onEdit={(job) => setModal({ open: true, job })}
                onDelete={handleDelete}
                onTrigger={handleTrigger}
                onToggleEnabled={handleToggleEnabled}
                onSnooze={handleSnooze}
                onDuplicate={handleDuplicate}
              />
            </div>
          </div>
        )}

        {activeTab === "day" && (
          <DayTimeline
            jobs={jobs}
            runningJobs={runningJobs}
            onJobSelect={() => setActiveTab("overview")}
          />
        )}

        {activeTab === "week" && (
          <WeekCalendar
            jobs={jobs}
            runningJobs={runningJobs}
            onJobSelect={() => setActiveTab("overview")}
          />
        )}

        {activeTab === "health" && <HealthTab jobs={jobs} />}

        {/* Modal */}
        <AnimatePresence>
          {modal.open && (
            <JobModal
              open={modal.open}
              job={modal.job ?? undefined}
              activeBoardId={activeBoardId ?? ""}
              agents={agents as Agent[]}
              boards={boards as Board[]}
              onClose={() => setModal({ open: false, job: null })}
              onSuccess={() => {
                qc.invalidateQueries({ queryKey: ["schedule-jobs"] });
                setModal({ open: false, job: null });
              }}
            />
          )}
        </AnimatePresence>
      </div>
    </AppShell>
  );
}
