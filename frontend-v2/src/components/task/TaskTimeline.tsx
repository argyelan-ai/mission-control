"use client";

/**
 * TaskTimeline — the "Task Flight Recorder".
 *
 * One vertical, chronological list merging every event source the backend
 * already records for a task (status transitions, dispatch/recovery/review
 * activity, comments of all types, and field milestones) — so "why is this
 * task stuck?" is answerable by scrolling once instead of cross-referencing
 * Events + Comments + History separately.
 */

import {
  AlertOctagon,
  AlertTriangle,
  ArrowUpCircle,
  Ban,
  CheckCircle2,
  Eye,
  Flag,
  GitBranch,
  Info,
  LifeBuoy,
  MessageCircle,
  MessageSquare,
  PlayCircle,
  RefreshCw,
  Send,
  Sparkles,
  Users,
  type LucideIcon,
} from "lucide-react";
import { timeAgo } from "@/lib/utils";
import { C } from "@/lib/colors";
import type { TaskTimelineEntry } from "@/lib/types";

// ── Kind → icon/color/label ──────────────────────────────────────────────────

const DEFAULT_META: { icon: LucideIcon; color: string } = { icon: Info, color: C.textMuted };

const KIND_META: Record<string, { icon: LucideIcon; color: string; label?: string }> = {
  // Milestones
  created: { icon: Sparkles, color: C.textSecondary },
  dispatched: { icon: Send, color: C.info },
  acked: { icon: PlayCircle, color: C.info },
  blocked: { icon: Ban, color: C.error },

  // TaskEvent
  status_change: { icon: RefreshCw, color: C.accent },

  // ActivityEvent buckets
  dispatch: { icon: Send, color: C.info },
  recovery: { icon: LifeBuoy, color: C.warning, label: "Recovery" },
  review: { icon: Eye, color: C.warning },
  stuck: { icon: AlertTriangle, color: C.error },
  promote: { icon: ArrowUpCircle, color: C.accent },
  phase: { icon: Flag, color: C.accent },
  subtask: { icon: GitBranch, color: C.textSecondary },
  handoff: { icon: Users, color: C.accent },
  system: { icon: Info, color: C.textMuted },

  // Comment types
  progress: { icon: MessageSquare, color: C.accent },
  blocker: { icon: AlertOctagon, color: C.error, label: "Blocker" },
  feedback: { icon: MessageCircle, color: C.warning },
  checkpoint: { icon: Flag, color: C.accent },
  reflection: { icon: Sparkles, color: C.accent },
  resolution: { icon: CheckCircle2, color: C.online },
  message: { icon: MessageSquare, color: C.textSecondary },
};

function getKindMeta(kind: string): { icon: LucideIcon; color: string; label: string } {
  const meta = KIND_META[kind] ?? DEFAULT_META;
  return { ...meta, label: meta.label ?? kind.replace(/_/g, " ") };
}

const SOURCE_LABEL: Record<TaskTimelineEntry["source"], string> = {
  milestone: "Milestone",
  task_event: "Status",
  activity_event: "Activity",
  comment: "Comment",
};

// ── Row ──────────────────────────────────────────────────────────────────────

function TimelineRow({ entry, isLast }: { entry: TaskTimelineEntry; isLast: boolean }) {
  const { icon: Icon, color } = getKindMeta(entry.kind);
  const absolute = new Date(entry.ts).toLocaleString();

  return (
    <div className="relative pl-5" style={{ paddingBottom: isLast ? 0 : 14 }}>
      {!isLast && (
        <span
          className="absolute top-4 bottom-0 left-[5px] w-px"
          style={{ background: C.border }}
        />
      )}
      <span
        className="absolute left-0 top-0.5 flex items-center justify-center w-[11px] h-[11px] rounded-full"
        style={{ background: C.bgBase, border: `1.5px solid ${color}` }}
      />
      <div className="flex items-center gap-1.5">
        <Icon size={11} style={{ color, flexShrink: 0 }} />
        <span className="text-xs font-medium truncate" style={{ color: C.textPrimary }}>
          {entry.title}
        </span>
      </div>

      {entry.detail && (
        <div className="mt-1 text-[11px] leading-relaxed" style={{ color: C.textSecondary }}>
          {entry.detail}
        </div>
      )}

      <div className="flex items-center gap-1.5 mt-1 text-[10px] flex-wrap">
        {entry.actor && (
          <span
            className="inline-flex items-center rounded px-1.5 py-0.5"
            style={{ background: C.bgHover, color: C.textSecondary }}
          >
            {entry.actor}
          </span>
        )}
        <span style={{ color: C.textDim }}>{SOURCE_LABEL[entry.source]}</span>
        <span style={{ color: C.textDim }}>·</span>
        <span title={absolute} style={{ color: C.textMuted }}>
          {timeAgo(entry.ts)}
        </span>
      </div>
    </div>
  );
}

// ── TaskTimeline ─────────────────────────────────────────────────────────────

interface TaskTimelineProps {
  entries: TaskTimelineEntry[];
  isLoading: boolean;
  truncated?: boolean;
}

export function TaskTimeline({ entries, isLoading, truncated }: TaskTimelineProps) {
  if (isLoading) {
    return (
      <div className="text-xs" style={{ color: C.textMuted }}>
        Lade Timeline…
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="text-xs" style={{ color: C.textMuted }}>
        Noch keine Ereignisse.
      </div>
    );
  }

  // Most recent first — matches every other reverse-chronological list in
  // this panel (Comments, History), but each row's connector line still
  // reads top→bottom, so it's the newest event that sits at the top.
  const ordered = [...entries].reverse();

  return (
    <div>
      {truncated && (
        <div className="mb-2 text-[11px]" style={{ color: C.textMuted }}>
          Zeige die letzten {entries.length} Ereignisse — ältere ausgeblendet.
        </div>
      )}
      <div className="overflow-y-auto pr-1" style={{ maxHeight: 500 }}>
        {ordered.map((entry, i) => (
          <TimelineRow key={`${entry.source}-${entry.kind}-${entry.ts}-${i}`} entry={entry} isLast={i === ordered.length - 1} />
        ))}
      </div>
    </div>
  );
}
