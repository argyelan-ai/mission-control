"use client";

import { X, Check, Trash2 } from "lucide-react";
import { Pill } from "@/components/shared/Pill";
import { StatusDot } from "@/components/shared/StatusDot";
import type { Task, TaskStatus, Agent } from "@/lib/types";
import { C, LANE } from "@/lib/colors";

// ── Status mapping ────────────────────────────────────────────────────────────

const STATUS_CONFIG: Record<TaskStatus, { color: string; label: string; dotStatus: "online" | "warning" | "error" | "busy" | "idle" | "offline" }> = {
  inbox: { color: LANE.inbox, label: "Inbox", dotStatus: "idle" },
  in_progress: { color: LANE.in_progress, label: "Active", dotStatus: "busy" },
  review: { color: LANE.review, label: "Review", dotStatus: "warning" },
  user_test: { color: LANE.user_test, label: "User Test", dotStatus: "busy" },
  done: { color: LANE.done, label: "Done", dotStatus: "online" },
  blocked: { color: LANE.blocked, label: "Blocked", dotStatus: "error" },
  failed: { color: LANE.failed, label: "Failed", dotStatus: "error" },
  aborted: { color: LANE.aborted, label: "Aborted", dotStatus: "warning" },
};

const PRIORITY_COLORS: Record<string, string> = {
  critical: C.error,
  high: C.warning,
  medium: C.textSecondary,
  low: C.textMuted,
};

// ── TaskHeader ────────────────────────────────────────────────────────────────

interface TaskHeaderProps {
  task: Task;
  agent: Agent | undefined;
  confirmDelete: boolean;
  setConfirmDelete: (v: boolean) => void;
  onDelete: () => void;
  deleteLoading: boolean;
  onClose: () => void;
}

export function TaskHeader({
  task,
  agent,
  confirmDelete,
  setConfirmDelete,
  onDelete,
  deleteLoading,
  onClose,
}: TaskHeaderProps) {
  const statusCfg = STATUS_CONFIG[task.status];
  const isActive = task.status === "in_progress" || task.status === "review";

  return (
    <div
      className="flex items-start gap-3 p-4 border-b shrink-0"
      style={{ borderColor: C.border }}
    >
      {/* Status circle */}
      <div className="relative mt-0.5">
        <div
          className="w-6 h-6 rounded-full flex items-center justify-center shrink-0"
          style={{
            border: `2px solid ${statusCfg.color}`,
            backgroundColor: `${statusCfg.color}1A`,
          }}
        >
          {task.status === "done" && (
            <Check size={10} strokeWidth={3} style={{ color: statusCfg.color }} />
          )}
          {(task.status === "blocked" || task.status === "failed" || task.status === "aborted") && (
            <X size={10} strokeWidth={3} style={{ color: statusCfg.color }} />
          )}
          {isActive && (
            <div
              className="w-2 h-2 rounded-full"
              style={{ backgroundColor: statusCfg.color }}
            />
          )}
        </div>
      </div>

      {/* Title + pills */}
      <div className="flex-1 min-w-0">
        <div
          className="text-[17px] font-semibold leading-snug"
          style={{ color: C.textPrimary }}
        >
          {task.title}
        </div>
        <div className="flex items-center gap-1.5 mt-2 flex-wrap">
          {/* Status pill with pulsing dot for active */}
          <span className="inline-flex items-center gap-1">
            {isActive && (
              <StatusDot status="busy" size="sm" pulse />
            )}
            <Pill color={statusCfg.color}>{statusCfg.label}</Pill>
          </span>
          <Pill color={PRIORITY_COLORS[task.priority] ?? C.textMuted}>
            {task.priority}
          </Pill>
          {agent && (
            <Pill color={C.accent} variant="outline">
              {agent.emoji} {agent.name}
            </Pill>
          )}
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-1 shrink-0">
        {confirmDelete ? (
          <div className="flex items-center gap-1">
            {isActive && (
              <span className="text-[10px] mr-1" style={{ color: C.warning }}>
                Agent active!
              </span>
            )}
            <button
              onClick={onDelete}
              disabled={deleteLoading}
              className="px-2 py-1 rounded text-[10px] font-semibold transition-colors cursor-pointer"
              style={{
                backgroundColor: `${C.error}26`,
                color: C.error,
              }}
            >
              {deleteLoading ? "..." : "Delete anyway"}
            </button>
            <button
              onClick={() => setConfirmDelete(false)}
              className="px-2 py-1 rounded text-[10px] transition-colors cursor-pointer"
              style={{ color: C.textMuted }}
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            onClick={() => setConfirmDelete(true)}
            className="p-1 rounded transition-colors hover:bg-[rgba(255,255,255,0.05)] cursor-pointer"
            style={{ color: isActive ? C.warning : C.textMuted }}
            title={isActive ? "Agent is working — delete anyway?" : "Delete task"}
          >
            <Trash2 size={14} />
          </button>
        )}
        <button
          onClick={onClose}
          className="w-[30px] h-[30px] rounded-lg flex items-center justify-center transition-colors hover:bg-[rgba(255,255,255,0.05)] cursor-pointer"
          style={{
            color: C.textSecondary,
            background: "rgba(255, 255, 255, 0.03)",
            border: `1px solid ${C.border}`,
          }}
        >
          <X size={16} />
        </button>
      </div>
    </div>
  );
}
