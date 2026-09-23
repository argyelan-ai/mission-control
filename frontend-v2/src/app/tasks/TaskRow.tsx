"use client";

import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { AlertTriangle, Brain, Check, Clock, Send, X } from "lucide-react";
import { api } from "@/lib/api";
import { C, LANE } from "@/lib/colors";
import { EntityIcon } from "@/components/shared/EntityIcon";
import type { Agent, Task, TaskStatus } from "@/lib/types";

// ── Status helpers ────────────────────────────────────────────────────────────

// labelKey resolves via t() at the render site (tasks.* namespace) — never
// store translated strings in module constants.
export const STATUS_CONFIG: Record<TaskStatus, { color: string; labelKey: string }> = {
  inbox: { color: LANE.inbox, labelKey: "statusInbox" },
  in_progress: { color: LANE.in_progress, labelKey: "statusActive" },
  review: { color: LANE.review, labelKey: "statusReview" },
  user_test: { color: LANE.user_test, labelKey: "statusUserTest" },
  waiting: { color: LANE.waiting, labelKey: "statusWaiting" },
  done: { color: LANE.done, labelKey: "statusDone" },
  blocked: { color: LANE.blocked, labelKey: "statusBlocked" },
  failed: { color: LANE.failed, labelKey: "statusFailed" },
  aborted: { color: LANE.aborted, labelKey: "statusAborted" },
};

export function TaskStatusDot({ status }: { status: TaskStatus }) {
  const icons: Partial<Record<TaskStatus, React.ReactNode>> = {
    // Glyphs sit ON the status fill → dark ink (≥5:1 on every status hue;
    // white would be 2.9–3.7:1 against the System-A status colours).
    done: <Check size={8} strokeWidth={3} className="text-[var(--color-on-accent)]" />,
    blocked: <X size={8} strokeWidth={3} className="text-[var(--color-on-accent)]" />,
    failed: <X size={8} strokeWidth={3} className="text-[var(--color-on-accent)]" />,
    aborted: <X size={8} strokeWidth={3} className="text-[var(--color-on-accent)]" />,
  };

  const color = STATUS_CONFIG[status].color;
  const isEmpty = status === "inbox";

  return (
    <span
      className="w-4 h-4 rounded-sm shrink-0 flex items-center justify-center"
      style={{
        backgroundColor: isEmpty ? "transparent" : color,
        border: isEmpty ? `2px solid ${color}` : "none",
      }}
    >
      {icons[status]}
    </span>
  );
}

// ── Task Row ──────────────────────────────────────────────────────────────────

