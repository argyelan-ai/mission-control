"use client";

/**
 * JobCard — one scheduled job on the phone (round-3 user test t4 S8).
 *
 * The table needs ~640 px: at 390 px it was squeezed ("Morning Bri…",
 * "Täglich u…") and the status column was cut off on the right. On the
 * phone a job is a card instead: the whole name, the trigger and the next
 * run in one line, the last run in a second, every action behind ⋯. Tap
 * opens the job. Selecting several jobs at once stays on the desktop table.
 */
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { AlarmClockOff, Copy, Pause, Pencil, Play, Power, Trash2 } from "lucide-react";
import type { ScheduledJob } from "@/lib/types";
import { C } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { OverflowMenu, type OverflowAction } from "@/components/shared/OverflowMenu";
import { describeTrigger, StatusDot } from "./JobRow";
import { nextRunRelative } from "./jobDisplay";

export function JobCard({
  job,
  onEdit,
  onDelete,
  onTrigger,
  onToggleEnabled,
  onSnooze,
  onDuplicate,
}: {
  job: ScheduledJob;
  onEdit: (job: ScheduledJob) => void;
  onDelete: (id: string) => void;
  onTrigger: (id: string) => void;
  onToggleEnabled: (id: string, enabled: boolean) => void;
  onSnooze: (id: string) => void;
  onDuplicate: (id: string) => void;
}) {
  const t = useTranslations("schedule");
  const locale = useLocale();
  const router = useRouter();
  const actions: OverflowAction[] = [
    { id: "run", label: t("runNow"), icon: Play, onClick: () => onTrigger(job.id) },
    {
      id: "toggle",
      label: job.enabled ? t("pause") : t("enable"),
      icon: job.enabled ? Pause : Power,
      onClick: () => onToggleEnabled(job.id, !job.enabled),
    },
    { id: "edit", label: t("edit"), icon: Pencil, onClick: () => onEdit(job) },
    { id: "duplicate", label: t("duplicate"), icon: Copy, onClick: () => onDuplicate(job.id) },
    { id: "snooze", label: t("snoozeMenu"), icon: AlarmClockOff, onClick: () => onSnooze(job.id) },
    { id: "delete", label: t("delete"), icon: Trash2, onClick: () => onDelete(job.id), destructive: true },
  ];
  const last = job.last_run_at ? t("lastRun", { ago: timeAgo(job.last_run_at, locale) }) : t("notRunYet");
  return (
    <div
      data-testid="job-card"
      data-region="job-card"
      className="flex items-start gap-2 rounded-lg pl-3 py-2"
      style={{ border: `1px solid ${C.border}`, background: C.borderSubtle }}
    >
      <span className="w-4 h-11 shrink-0 flex items-center justify-center" aria-hidden="true">
        <StatusDot job={job} />
      </span>
      <button
        type="button"
        onClick={() => router.push(`/schedule/${job.id}`)}
        className="min-w-0 flex-1 min-h-11 py-1 flex flex-col items-start gap-1 text-left bg-transparent border-0 p-0 cursor-pointer"
      >
        <span className="text-sm font-medium break-words line-clamp-3" style={{ color: C.textPrimary }}>
          {job.name}
        </span>
        <span className="text-xs" style={{ color: C.textSecondary }}>
          {describeTrigger(job, t, locale)} · {t("nextLabel", { when: nextRunRelative(job, t) })}
        </span>
        <span className="text-xs" style={{ color: C.textMuted }}>
          {last}
          {job.agent_name ? ` · ${job.agent_name}` : ""}
        </span>
      </button>
      <OverflowMenu actions={actions} label={t("jobActions", { name: job.name })} />
    </div>
  );
}
