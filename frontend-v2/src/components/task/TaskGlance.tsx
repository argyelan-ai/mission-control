"use client";

/**
 * TaskGlance — the top of the task cockpit (09/2026): what a person needs in
 * the first two seconds, without scrolling.
 *
 *   Summary      first paragraph of the description, de-markdowned
 *   Needs you    only when the task is waiting on the operator
 *   Numbers      subtasks · changes · open — three tiles, no more
 *   Checkpoints  milestones + progress notes from the timeline, newest first
 *
 * Everything shown here is derived (lib/taskGlance + lib/taskCheckpoints);
 * the technical fields stay in the Technical tab.
 */

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { humanStatus, plainSummary, silenceHours, type GlanceTone } from "@/lib/taskGlance";
import { deriveCheckpoints, type CheckpointTone } from "@/lib/taskCheckpoints";
import type { Task, TaskComment, TaskTimelineResponse } from "@/lib/types";

const TONE_COLOR: Record<GlanceTone | CheckpointTone, string> = {
  warning: C.warning,
  info: C.info,
  error: C.error,
  online: C.online,
  muted: C.textMuted,
  accent: C.accent,
};

const SILENCE_WARN_HOURS = 4;

export function GlanceStatusPill({ task }: { task: Task }) {
  const t = useTranslations("tasks");
  const { key, tone } = humanStatus(task);
  const color = TONE_COLOR[tone];
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] font-medium"
      style={{ background: `${color}1F`, border: `1px solid ${color}55`, color: tone === "error" ? STATUS_TEXT.error : tone === "info" ? STATUS_TEXT.info : color }}
    >
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: color }} />
      {t(key)}
    </span>
  );
}