export function TaskRow({
  task,
  agents,
  boardId,
  onClick,
}: {
  task: Task;
  agents: Agent[];
  boardId: string;
  onClick: () => void;
}) {
  const t = useTranslations("tasks");
  const agent = agents.find((a) => a.id === task.assigned_agent_id);
  const qc = useQueryClient();
  const [showDoneWarning, setShowDoneWarning] = useState(false);

  const dispatchMutation = useMutation({
    mutationFn: () => api.tasks.update(boardId, task.id, { status: "in_progress" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      setShowDoneWarning(false);
    },
  });

  const canDispatch = task.status === "inbox" && task.assigned_agent_id;
  const isDone = task.status === "done";

  const staleMins = useMemo(() => {
    if (task.status !== "in_progress" || !task.last_activity_at) return 0;
    return Math.floor((Date.now() - new Date(task.last_activity_at).getTime()) / 60000);
  }, [task.status, task.last_activity_at]);
  const isStale = staleMins >= 15;
  const isCritical = staleMins >= 30;

  const priorityColor = (p: string) => {
    switch (p) {
      case "critical": return C.error;
      case "high": return C.warning;
      default: return null;
    }
  };

  const handleDispatch = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (isDone) { setShowDoneWarning(true); return; }
    dispatchMutation.mutate();
  };

  const handleForceDispatch = (e: React.MouseEvent) => {
    e.stopPropagation();
    dispatchMutation.mutate();
  };

  return (
    <div className="relative">
      {/* Kein <button> als Container (nested-interactive): Titel-Button deckt
          per ::after die ganze Zeile ab, Aktionen liegen mit z-[1] darüber. */}
      <div className="w-full flex items-center gap-3 px-3 py-2 rounded-md text-left transition-all bg-[var(--color-bg-surface)] hover:bg-[var(--color-bg-hover)] group" style={{ border: `1px solid ${C.border}` }}>
        <TaskStatusDot status={task.status} />
        <button
          type="button"
          onClick={onClick}
          aria-label={t("openTask", { title: task.title })}
          className="flex-1 text-sm truncate flex items-center gap-1 min-w-0 text-left cursor-pointer after:absolute after:inset-0 after:content-['']"
          style={{ color: isDone ? C.textMuted : C.textPrimary }}
        >
          <span className="truncate">{task.title}</span>
          {/* Checklist-Progress Badge */}
          {task.checklist_total > 0 && (
            <span
              className="ml-1.5 px-1.5 py-0.5 rounded-sm text-xs font-mono shrink-0"
              style={{
                background:
                  task.checklist_done === task.checklist_total
                    ? `${C.online}26`
                    : C.accentSubtle,
                color:
                  task.checklist_done === task.checklist_total
                    ? C.online
                    : C.accent,
              }}
            >
              {task.checklist_done}/{task.checklist_total}
            </span>
          )}
        </button>
        <div className="relative z-[1] flex items-center gap-2 shrink-0 opacity-60 group-hover:opacity-100 transition-opacity">
          {task.priority !== "medium" && priorityColor(task.priority) && (
            <span
              className="text-[10px] px-1 py-0.5 rounded-sm uppercase font-semibold"
              style={{ color: priorityColor(task.priority)! }}
            >
              {task.priority}
            </span>
          )}
          {agent && (
            <span title={agent.name}>
              <EntityIcon value={agent.emoji} size={13} />
            </span>
          )}
          {isStale && (
            <span
              className="inline-flex items-center gap-0.5 text-[10px] font-medium px-1.5 py-0.5 rounded-sm"
              title={t("noActivityFor", { mins: staleMins })}
              style={{
                color: isCritical ? C.error : C.warning,
                backgroundColor: isCritical ? `${C.error}1A` : `${C.warning}1A`,
              }}
            >
              <Clock size={10} />
              {staleMins}m
            </span>
          )}
          {/* Phase E task-klammer quick-link: jump to all vault notes +
              wrappers that share this task's UUID. Hover-only so the row
              stays uncluttered for the common case where the operator just wants
              to scan the task list. */}
          <Link
            href={`/memory?task=${task.id}`}
            onClick={(e) => e.stopPropagation()}
            className="p-1 rounded-sm transition-colors opacity-0 group-hover:opacity-100 hover:bg-[var(--color-bg-hover)] cursor-pointer touch-visible"
            title={t("vaultLink")}
            style={{ color: C.textMuted }}
          >
            <Brain size={12} />
          </Link>
          {(canDispatch || isDone) && (
            <button
              onClick={handleDispatch}
              disabled={dispatchMutation.isPending}
              className="p-1 rounded-sm transition-colors opacity-0 group-hover:opacity-100 hover:bg-[var(--color-bg-hover)] cursor-pointer touch-visible"
              title={isDone ? t("taskAlreadyDoneDispatch") : t("dispatchTask")}
              style={{ color: isDone ? C.warning : C.accent }}
            >
              <Send size={12} />
            </button>
          )}
        </div>
      </div>

      {/* Done warning */}
      {showDoneWarning && (
        <div
          className="absolute right-2 top-full mt-1 z-10 p-3 rounded-md text-xs"
          style={{
            backgroundColor: C.bgBase,
            border: `1px solid ${C.warning}40`,
            boxShadow: "var(--shadow-elevated)",
          }}
        >
          <div className="flex items-center gap-1.5 mb-2 font-medium" style={{ color: C.warning }}>
            <AlertTriangle size={12} />
            {t("taskAlreadyDone")}
          </div>
          <p className="mb-2" style={{ color: C.textSecondary }}>
            {t("dispatchAgainConfirm")}
          </p>
          <div className="flex gap-2">
            <button
              onClick={handleForceDispatch}
              disabled={dispatchMutation.isPending}
              className="px-2 py-1 rounded-sm text-[11px] font-medium cursor-pointer"
              style={{ backgroundColor: `${C.warning}1F`, color: C.warning }}
            >
              {t("yesDispatch")}
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); setShowDoneWarning(false); }}
              className="px-2 py-1 rounded-sm text-[11px] cursor-pointer"
              style={{ color: C.textMuted }}
            >
              {t("cancel")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

