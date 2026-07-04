"use client";

import { cn } from "@/lib/utils";
import { timeAgo } from "@/lib/utils";
import { StatusDot } from "./StatusDot";
import { Pill } from "./Pill";
import { C, LANE, STATUS } from "@/lib/colors";

interface ActivityEvent {
  id: string;
  title: string;
  agent_name?: string;
  agent_emoji?: string;
  event_type: string;
  created_at: string;
  from_status?: string;
  to_status?: string;
}

interface ActivityFeedProps {
  events: ActivityEvent[];
  className?: string;
}

type StatusType = "online" | "warning" | "error" | "busy" | "idle" | "offline";

const eventTypeToStatus: Record<string, StatusType> = {
  "task.created": "idle",
  "task.status_changed": "online",
  "task.completed": "online",
  "task.failed": "error",
  "agent.online": "online",
  "agent.offline": "offline",
  "agent.error": "error",
  "agent.provisioned": "online",
  "agent.created": "busy",
  // Phase 6 (CTX-03 + REC-03): compaction + tiered recovery audit events.
  // Backend emits these from session_monitor (compaction) and task_runner
  // (recovery tiers). See ADR-026.
  "agent.compaction": "warning",
  "agent.recovery_started": "warning",
  "agent.recovery_tier_complete": "online",
  "agent.recovery_failed": "error",
};

const statusColors: Record<string, string> = {
  inbox: LANE.inbox,          // C.textMuted — neutral
  in_progress: LANE.in_progress, // C.info
  review: LANE.review,        // C.warning
  done: LANE.done,            // C.online
  blocked: LANE.blocked,      // C.error
  failed: LANE.failed,        // C.error
  online: C.online,
  offline: STATUS.offline,
  error: C.error,
  busy: C.accent,             // was lila #8B5CF6 — busy = active work = teal
};

function getStatusForEvent(eventType: string): StatusType {
  return eventTypeToStatus[eventType] ?? "idle";
}

export function ActivityFeed({ events, className }: ActivityFeedProps) {
  if (!events.length) {
    return (
      <div className={cn("text-sm text-[var(--color-text-muted)] py-6 text-center", className)}>
        No activity
      </div>
    );
  }

  return (
    <div className={cn("relative", className)}>
      {/* Vertical timeline line */}
      <div
        aria-hidden
        className="absolute left-[3px] top-2 bottom-2 w-px bg-[var(--color-border-subtle)]"
      />

      <div className="space-y-4">
        {events.map((event) => (
          <div key={event.id} className="relative flex gap-3 pl-1">
            {/* Timeline dot */}
            <div className="relative z-10 mt-1.5 shrink-0">
              <StatusDot
                status={getStatusForEvent(event.event_type)}
                size="sm"
              />
            </div>

            {/* Content */}
            <div className="min-w-0 flex-1">
              <p className="text-sm text-[var(--color-text-primary)] leading-snug">
                {event.title}
              </p>

              {/* Status transition */}
              {event.from_status && event.to_status && (
                <div className="mt-1.5 flex items-center gap-1.5">
                  <Pill
                    color={statusColors[event.from_status] ?? C.textDim}
                    size="sm"
                  >
                    {event.from_status.replace("_", " ")}
                  </Pill>
                  <span className="text-[var(--color-text-muted)] text-xs">
                    →
                  </span>
                  <Pill
                    color={statusColors[event.to_status] ?? C.textDim}
                    size="sm"
                  >
                    {event.to_status.replace("_", " ")}
                  </Pill>
                </div>
              )}

              {/* Agent + time */}
              <div className="mt-1 flex items-center gap-2 text-xs text-[var(--color-text-muted)]">
                {event.agent_name && (
                  <span>
                    {event.agent_emoji && `${event.agent_emoji} `}
                    {event.agent_name}
                  </span>
                )}
                <span>{timeAgo(event.created_at)}</span>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