export function TaskGlance({
  task,
  boardId,
  latestComment,
  subtasks,
  changes,
  onOpenDescription,
}: {
  task: Task;
  boardId: string;
  latestComment?: TaskComment | null;
  subtasks?: { done: number; total: number } | null;
  changes?: { commits: number; files: number; additions: number; deletions: number } | null;
  onOpenDescription?: () => void;
}) {
  const t = useTranslations("tasks");
  const locale = useLocale();
  const [showAllCheckpoints, setShowAllCheckpoints] = useState(false);

  const { data: timeline } = useQuery<TaskTimelineResponse>({
    queryKey: ["task-timeline", boardId, task.id],
    queryFn: () => api.tasks.timeline(boardId, task.id),
    refetchInterval: 30_000,
  });
  const checkpoints = deriveCheckpoints(timeline?.entries ?? [], { limit: showAllCheckpoints ? 30 : 5 });
  const hasMore = deriveCheckpoints(timeline?.entries ?? [], { limit: 6 }).length > 5;

  const summary = plainSummary(task.description);
  const { tone } = humanStatus(task);
  const needsYou = tone === "warning";
  const silence = silenceHours(task.last_activity_at ?? latestComment?.created_at ?? null);
  const stale = silence != null && silence >= SILENCE_WARN_HOURS && (task.status === "in_progress" || task.status === "waiting");

  const askLine = latestComment ? firstLine(latestComment.content) : null;

  return (
    <div className="px-4 pt-3 pb-2" style={{ borderBottom: `1px solid ${C.border}` }}>
      {/* Summary */}
      <div className="text-[10px] font-semibold uppercase tracking-[0.07em] mb-1" style={{ color: C.textDim }}>
        {t("glanceSummary")}
      </div>
      <p className="text-[13px] leading-relaxed m-0" style={{ color: summary ? C.textPrimary : C.textMuted }}>
        {summary || t("glanceNoSummary")}
      </p>
      {task.description && onOpenDescription && (
        <button
          type="button"
          onClick={onOpenDescription}
          className="mt-1 inline-flex items-center gap-0.5 text-[11px] cursor-pointer"
          style={{ color: C.textMuted, borderBottom: `1px dashed ${C.textDim}` }}
        >
          {t("techDescription")}
          <ChevronRight size={10} />
        </button>
      )}

      {/* Needs you */}
      {needsYou && (
        <div
          className="mt-3 rounded-lg px-3 py-2"
          style={{ background: C.bgSurface, border: `1px solid ${C.warning}55` }}
        >
          <div className="text-[10px] font-semibold uppercase tracking-[0.07em]" style={{ color: STATUS_TEXT.warning ?? C.warning }}>
            {t("glanceNeedsYou")}
          </div>
          <div className="text-xs mt-0.5" style={{ color: C.textPrimary }}>
            {askLine ?? t("glanceNeedsYouHint")}
          </div>
          {latestComment && (
            <div className="text-[11px] mt-0.5" style={{ color: C.textDim }}>
              {latestComment.author_agent_name ?? latestComment.author_type} · {timeAgo(latestComment.created_at, locale)}
            </div>
          )}
        </div>
      )}

      {/* Silence warning */}
      {stale && !needsYou && (
        <div className="mt-2 text-[11px]" style={{ color: STATUS_TEXT.warning ?? C.warning }}>
          {t("glanceSilence", { hours: silence })}
        </div>
      )}

      {/* Numbers */}
      <div className="grid grid-cols-3 gap-1.5 mt-3">
        <Tile label={t("glanceSubtasks")} value={subtasks ? `${subtasks.done}` : "—"} sub={subtasks ? `/ ${subtasks.total}` : undefined} bar={subtasks && subtasks.total ? subtasks.done / subtasks.total : undefined} />
        <Tile
          label={t("glanceChanges")}
          value={changes ? `${changes.commits}` : "—"}
          sub={changes ? `· ${t("changesFiles", { count: changes.files })}` : undefined}
        />
        <Tile
          label={t("glanceOpen")}
          value={needsYou ? t("glanceQuestion") : t("glanceNothingOpen")}
          tone={needsYou ? "warning" : undefined}
        />
      </div>

      {/* Checkpoints */}
      <div className="text-[10px] font-semibold uppercase tracking-[0.07em] mt-3 mb-1" style={{ color: C.textDim }}>
        {t("glanceCheckpoints")}
      </div>
      {checkpoints.length === 0 ? (
        <div className="text-[11px]" style={{ color: C.textMuted }}>{t("glanceNoCheckpoints")}</div>
      ) : (
        <ol className="m-0 p-0 list-none">
          {checkpoints.map((cp, i) => (
            <li
              key={`${cp.ts}-${i}`}
              className="flex items-baseline gap-2 py-1 text-xs"
              style={{ borderTop: i === 0 ? undefined : `1px solid ${C.borderSubtle}` }}
            >
              <span className="w-1.5 h-1.5 rounded-full shrink-0 relative top-[-1px]" style={{ background: TONE_COLOR[cp.tone] }} />
              <span className="flex-1 min-w-0 truncate" style={{ color: i === 0 ? C.textPrimary : C.textSecondary }} title={cp.text}>
                {cp.text}
              </span>
              <span className="shrink-0 font-mono text-[10px]" style={{ color: C.textDim }}>
                {timeAgo(cp.ts, locale)}
              </span>
            </li>
          ))}
        </ol>
      )}
      {hasMore && (
        <button
          type="button"
          onClick={() => setShowAllCheckpoints((v) => !v)}
          className="mt-1 text-[11px] cursor-pointer"
          style={{ color: C.textMuted }}
        >
          {showAllCheckpoints ? "−" : "+"} {timeline?.entries ? deriveCheckpoints(timeline.entries, { limit: 30 }).length - 5 : ""}
        </button>
      )}
    </div>
  );
}

function Tile({ label, value, sub, bar, tone }: { label: string; value: string; sub?: string; bar?: number; tone?: "warning" }) {
  return (
    <div className="rounded-lg px-2.5 py-2" style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}>
      <div className="text-[9px] font-semibold uppercase tracking-[0.07em]" style={{ color: C.textDim }}>{label}</div>
      <div className="text-[13px] font-semibold mt-0.5 truncate" style={{ color: tone === "warning" ? STATUS_TEXT.warning ?? C.warning : C.textPrimary }}>
        {value}
        {sub && <span className="font-normal text-[11px] ml-1" style={{ color: C.textDim }}>{sub}</span>}
      </div>
      {bar != null && (
        <div className="h-[3px] rounded-full mt-1.5 overflow-hidden" style={{ background: C.bgHover }}>
          <div className="h-full" style={{ width: `${Math.round(bar * 100)}%`, background: C.accent }} />
        </div>
      )}
    </div>
  );
}

function firstLine(content: string): string {
  const line = content
    .split("\n")
    .map((l) => l.trim())
    .find((l) => l && !/^#{1,6}\s/.test(l));
  return (line ?? "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/`([^`]*)`/g, "$1")
    .slice(0, 160);
}
