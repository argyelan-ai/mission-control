"use client";

/**
 * JobRow — single job entry inside JobsTable. Designed for table-like
 * grid (status | name+tags | trigger | next | last | agent | actions).
 *
 * Hover-only action cluster on the right. Click on the name area opens
 * the job detail page.
 */

import { useRouter } from "next/navigation";
import {
  Play,
  Pencil,
  Copy,
  AlarmClockOff,
  Trash2,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Loader2,
} from "lucide-react";
import { motion } from "framer-motion";
import type { ScheduledJob } from "@/lib/types";
import cronstrue from "cronstrue";
import { C, STATUS_TEXT } from "@/lib/colors";

interface JobRowProps {
  job: ScheduledJob;
  selected: boolean;
  onSelectChange: (selected: boolean) => void;
  onEdit: (job: ScheduledJob) => void;
  onDelete: (id: string) => void;
  onTrigger: (id: string) => void;
  onToggleEnabled: (id: string, enabled: boolean) => void;
  onSnooze: (id: string) => void;
  onDuplicate: (id: string) => void;
}

// Tag colors: use a rotating palette of token-based colors (accent + status)
const TAG_COLORS = [
  C.accent,
  C.info,
  C.warning,
  C.textSecondary,
  C.online,
  C.info,
  C.error,
  C.accent,
];

function tagColor(tag: string): string {
  let hash = 0;
  for (let i = 0; i < tag.length; i += 1) hash = (hash * 31 + tag.charCodeAt(i)) | 0;
  return TAG_COLORS[Math.abs(hash) % TAG_COLORS.length];
}

function describeTrigger(j: ScheduledJob): string {
  if (j.schedule_cron) {
    try {
      return cronstrue.toString(j.schedule_cron, { locale: "en" });
    } catch {
      return `Cron: ${j.schedule_cron}`;
    }
  }
  if (j.schedule_type === "daily") return `Daily at ${j.schedule_time ?? "—"}`;
  if (j.schedule_type === "weekdays") return `Mon–Fri at ${j.schedule_time ?? "—"}`;
  if (j.schedule_type === "interval") {
    const h = j.schedule_interval_hours ?? 0;
    if (h === 1) return "Every hour";
    return `Every ${h} hours`;
  }
  return "—";
}

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  const diff = Date.now() - t;
  if (diff < 0) {
    const mAhead = Math.floor(-diff / 60000);
    if (mAhead < 60) return `in ${mAhead}m`;
    const hAhead = Math.floor(mAhead / 60);
    if (hAhead < 48) return `in ${hAhead}h`;
    const dAhead = Math.floor(hAhead / 24);
    return `in ${dAhead}d`;
  }
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function StatusDot({ job }: { job: ScheduledJob }) {
  if (!job.enabled) {
    return (
      <span
        className="block h-2 w-2 rounded-full"
        style={{ background: C.borderActive }}
        title="Paused"
      />
    );
  }
  if ((job.last_run_status as string | null) === "running") {
    return <Loader2 size={12} className="animate-spin" style={{ color: C.warning }} />;
  }
  if (job.last_run_status === "failed") {
    return (
      <span
        className="block h-2 w-2 rounded-full"
        style={{ background: C.error }}
        title="Last run failed"
      />
    );
  }
  if (job.last_run_status === "success") {
    return (
      <span
        className="block h-2 w-2 rounded-full"
        style={{ background: C.online }}
        title="Successful"
      />
    );
  }
  return (
    <span
      className="block h-2 w-2 rounded-full"
      style={{ background: C.textSecondary }}
      title="Not run yet"
    />
  );
}

