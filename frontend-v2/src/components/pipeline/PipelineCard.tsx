"use client";

import { CheckCircle, XCircle, Lock, Pause } from "lucide-react";
import { SpotlightCard } from "@/components/shared/SpotlightCard";
import type { PipelineTask } from "@/lib/types";
import { C } from "@/lib/colors";

// ── Priority helpers ──────────────────────────────────────────────────────────

type LaneKey = "inbox" | "in_progress" | "review" | "user_test" | "blocked" | "failed" | "aborted" | "done";

function priorityStripeGradient(priority: string, isDone: boolean): string {
  // Done = calm: heavily muted stripe instead of full green — the saturated
  // C.online clashed with the health/KPI greens on Home (operator, Jun 11)
  if (isDone) return `linear-gradient(180deg, ${C.online}59, ${C.online}26)`;
  switch (priority) {
    case "critical": return `linear-gradient(180deg, ${C.error}, ${C.error}CC)`;
    case "high": return `linear-gradient(180deg, ${C.warning}, ${C.warning}CC)`;
    case "medium": return `linear-gradient(180deg, ${C.textMuted}, ${C.textDim})`;
    default: return "transparent";
  }
}

function priorityGlowColor(priority: string): string | null {
  // No colored glows — flat design rule. Only subtle structural radial kept for visual depth.
  switch (priority) {
    case "critical": return `${C.error}1A`;
    case "high": return `${C.warning}1A`;
    default: return null;
  }
}

// ── PipelineCard ──────────────────────────────────────────────────────────────

interface PipelineCardProps {
  task: PipelineTask;
  laneKey: LaneKey;
  onClick: () => void;
}

export function PipelineCard({ task, laneKey, onClick }: PipelineCardProps) {
  const isBlocked = laneKey === "blocked";
  const isFailed = laneKey === "failed";
  const isDone = laneKey === "done";

  const glowColor = priorityGlowColor(task.priority);

  const bgColor = isBlocked || isFailed
    ? C.bgSurface
    : task.has_blocked_deps
    ? C.bgSurface
    : isDone
    ? C.bgSurface
    : C.bgSurface;

  const borderColor = isBlocked || isFailed
    ? `${C.error}40`
    : task.has_blocked_deps
    ? `${C.error}26`
    : isDone
    ? `${C.online}26`
    : C.border;

  return (
    <SpotlightCard
      as="button"
      onClick={onClick}
      className="shrink-0 rounded-lg text-left group pipeline-card"
      style={{
        transition: "transform 0.3s cubic-bezier(0.16, 1, 0.3, 1)",
      }}
    >
      <div
        className="relative flex flex-col gap-1.5 p-3 rounded-lg h-full overflow-hidden"
        style={{
          background: bgColor,
          border: `1px solid ${borderColor}`,
          borderLeft: "none",
        }}
      >
        {/* 3px priority stripe — intentional design, kept */}
        <div
          aria-hidden
          className="absolute left-0 top-0 bottom-0 w-[3px] rounded-l-lg"
          style={{ background: priorityStripeGradient(task.priority, isDone) }}
        />

        {/* Priority tint in top-left — structural only, no colored glow */}
        {glowColor && !isDone && (
          <div
            aria-hidden
            className="absolute top-0 left-0 w-20 h-20 pointer-events-none"
            style={{
              background: `radial-gradient(circle at 0% 0%, ${glowColor}, transparent 70%)`,
            }}
          />
        )}

        {/* Title (2 lines max) */}
        <div
          className="text-[13px] font-medium leading-snug relative z-10"
          style={{
            color: isDone ? C.textSecondary : C.textPrimary,
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
          }}
        >
          {task.title}
        </div>

        {/* Footer */}
        <div
          className="flex items-center gap-1 mt-auto pt-1.5 relative z-10"
          style={{ borderTop: `1px solid ${C.border}` }}
        >
          {/* Agent info */}
          {task.agent ? (
            isDone ? (
              <span
                className="flex items-center gap-1 text-[10px] truncate"
                style={{ color: C.textMuted, maxWidth: 90 }}
              >
                <CheckCircle size={10} className="shrink-0" style={{ color: C.online }} />
                {task.agent.name}
              </span>
            ) : isFailed ? (
              <span
                className="flex items-center gap-1 text-[10px] truncate"
                style={{ color: C.error, maxWidth: 90 }}
              >
                <XCircle size={10} className="shrink-0" />
                {task.agent.name}
              </span>
            ) : (
              <span
                className="text-[10px] truncate"
                style={{ color: C.textMuted, maxWidth: 80 }}
              >
                {task.agent.emoji} {task.agent.name}
              </span>
            )
          ) : (
            <span className="text-[10px]" style={{ color: C.textMuted }}>
              --
            </span>
          )}

          {task.has_blocked_deps && (
            <Lock size={9} className="shrink-0" style={{ color: C.warning }} />
          )}

          {laneKey === "review" && task.review_decision === "hold" && (
            <Pause size={9} className="shrink-0" style={{ color: C.warning }} />
          )}

          {/* Dispatch phase badges */}
          {task.dispatch_phase === "planning" && (
            <span
              className="text-[10px] font-semibold tracking-wider uppercase shrink-0 px-1 py-0.5 rounded"
              style={{ color: C.accentHover, backgroundColor: C.accentSubtle }}
            >
              PLAN
            </span>
          )}
          {task.dispatch_phase === "ready" && (
            <span
              className="text-[10px] font-semibold tracking-wider uppercase shrink-0 px-1 py-0.5 rounded"
              style={{ color: C.online, backgroundColor: `${C.online}1F` }}
            >
              READY
            </span>
          )}

          {/* Tags + Priority — right-aligned */}
          <div className="flex items-center gap-1 ml-auto overflow-hidden shrink-0">
            {task.tags && task.tags.length > 0 && (
              <>
                {task.tags.slice(0, 2).map((tag, i) => (
                  <span
                    key={i}
                    className="text-[10px] px-1.5 py-0.5 rounded leading-none shrink-0"
                    style={{
                      color: tag.color || C.textSecondary,
                      backgroundColor: `${tag.color || C.textSecondary}26`,
                    }}
                  >
                    {tag.name}
                  </span>
                ))}
                {task.tags.length > 2 && (
                  <span className="text-[10px] shrink-0" style={{ color: C.textMuted }}>
                    +{task.tags.length - 2}
                  </span>
                )}
              </>
            )}
            {(task.priority === "critical" || task.priority === "high") && (
              <span
                className="text-[10px] px-1 py-0.5 rounded uppercase tracking-wide font-semibold shrink-0"
                style={{
                  color: task.priority === "critical" ? C.error : C.warning,
                  backgroundColor: task.priority === "critical"
                    ? `${C.error}26`
                    : `${C.warning}26`,
                }}
              >
                {task.priority === "critical" ? "CRIT" : "HIGH"}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Sizing + Hover via CSS */}
      <style>{`
        .pipeline-card { width: 180px; min-width: 180px; height: 120px; }
        @media (min-width: 768px) { .pipeline-card { width: 210px; min-width: 210px; height: 130px; } }
        .pipeline-card:hover { transform: translateY(-2px) scale(1.02); }
      `}</style>
    </SpotlightCard>
  );
}
