"use client";

import { CircleDot, ArrowUpRight } from "lucide-react";
import type { TaskCardData } from "./types";
import { CardShell } from "./CardShell";
import { C, LANE } from "@/lib/colors";

const STATUS_COLORS: Record<string, string> = {
  inbox: LANE.inbox,
  in_progress: LANE.in_progress,
  blocked: LANE.blocked,
  review: LANE.review,
  done: LANE.done,
  failed: LANE.failed,
};

const PRIO_COLORS: Record<string, string> = {
  critical: C.error,
  high: C.warning,
  medium: C.textMuted,
  low: "transparent",
};

/**
 * TaskCard — one MC task as a glanceable strip with status dot, assignee,
 * priority bar, and a link into the TaskDetail page.
 */
export function TaskCard({
  data,
  title,
  onClose,
  onPreview,
}: {
  data: TaskCardData;
  title?: string | null;
  onClose: () => void;
  onPreview: () => void;
}) {
  const displayTitle = title || data.title || "Task";
  const statusColor = STATUS_COLORS[data.status || ""] || C.textMuted;
  const prioColor = PRIO_COLORS[data.priority || ""] || "transparent";

  return (
    <CardShell
      onClose={onClose}
      icon={
        <CircleDot
          size={13}
          style={{ color: statusColor }}
        />
      }
      kind="task"
      meta={data.assignee || undefined}
    >
      <button
        type="button"
        onClick={onPreview}
        className="flex items-start gap-1.5 group min-w-0 flex-1 text-left cursor-pointer w-full"
      >
        {/* priority bar */}
        {prioColor !== "transparent" && (
          <div
            className="w-[2px] self-stretch rounded-full shrink-0"
            style={{ background: prioColor }}
          />
        )}
        <div className="min-w-0 flex-1">
          <div
            className="text-[11px] font-medium leading-snug truncate group-hover:text-white transition-colors"
            style={{ color: "var(--color-text-primary)" }}
          >
            {displayTitle}
          </div>
          <div className="flex items-center gap-1.5 mt-0.5">
            <span
              className="text-[9px] uppercase tracking-wider"
              style={{ color: statusColor, letterSpacing: "0.06em" }}
            >
              {data.status || "?"}
            </span>
            {data.assignee && (
              <>
                <span style={{ color: "var(--color-text-muted)" }}>·</span>
                <span
                  className="text-[10px] truncate"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  {data.assignee}
                </span>
              </>
            )}
          </div>
        </div>
        <ArrowUpRight
          size={11}
          className="mt-0.5 opacity-50 group-hover:opacity-100 shrink-0"
          style={{ color: "var(--color-text-secondary)" }}
        />
      </button>
    </CardShell>
  );
}