export function JobRow({
  job,
  selected,
  onSelectChange,
  onEdit,
  onDelete,
  onTrigger,
  onToggleEnabled,
  onSnooze,
  onDuplicate,
}: JobRowProps) {
  const router = useRouter();
  const tags = job.tags ?? [];
  const visibleTags = tags.slice(0, 2);
  const overflowCount = tags.length - visibleTags.length;
  const failures = job.consecutive_failures ?? 0;
  const snoozedUntil = job.snoozed_until;

  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      className="group relative grid grid-cols-[24px_24px_minmax(0,2fr)_minmax(0,1.5fr)_1fr_1fr_minmax(0,1fr)_auto] items-center gap-3 rounded-lg px-3 py-2.5 transition"
      style={{
        border: `1px solid ${C.border}`,
        background: C.borderSubtle,
      }}
    >
      <input
        type="checkbox"
        checked={selected}
        onChange={(e) => onSelectChange(e.target.checked)}
        className="h-3.5 w-3.5 cursor-pointer"
        style={{ accentColor: C.accent }}
        aria-label={`Select ${job.name}`}
      />

      <div className="flex items-center justify-center">
        <StatusDot job={job} />
      </div>

      {/* Sticky Name cell — left offset: checkbox(24px) + gap(12px) + dot(24px) + gap(12px) = 72px.
          Opaque bg required so scrolling row content doesn't show through (M11).
          bgSurface matches the row tone (borderSubtle over bgDeep) — bgBase read as a black hole on mobile. */}
      <button
        type="button"
        onClick={() => router.push(`/schedule/${job.id}`)}
        className="flex min-w-0 flex-col items-start gap-0.5 text-left sticky z-10"
        style={{ left: "72px", backgroundColor: C.bgSurface }}
      >
        <span className="flex items-center gap-1.5 text-sm font-medium truncate max-w-full" style={{ color: C.textPrimary }}>
          <span className="truncate">{job.name}</span>
          {failures >= 2 && (
            <span
              className="flex shrink-0 items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[9px] font-semibold"
              style={{
                background: `${C.error}1F`,
                color: STATUS_TEXT.error,
                border: `1px solid ${C.error}4D`,
              }}
              title={`${failures} consecutive failed runs`}
            >
              <AlertTriangle size={9} />
              {failures}x fail
            </span>
          )}
          {snoozedUntil && (
            <span
              className="shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-medium"
              style={{
                background: `${C.warning}1F`,
                color: C.warning,
                border: `1px solid ${C.warning}4D`,
              }}
              title={`Snoozed until ${new Date(snoozedUntil).toLocaleString("en-GB")}`}
            >
              💤 {new Date(snoozedUntil).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}
            </span>
          )}
        </span>
        {(visibleTags.length > 0 || overflowCount > 0) && (
          <div className="flex items-center gap-1">
            {visibleTags.map((t) => {
              const c = tagColor(t);
              return (
                <span
                  key={t}
                  className="rounded-full px-1.5 py-0.5 text-[9px] font-medium"
                  style={{
                    background: `${c}1F`,
                    color: c,
                    border: `1px solid ${c}33`,
                  }}
                >
                  {t}
                </span>
              );
            })}
            {overflowCount > 0 && (
              <span className="text-[9px]" style={{ color: C.textDim }}>+{overflowCount}</span>
            )}
          </div>
        )}
      </button>

      <div className="min-w-0 truncate text-xs" style={{ color: C.textSecondary }} title={describeTrigger(job)}>
        {describeTrigger(job)}
      </div>

      <div className="text-xs" style={{ color: C.textSecondary }}>{relativeTime(job.next_run_at)}</div>

      <div className="flex items-center gap-1.5 text-xs" style={{ color: C.textSecondary }}>
        {job.last_run_status === "success" && (
          <CheckCircle2 size={12} style={{ color: C.online }} />
        )}
        {job.last_run_status === "failed" && (
          <XCircle size={12} style={{ color: C.error }} />
        )}
        <span>{relativeTime(job.last_run_at)}</span>
      </div>

      <div className="min-w-0 truncate text-xs" style={{ color: C.textSecondary }}>
        {job.agent_name ?? "—"}
      </div>

      <div className="flex items-center gap-1 opacity-0 transition group-hover:opacity-100 touch-visible-secondary">
        <IconBtn
          title={job.enabled ? "Pause" : "Enable"}
          onClick={() => onToggleEnabled(job.id, !job.enabled)}
        >
          <span
            className="block h-2 w-2 rounded-full"
            style={{
              background: job.enabled ? C.online : C.borderActive,
            }}
          />
        </IconBtn>
        <IconBtn title="Run now" onClick={() => onTrigger(job.id)}>
          <Play size={12} />
        </IconBtn>
        <IconBtn title="Edit" onClick={() => onEdit(job)}>
          <Pencil size={12} />
        </IconBtn>
        <IconBtn title="Duplicate" onClick={() => onDuplicate(job.id)}>
          <Copy size={12} />
        </IconBtn>
        <IconBtn title="Snooze" onClick={() => onSnooze(job.id)}>
          <AlarmClockOff size={12} />
        </IconBtn>
        <IconBtn title="Delete" danger onClick={() => onDelete(job.id)}>
          <Trash2 size={12} />
        </IconBtn>
      </div>
    </motion.div>
  );
}

function IconBtn({
  children,
  onClick,
  title,
  danger,
}: {
  children: React.ReactNode;
  onClick: () => void;
  title: string;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      title={title}
      className="relative flex h-7 w-7 items-center justify-center rounded-md transition after:absolute after:-inset-1.5 max-sm:after:-inset-2"
      style={{
        border: `1px solid ${C.borderSubtle}`,
        background: C.borderSubtle,
        color: danger ? STATUS_TEXT.error : C.textSecondary,
      }}
    >
      {children}
    </button>
  );
}
