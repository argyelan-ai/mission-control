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
import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { formatAbsolute, formatAge } from "@/lib/taskDetail/format";
import { groupTimelineEntries, type TimelineItem } from "@/lib/taskDetail/timelineGroups";
import { eventLabel } from "@/lib/taskDetail/eventLabel";
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

const SOURCE_LABEL_KEY: Record<TaskTimelineEntry["source"], string> = {
  milestone: "detail.sourceMilestone",
  task_event: "detail.sourceStatus",
  activity_event: "detail.sourceActivity",
  comment: "detail.sourceComment",
};

// ── Row ──────────────────────────────────────────────────────────────────────

function Connector({ isLast }: { isLast: boolean }) {
  return !isLast ? (
    <span className="absolute top-4 bottom-0 left-[5px] w-px" style={{ background: C.border }} />
  ) : null;
}

function Node({ color }: { color: string }) {
  return (
    <span
      className="absolute left-0 top-0.5 flex items-center justify-center w-[11px] h-[11px] rounded-full"
      style={{ background: C.bgBase, border: `1.5px solid ${color}` }}
    />
  );
}

function TimelineRow({ entry, isLast }: { entry: TaskTimelineEntry; isLast: boolean }) {
  const t = useTranslations("tasks");
  const locale = useLocale();
  const { icon: Icon, color } = getKindMeta(entry.kind);

  return (
    <div className="relative pl-5" style={{ paddingBottom: isLast ? 0 : 14 }}>
      <Connector isLast={isLast} />
      <Node color={color} />
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
            className="inline-flex items-center rounded-sm px-1.5 py-0.5"
            style={{ background: C.bgHover, color: C.textSecondary }}
          >
            {entry.actor}
          </span>
        )}
        <span style={{ color: C.textDim }}>{t(SOURCE_LABEL_KEY[entry.source])}</span>
        <span style={{ color: C.textDim }}>·</span>
        <span title={formatAbsolute(entry.ts, locale)} style={{ color: C.textMuted }}>
          {t("detail.ago", { age: formatAge(entry.ts, locale) ?? "—" })}
        </span>
      </div>
    </div>
  );
}

/** One row for a repeated system event: "Blocked reminder ×27 · first … · last …". */
function TimelineGroupRow({
  item,
  isLast,
}: {
  item: Extract<TimelineItem, { type: "group" }>;
  isLast: boolean;
}) {
  const t = useTranslations("tasks");
  const locale = useLocale();
  const [open, setOpen] = useState(false);
  const { icon: Icon, color } = getKindMeta(item.entries[item.entries.length - 1].kind);
  const label = eventLabel(t, item.eventType);

  return (
    <div className="relative pl-5" style={{ paddingBottom: isLast ? 0 : 14 }} data-testid="timeline-group">
      <Connector isLast={isLast} />
      <Node color={color} />
      <div className="flex items-center gap-1.5">
        <Icon size={11} style={{ color, flexShrink: 0 }} />
        <span className="text-xs font-medium truncate" style={{ color: C.textPrimary }}>
          {t("detail.repeated", { label, count: item.entries.length })}
        </span>
      </div>
      <div className="flex items-center gap-1.5 mt-1 text-[10px] flex-wrap" style={{ color: C.textMuted }}>
        <span>
          {t("detail.timelineFirstLast", {
            first: formatAge(item.first, locale) ?? "—",
            last: formatAge(item.last, locale) ?? "—",
          })}
        </span>
        <span style={{ color: C.textDim }}>·</span>
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="cursor-pointer hover:underline"
          style={{ color: C.textSecondary }}
        >
          {open ? t("detail.timelineHideEach") : t("detail.timelineShowEach")}
        </button>
      </div>
      {open && (
        <div className="mt-2">
          {[...item.entries].reverse().map((e, i, arr) => (
            <TimelineRow key={`${e.ts}-${i}`} entry={e} isLast={i === arr.length - 1} />
          ))}
        </div>
      )}
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
  const t = useTranslations("tasks");

  if (isLoading) {
    return (
      <div className="min-h-[200px] text-xs" style={{ color: C.textMuted }} aria-busy="true">
        {t("detail.timelineLoading")}
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="text-xs" style={{ color: C.textMuted }}>
        {t("detail.timelineEmpty")}
      </div>
    );
  }

  // Repeated system events (reminders) collapse into one row, then most
  // recent first — matches every other reverse-chronological list in this
  // panel (Comments, History). No inner scroll box: the panel scrolls.
  const ordered = groupTimelineEntries(entries).reverse();

  return (
    <div>
      {truncated && (
        <div className="mb-2 text-[11px]" style={{ color: C.textMuted }}>
          {t("detail.timelineTruncated", { count: entries.length })}
        </div>
      )}
      <div>
        {ordered.map((item, i) =>
          item.type === "group" ? (
            <TimelineGroupRow key={`group-${item.eventType}`} item={item} isLast={i === ordered.length - 1} />
          ) : (
            <TimelineRow
              key={`${item.entry.source}-${item.entry.kind}-${item.entry.ts}-${i}`}
              entry={item.entry}
              isLast={i === ordered.length - 1}
            />
          ),
        )}
      </div>
    </div>
  );
}
