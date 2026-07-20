"use client";

import { timeAgo } from "@/lib/utils";
import { Pill } from "@/components/shared/Pill";
import type { TaskEvent } from "@/lib/types";
import { C, LANE } from "@/lib/colors";

// ── Status colors ────────────────────────────────────────────────────────────

const statusColors: Record<string, string> = {
  inbox: LANE.inbox,
  in_progress: LANE.in_progress,
  review: LANE.review,
  user_test: LANE.user_test,
  waiting: LANE.waiting,
  done: LANE.done,
  blocked: LANE.blocked,
  failed: LANE.failed,
  aborted: LANE.aborted,
};

function getTaskEventTitle(event: TaskEvent) {
  if (event.title) return event.title;
  return `Status geaendert: ${String(event.from_status).replace(/_/g, " ")} -> ${String(
    event.to_status
  ).replace(/_/g, " ")}`;
}

// ── TaskHistory ──────────────────────────────────────────────────────────────

interface TaskHistoryProps {
  events: TaskEvent[];
  isLoading: boolean;
}

export function TaskHistory({ events, isLoading }: TaskHistoryProps) {
  if (isLoading) {
    return (
      <div className="text-xs" style={{ color: C.textMuted }}>
        Lade History...
      </div>
    );
  }

  if (events.length === 0) {
    return (
      <div className="text-xs" style={{ color: C.textMuted }}>
        Noch keine Task-Events vorhanden.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {events.map((event) => (
        <div
          key={event.id}
          className="rounded-lg p-3"
          style={{
            backgroundColor: "rgba(255, 255, 255, 0.02)",
            border: `1px solid ${C.border}`,
          }}
        >
          <div className="text-xs font-medium" style={{ color: C.textPrimary }}>
            {getTaskEventTitle(event)}
          </div>

          {/* Status transition pills */}
          {event.from_status && event.to_status && (
            <div className="mt-1.5 flex items-center gap-1.5">
              <Pill color={statusColors[event.from_status] ?? C.textMuted} size="sm">
                {String(event.from_status).replace("_", " ")}
              </Pill>
              <span className="text-xs" style={{ color: C.textMuted }}>&rarr;</span>
              <Pill color={statusColors[event.to_status] ?? C.textMuted} size="sm">
                {String(event.to_status).replace("_", " ")}
              </Pill>
            </div>
          )}

          <div className="flex items-center gap-2 mt-1 text-[11px] flex-wrap">
            <span style={{ color: C.textSecondary }}>
              {event.agent_name || event.changed_by}
            </span>
            <span style={{ color: C.textMuted }}>·</span>
            <span style={{ color: C.textMuted }}>
              {timeAgo(event.created_at)}
            </span>
            {event.reason && (
              <>
                <span style={{ color: C.textMuted }}>·</span>
                <span style={{ color: C.textMuted }}>{event.reason}</span>
              </>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
