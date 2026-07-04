"use client";

import { motion } from "framer-motion";
import { AlertTriangle, Loader2, Pause, Play, Square, Trash2 } from "lucide-react";
import { C } from "@/lib/colors";
import type { Loop } from "@/lib/types";
import { canPauseLoop, canStartLoop, canStopLoop, isLoopInactive, LOOP_STATUS_META } from "./loopMeta";

interface LoopCardProps {
  loop: Loop;
  onOpen: () => void;
  onStart: () => void;
  onPause: () => void;
  onStop: () => void;
  onDelete: () => void;
  actionPending?: boolean;
}

export function LoopCard({ loop, onOpen, onStart, onPause, onStop, onDelete, actionPending }: LoopCardProps) {
  const meta = LOOP_STATUS_META[loop.status];
  const roundNo = loop.current_round_no ?? loop.rounds_completed;
  const hasMax = typeof loop.max_rounds === "number" && loop.max_rounds > 0;
  const progressPct = hasMax ? Math.min(100, (loop.rounds_completed / loop.max_rounds!) * 100) : 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
      className="flex flex-col gap-3 rounded-xl px-4 py-3.5"
      style={{ background: C.borderSubtle, border: `1px solid ${C.borderSubtle}` }}
    >
      <button
        type="button"
        onClick={onOpen}
        className="flex flex-col gap-2 text-left cursor-pointer w-full"
      >
        <div className="flex items-start justify-between gap-3">
          <span className="text-sm font-medium truncate" style={{ color: C.textPrimary }}>
            {loop.name}
          </span>
          <span
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide"
            style={{ background: `${meta.color}22`, border: `1px solid ${meta.color}55`, color: meta.textColor }}
          >
            {loop.status === "running" && (
              <span
                className="w-1.5 h-1.5 rounded-full animate-pulse"
                style={{ background: meta.color }}
                aria-hidden
              />
            )}
            {meta.label}
          </span>
        </div>

        {loop.goal && (
          <p className="text-xs line-clamp-2" style={{ color: C.textSecondary }}>
            {loop.goal}
          </p>
        )}

        <div className="flex items-center gap-2">
          <span className="text-[11px] font-mono shrink-0" style={{ color: C.textMuted }}>
            {hasMax ? `Round ${roundNo} of ${loop.max_rounds}` : `Round ${roundNo} · open-ended`}
          </span>
          {hasMax && (
            <div className="flex-1 h-1 rounded-full overflow-hidden" style={{ background: C.border }}>
              <div
                className="h-full rounded-full"
                style={{ width: `${progressPct}%`, background: C.accent }}
              />
            </div>
          )}
        </div>

        {loop.consecutive_failed_rounds > 0 && (
          <div className="flex items-center gap-1.5 text-[11px]" style={{ color: C.warning }}>
            <AlertTriangle size={11} />
            {loop.consecutive_failed_rounds} failed round{loop.consecutive_failed_rounds === 1 ? "" : "s"} in a row
          </div>
        )}
      </button>

      <div className="flex items-center gap-1.5 pt-1" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
        <div className="flex items-center gap-1.5 pt-2 flex-1">
          {canStartLoop(loop.status) && (
            <ActionButton
              label="Start"
              icon={<Play size={12} />}
              onClick={onStart}
              disabled={actionPending}
              tone={C.accent}
            />
          )}
          {canPauseLoop(loop.status) && (
            <ActionButton
              label="Pause"
              icon={<Pause size={12} />}
              onClick={onPause}
              disabled={actionPending}
              tone={C.warning}
            />
          )}
          {canStopLoop(loop.status) && (
            <ActionButton
              label="Stop"
              icon={<Square size={12} />}
              onClick={onStop}
              disabled={actionPending}
              tone={C.error}
            />
          )}
          {actionPending && <Loader2 size={12} className="animate-spin" style={{ color: C.textMuted }} />}
        </div>
        {isLoopInactive(loop.status) && (
          <button
            type="button"
            aria-label={`Delete ${loop.name}`}
            onClick={onDelete}
            disabled={actionPending}
            className="flex items-center justify-center rounded-md p-1.5 mt-2 cursor-pointer transition-colors disabled:opacity-50"
            style={{ color: C.textMuted }}
          >
            <Trash2 size={13} />
          </button>
        )}
      </div>
    </motion.div>
  );
}

function ActionButton({
  label,
  icon,
  onClick,
  disabled,
  tone,
}: {
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  tone: string;
}) {
  return (
    <button
      type="button"
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
