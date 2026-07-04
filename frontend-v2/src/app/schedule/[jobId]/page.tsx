"use client";

import { useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import {
  ArrowLeft,
  Pencil,
  CheckCircle2,
  XCircle,
  SkipForward,
  Loader2,
  AlertTriangle,
  ExternalLink,
} from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { GlassCard } from "@/components/shared/GlassCard";
import { KPICard } from "@/components/shared/KPICard";
import { JobModal } from "@/components/schedule/JobModal";
import { api } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { timeAgo, cn } from "@/lib/utils";
import type {
  Agent,
  Board,
  ScheduledJob,
  ScheduledJobCreate,
  ScheduledJobRun,
  Task,
  TaskStatus,
} from "@/lib/types";
import { C, LANE, STATUS_TEXT } from "@/lib/colors";

// Helpers
function formatMs(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}min`;
}

function formatPercent(rate: number | null | undefined): string {
  if (rate == null || !Number.isFinite(rate)) return "—";
  return `${(rate * 100).toFixed(0)}%`;
}

function scheduleLabel(job: ScheduledJob): string {
  if (job.schedule_cron) return `cron ${job.schedule_cron}`;
  if (job.schedule_type === "daily" && job.schedule_time)
    return `daily ${job.schedule_time}`;
  if (job.schedule_type === "weekdays" && job.schedule_time)
    return `Mon–Fri ${job.schedule_time}`;
  if (job.schedule_type === "interval" && job.schedule_interval_hours)
    return `every ${job.schedule_interval_hours}h`;
  if (job.schedule_type === "weekly_custom" && job.schedule_weekdays)
    return `weekdays ${job.schedule_weekdays.join(",")} ${job.schedule_time ?? ""}`;
  return "—";
}

// Task status chips via LANE map
const TASK_STATUS_COLOR: Record<string, string> = {
  inbox:       `${LANE.inbox}26`,
  in_progress: `${LANE.in_progress}2E`,
  review:      `${LANE.review}2E`,
  blocked:     `${LANE.blocked}2E`,
  done:        `${LANE.done}29`,
  failed:      `${LANE.failed}2E`,
};
const TASK_STATUS_TEXT: Record<string, string> = {
  inbox:       C.textMuted,
  in_progress: STATUS_TEXT.info,
  review:      C.warning,
  blocked:     STATUS_TEXT.error,
  done:        C.online,
  failed:      STATUS_TEXT.error,
};

function RunStatusIcon({ status }: { status: string }) {
  const size = 13;
  if (status === "success")
    return (
      <CheckCircle2 size={size} style={{ color: C.online }} />
    );
  if (status === "failed")
    return <XCircle size={size} style={{ color: C.error }} />;
  if (status === "running")
    return (
      <Loader2 size={size} className="animate-spin" style={{ color: C.accent }} />
    );
  return <SkipForward size={size} style={{ color: C.textMuted }} />;
}

export default function ScheduleJobDetailPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const params = useParams<{ jobId: string }>();
  const jobId = params?.jobId ?? "";
  const activeBoardId = useAppStore((s) => s.activeBoardId);

  const [editOpen, setEditOpen] = useState(false);

  // Job (lookup via list since no getJob endpoint exists)
  const { data: jobs = [] } = useQuery({
    queryKey: ["schedule-jobs"],
    queryFn: () => api.schedule.listJobs(),
    refetchInterval: 30_000,
  });
  const job = useMemo(() => jobs.find((j) => j.id === jobId) ?? null, [jobs, jobId]);

  const { data: stats } = useQuery({
    queryKey: ["schedule-job-stats", jobId],
    queryFn: () => api.schedule.getStats(jobId),
    enabled: !!jobId,
    refetchInterval: 60_000,
  });

  const { data: runs = [] } = useQuery({
    queryKey: ["schedule-job-runs", jobId],
    queryFn: () => api.schedule.getRuns(jobId, 50),
    enabled: !!jobId,
    refetchInterval: 30_000,
  });

  const { data: createdTasks = [] } = useQuery({
    queryKey: ["schedule-job-tasks", jobId],
    queryFn: () => api.schedule.getCreatedTasks(jobId, 20),
    enabled: !!jobId,
    refetchInterval: 60_000,
  });

  const { data: agents = [] } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
  });

  const { data: boards = [] } = useQuery({
    queryKey: ["boards"],
    queryFn: () => api.boards.list(),
  });

  const updateMutation = useMutation({
    mutationFn: (data: Partial<ScheduledJobCreate> & { enabled?: boolean }) =>
      api.schedule.updateJob(jobId, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["schedule-jobs"] }),
  });

  const board = boards.find((b: Board) => b.id === job?.task_board_id) ?? null;
  const agentLookup = useMemo(() => {
    const m = new Map<string, Agent>();
    for (const a of agents as Agent[]) m.set(a.id, a);
    return m;
  }, [agents]);

  if (!job) {
    return (
      <AppShell>
        <div className="flex flex-col items-center justify-center h-full gap-3">
          <Loader2 size={20} className="animate-spin" style={{ color: C.accent }} />
          <p className="text-sm" style={{ color: C.textMuted }}>
            Loading job…
          </p>
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <div className="flex flex-col h-full overflow-y-auto">
        {/* Header */}
        <div
          className="flex items-center gap-3 px-6 py-3 border-b flex-shrink-0"
          style={{ borderColor: C.border }}
        >
          <Link
            href="/schedule"
            aria-label="Back to schedule"
            className="flex items-center gap-1.5 text-xs px-2 py-1 rounded transition-colors shrink-0"
            style={{ color: C.textMuted }}
          >
            <ArrowLeft size={13} />
            <span className="max-sm:hidden">Schedule</span>
          </Link>
          <span className="max-sm:hidden" style={{ color: C.textMuted }}>/</span>
          {/* h2 intentionally — this is a sub-page breadcrumb, h1 already in AppShell nav */}
          <h1
            className="text-sm font-semibold flex-1 min-w-0 truncate"
            style={{ color: C.textPrimary }}
          >
            {job.name}
          </h1>
          <button
            onClick={() => updateMutation.mutate({ enabled: !job.enabled })}
            className="px-2.5 py-1 rounded text-[11px] font-mono transition-colors cursor-pointer shrink-0"
            style={{
              color: job.enabled ? C.online : C.textMuted,
              border: `1px solid ${C.borderActive}`,
            }}
          >
            {job.enabled ? "ON" : "OFF"}
          </button>
          <button
            onClick={() => setEditOpen(true)}
            aria-label="Edit job"
            className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs transition-colors cursor-pointer shrink-0"
            style={{
              color: C.textSecondary,
              border: `1px solid ${C.borderActive}`,
            }}
          >
            <Pencil size={12} />
            <span className="max-sm:hidden">Edit</span>
          </button>
        </div>

        <div className="flex flex-col gap-5 p-6">
          {/* KPI Cards */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <KPICard
              label="Success Rate (7d)"
              value={formatPercent(stats?.success_rate_7d)}
            />
            <KPICard
              label="Avg. Duration"
              value={formatMs(stats?.avg_duration_ms ?? 0)}
            />
            <KPICard
              label="P95 Duration"
              value={formatMs(stats?.p95_duration_ms ?? 0)}
            />
            <KPICard
              label="Runs (30d)"
              value={stats?.total_runs_30d ?? 0}
            />
          </div>

          {/* Chart + Config */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <GlassCard className="lg:col-span-2 p-5">
              <h2
                className="text-sm font-medium mb-3"
                style={{ color: C.textPrimary }}
              >
                Runs (30 days)
              </h2>
              <div style={{ width: "100%", height: 220 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={stats?.runs_by_day ?? []}>
                    <CartesianGrid strokeDasharray="3 3" stroke={C.borderSubtle} />
                    <XAxis
                      dataKey="date"
                      tick={{ fill: C.textMuted, fontSize: 11 }}
                      stroke={C.borderActive}
                    />
                    <YAxis
                      tick={{ fill: C.textMuted, fontSize: 11 }}
                      stroke={C.borderActive}
                      allowDecimals={false}
                    />
                    <Tooltip
                      contentStyle={{
                        background: C.bgSurface,
                        border: `1px solid ${C.border}`,
                        borderRadius: 8,
                        fontSize: 12,
                      }}
                    />
                    <Area
                      type="monotone"
                      dataKey="success"
                      stackId="1"
                      stroke={C.online}
                      fill={`${C.online}4D`}
                    />
                    <Area
                      type="monotone"
                      dataKey="failed"
                      stackId="1"
                      stroke={C.error}
                      fill={`${C.error}4D`}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </GlassCard>

            {/* Config Card */}
            <GlassCard className="p-5">
              <h2
                className="text-sm font-medium mb-3"
                style={{ color: C.textPrimary }}
              >
                Configuration
              </h2>
              <dl className="flex flex-col gap-3 text-xs">
                <ConfigRow label="Trigger" value={scheduleLabel(job)} />
                <ConfigRow
                  label="Next run"
                  value={
                    job.next_run_at
                      ? new Date(job.next_run_at).toLocaleString("de-CH", {
                          weekday: "short",
                          day: "2-digit",
                          month: "2-digit",
                          hour: "2-digit",
                          minute: "2-digit",
                        })
                      : "—"
                  }
                />
                <ConfigRow
                  label="Last status"
                  value={job.last_run_status ?? "—"}
                  valueColor={
                    job.last_run_status === "failed"
                      ? STATUS_TEXT.error
                      : job.last_run_status === "success"
                      ? C.online
                      : undefined
                  }
                />
                <ConfigRow
                  label="Retry"
                  value={
                    job.retry_max > 0
                      ? `${job.retry_max}× / ${job.retry_delay_minutes}min`
                      : "none"
                  }
                />
                <ConfigRow
                  label="Skip Review"
                  value={job.task_skip_review ? "yes" : "no"}
                />
                <ConfigRow label="Board" value={board?.name ?? "—"} />
                <ConfigRow label="Action" value={job.action_type} />
                {job.consecutive_failures && job.consecutive_failures > 0 ? (
                  <ConfigRow
                    label="Consecutive failures"
                    value={String(job.consecutive_failures)}
                    valueColor={STATUS_TEXT.error}
                  />
                ) : null}
                {job.snoozed_until ? (
                  <ConfigRow
                    label="Snoozed until"
                    value={new Date(job.snoozed_until).toLocaleString("de-CH")}
                  />
                ) : null}
              </dl>
            </GlassCard>
          </div>

          {/* Run History + Created Tasks */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <GlassCard className="p-5">
              <h2
                className="text-sm font-medium mb-3"
                style={{ color: C.textPrimary }}
              >
                Run History
              </h2>
              {runs.length === 0 ? (
                <p
                  className="text-xs py-6 text-center"
                  style={{ color: C.textMuted }}
                >
                  No runs yet.
                </p>
              ) : (
                <div className="flex flex-col max-h-[400px] overflow-y-auto">
                  {runs.map((run) => (
                    <RunRow key={run.id} run={run} />
                  ))}
                </div>
              )}
            </GlassCard>

            <GlassCard className="p-5">
              <h2
                className="text-sm font-medium mb-3"
                style={{ color: C.textPrimary }}
              >
                Created Tasks
              </h2>
              {createdTasks.length === 0 ? (
                <p
                  className="text-xs py-6 text-center"
                  style={{ color: C.textMuted }}
                >
                  This job hasn&apos;t created any tasks yet.
                </p>
              ) : (
                <div className="flex flex-col gap-1.5 max-h-[400px] overflow-y-auto">
                  {createdTasks.map((t: Task) => (
                    <TaskRow
                      key={t.id}
                      task={t}
                      agentName={
                        t.assigned_agent_id
                          ? agentLookup.get(t.assigned_agent_id)?.name ?? null
                          : null
                      }
                    />
                  ))}
                </div>
              )}
            </GlassCard>
          </div>
        </div>

        {/* Edit Modal */}
        {editOpen && (
          <JobModal
            open={editOpen}
            job={job}
            activeBoardId={activeBoardId ?? ""}
            agents={agents as Agent[]}
            boards={boards as Board[]}
            onClose={() => setEditOpen(false)}
            onSuccess={() => {
              qc.invalidateQueries({ queryKey: ["schedule-jobs"] });
              setEditOpen(false);
            }}
          />
        )}
      </div>
    </AppShell>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function ConfigRow({
  label,
  value,
  valueColor,
}: {
  label: string;
  value: string;
  valueColor?: string;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt
        className="text-[10px] uppercase tracking-wider flex-shrink-0"
        style={{ color: C.textMuted }}
      >
        {label}
      </dt>
      <dd
        className="text-xs text-right truncate"
        style={{ color: valueColor ?? C.textPrimary }}
      >
        {value}
      </dd>
    </div>
  );
}

function RunRow({ run }: { run: ScheduledJobRun }) {
  const duration =
    run.finished_at != null
      ? new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()
      : null;

  return (
    <div
      className="flex items-center gap-2.5 py-2 border-b text-xs last:border-0"
      style={{ borderColor: C.borderSubtle }}
    >
      <RunStatusIcon status={run.status} />
      <span
        className="flex-1 min-w-0 truncate"
        style={{ color: C.textSecondary }}
      >
        {timeAgo(run.started_at)}
        {run.retry_attempt > 0 && (
          <span className="ml-1.5" style={{ color: C.textMuted }}>
            (retry {run.retry_attempt})
          </span>
        )}
      </span>
      {duration !== null && (
        <span
          className="font-mono text-[11px] flex-shrink-0"
          style={{ color: C.textMuted }}
        >
          {formatMs(duration)}
        </span>
      )}
      {run.error && (
        <span
          className="max-w-[140px] truncate text-[11px] flex-shrink-0"
          title={run.error}
          style={{ color: STATUS_TEXT.error }}
        >
          {run.error}
        </span>
      )}
      {run.task_id && (
        <Link
          href={`/tasks/${run.task_id}`}
          title="Go to task"
          className="flex-shrink-0 p-1 rounded transition-colors"
          style={{ color: C.textMuted }}
        >
          <ExternalLink size={11} />
        </Link>
      )}
    </div>
  );
}

function TaskRow({ task, agentName }: { task: Task; agentName: string | null }) {
  const status = task.status as TaskStatus;
  const ts = task.completed_at ?? task.started_at ?? null;
  return (
    <Link
      href={`/tasks/${task.id}`}
      className="flex items-center gap-2.5 px-3 py-2 rounded-lg border transition-colors"
      style={{ borderColor: C.border }}
    >
      <span
        className="text-[10px] uppercase font-medium px-1.5 py-0.5 rounded flex-shrink-0"
        style={{
          background: TASK_STATUS_COLOR[status] ?? C.borderActive,
          color: TASK_STATUS_TEXT[status] ?? C.textSecondary,
        }}
      >
        {status}
      </span>
      <span
        className={cn("flex-1 min-w-0 truncate text-xs")}
        style={{ color: C.textPrimary }}
      >
        {task.title}
      </span>
      {agentName && (
        <span
          className="text-[10px] flex-shrink-0"
          style={{ color: C.textMuted }}
        >
          {agentName}
        </span>
      )}
      {ts && (
        <span
          className="text-[10px] flex-shrink-0"
          style={{ color: C.textMuted }}
        >
          {timeAgo(ts)}
        </span>
      )}
    </Link>
  );
}
