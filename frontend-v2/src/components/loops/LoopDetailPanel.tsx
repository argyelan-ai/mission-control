"use client";

import ReactMarkdown from "react-markdown";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowUpRight, Loader2, Pause, Play, Square, Trash2 } from "lucide-react";
import { SlideOverPanel } from "@/components/shared/SlideOverPanel";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { LoopRound } from "@/lib/types";
import { canPauseLoop, canStartLoop, canStopLoop, isLoopInactive, LOOP_STATUS_META } from "./loopMeta";

interface LoopDetailPanelProps {
  loopId: string | null;
  open: boolean;
  onClose: () => void;
  onStart: (id: string) => void;
  onPause: (id: string) => void;
  onStop: (id: string) => void;
  onDelete: (id: string) => void;
  actionPending?: boolean;
}

const BACKLOG_SOURCE_LABEL: Record<string, string> = {
  markdown: "Markdown list",
  project: "Project tasks",
  tag: "Tag",
  open_ended: "Open-ended",
};

export function LoopDetailPanel({
  loopId,
  open,
  onClose,
  onStart,
  onPause,
  onStop,
  onDelete,
  actionPending,
}: LoopDetailPanelProps) {
  const { data: loop, isLoading } = useQuery({
    queryKey: ["loop", loopId],
    queryFn: () => api.loops.get(loopId!),
    enabled: open && !!loopId,
  });

  return (
    <SlideOverPanel open={open} onClose={onClose} title={loop?.name ?? "Loop"} desktopWidth="480px">
      {isLoading && (
        <div className="flex items-center gap-2 px-5 py-6" style={{ color: C.textMuted }}>
          <Loader2 size={14} className="animate-spin" />
          <span className="text-xs">Loading loop...</span>
        </div>
      )}

      {loop && (
        <div className="flex flex-col gap-5 px-5 py-4">
          {/* Status + actions */}
          <div className="flex items-center justify-between gap-3">
            <span
              className="inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide"
              style={{
                background: `${LOOP_STATUS_META[loop.status].color}22`,
                border: `1px solid ${LOOP_STATUS_META[loop.status].color}55`,
                color: LOOP_STATUS_META[loop.status].textColor,
              }}
            >
              {LOOP_STATUS_META[loop.status].label}
            </span>
            <div className="flex items-center gap-1.5">
              {canStartLoop(loop.status) && (
                <PanelActionButton icon={<Play size={12} />} label="Start" tone={C.accent} onClick={() => onStart(loop.id)} disabled={actionPending} />
              )}
              {canPauseLoop(loop.status) && (
                <PanelActionButton icon={<Pause size={12} />} label="Pause" tone={C.warning} onClick={() => onPause(loop.id)} disabled={actionPending} />
              )}
              {canStopLoop(loop.status) && (
                <PanelActionButton icon={<Square size={12} />} label="Stop" tone={C.error} onClick={() => onStop(loop.id)} disabled={actionPending} />
              )}
              {isLoopInactive(loop.status) && (
                <PanelActionButton icon={<Trash2 size={12} />} label="Delete" tone={C.textMuted} onClick={() => onDelete(loop.id)} disabled={actionPending} />
              )}
            </div>
          </div>

          {loop.last_error && (
            <div
              className="flex items-start gap-2 rounded-md px-3 py-2 text-xs"
              style={{ background: `${C.error}14`, border: `1px solid ${C.error}55`, color: C.error }}
            >
              <AlertTriangle size={13} className="mt-0.5 shrink-0" />
              <span>{loop.last_error}</span>
            </div>
          )}

          {/* Goal */}
          <section className="flex flex-col gap-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: C.textMuted }}>
              Goal
            </h3>
            <div
              className="prose prose-invert prose-sm max-w-none text-sm leading-relaxed"
              style={{ color: C.textSecondary }}
            >
              <ReactMarkdown>{loop.goal}</ReactMarkdown>
            </div>
          </section>

          {/* Configuration */}
          <section className="flex flex-col gap-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: C.textMuted }}>
              Configuration
            </h3>
            <div className="grid grid-cols-2 gap-2 text-xs">
              <ConfigRow label="Backlog" value={BACKLOG_SOURCE_LABEL[loop.backlog_source] ?? loop.backlog_source} />
              <ConfigRow label="Max rounds" value={loop.max_rounds ? String(loop.max_rounds) : "Unlimited"} />
              <ConfigRow label="Human gate every" value={loop.human_every_n_rounds > 0 ? `${loop.human_every_n_rounds} rounds` : "Never"} />
              <ConfigRow label="Pause after" value={`${loop.pause_on_failed_rounds} failed rounds`} />
              <ConfigRow label="Max duration" value={loop.max_duration_minutes ? `${loop.max_duration_minutes} min` : "No limit"} />
              <ConfigRow label="Stop on empty backlog" value={loop.stop_on_backlog_empty ? "Yes" : "No"} />
            </div>
          </section>

          {/* Round history */}
          <section className="flex flex-col gap-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: C.textMuted }}>
              Rounds ({loop.rounds.length})
            </h3>
            {loop.rounds.length === 0 ? (
              <p className="text-xs" style={{ color: C.textDim }}>No rounds yet.</p>
            ) : (
              <div className="flex flex-col gap-2">
                {[...loop.rounds].reverse().map((round) => (
                  <RoundCard key={round.id} round={round} />
                ))}
              </div>
            )}
          </section>
        </div>
      )}
    </SlideOverPanel>
  );
}

function RoundCard({ round }: { round: LoopRound }) {
  const outcomeMeta =
    round.outcome === "done"
      ? { label: "Done", color: C.online }
      : round.outcome === "failed"
        ? { label: "Failed", color: C.error }
        : { label: "In progress", color: C.accent };

  return (
    <div className="flex flex-col gap-1.5 rounded-lg px-3 py-2.5" style={{ background: C.bgSurface, border: `1px solid ${C.borderSubtle}` }}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium" style={{ color: C.textPrimary }}>
          Round {round.round_no}
        </span>
        <span
          className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide"
          style={{ background: `${outcomeMeta.color}22`, color: outcomeMeta.color }}
        >
          {outcomeMeta.label}
        </span>
      </div>

      {round.report && (
        <div className="prose prose-invert prose-sm max-w-none text-xs leading-relaxed" style={{ color: C.textSecondary }}>
          <ReactMarkdown>{round.report}</ReactMarkdown>
        </div>
      )}

      <Link
        href={`/tasks?taskId=${round.task_id}`}
        className="inline-flex items-center gap-1 text-[11px] w-fit"
        style={{ color: C.accent }}
      >
        View task
        <ArrowUpRight size={11} />
      </Link>
    </div>
  );
}

function ConfigRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5 rounded-md px-2.5 py-2" style={{ background: C.bgSurface, border: `1px solid ${C.borderSubtle}` }}>
      <span className="text-[10px] uppercase tracking-wide" style={{ color: C.textDim }}>{label}</span>
      <span className="font-medium" style={{ color: C.textPrimary }}>{value}</span>
    </div>
  );
}

function PanelActionButton({
  icon,
  label,
  tone,
  onClick,
  disabled,
}: {
  icon: React.ReactNode;
  label: string;
  tone: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11px] font-medium cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
      style={{ background: `${tone}1A`, border: `1px solid ${tone}4D`, color: tone }}
    >
      {icon}
      {label}
    </button>
  );
}
