"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle, RotateCcw, Pause, StopCircle, Play } from "lucide-react";
import { api } from "@/lib/api";
import type { Task, TaskStatus, ReviewDecision } from "@/lib/types";
import { C } from "@/lib/colors";

// ── Review Decision Section ──────────────────────────────────────────────────

function ReviewDecisionSection({
  task,
  boardId,
}: {
  task: Task;
  boardId: string;
}) {
  const qc = useQueryClient();
  const [reviewComment, setReviewComment] = useState("");

  const reviewMutation = useMutation({
    mutationFn: (body: { decision: "approve" | "request_changes" | "hold"; comment: string }) =>
      api.tasks.review(boardId, task.id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
      qc.invalidateQueries({ queryKey: ["task-comments", task.id] });
      setReviewComment("");
    },
  });

  const canReview = task.run_control !== "stopped" && task.run_control !== "manual_hold";

  if (!canReview) {
    return (
      <div
        className="px-3 py-2 rounded-lg text-xs"
        style={{
          backgroundColor: `${C.error}0F`,
          color: C.error,
          border: `1px solid ${C.error}26`,
        }}
      >
        Review blockiert -- Task ist {task.run_control === "stopped" ? "gestoppt" : "gehalten"}
      </div>
    );
  }

  const decisionLabels: Record<ReviewDecision, { label: string; color: string }> = {
    approved: { label: "Approved", color: C.online },
    changes_requested: { label: "Changes Requested", color: C.warning },
    hold: { label: "On Hold", color: C.warning },
  };

  return (
    <div
      className="rounded-lg p-3 space-y-2.5"
      style={{
        backgroundColor: "rgba(255, 255, 255, 0.02)",
        border: `1px solid ${C.border}`,
      }}
    >
      <div className="flex items-center justify-between">
        <span
          className="text-[10px] font-semibold uppercase tracking-[0.06em]"
          style={{ color: C.textMuted }}
        >
          Review
        </span>
        {task.review_decision && (
          <span
            className="text-[10px] font-medium px-1.5 py-0.5 rounded"
            style={{
              color: decisionLabels[task.review_decision].color,
              backgroundColor: `${decisionLabels[task.review_decision].color}26`,
            }}
          >
            {decisionLabels[task.review_decision].label}
          </span>
        )}
      </div>

      {/* Quick-Reasons */}
      <div className="flex gap-1.5 mb-2">
        {["Sieht gut aus", "Tests bestanden", "Evidence geprueft"].map((reason) => (
          <button
            key={reason}
            type="button"
            onClick={() => setReviewComment(reason)}
            className="px-2 py-1 rounded text-[10px] font-medium transition-colors cursor-pointer"
            style={{
              backgroundColor: C.bgElevated,
              color: C.textSecondary,
              border: `1px solid ${C.border}`,
            }}
          >
            {reason}
          </button>
        ))}
      </div>

      <textarea
        value={reviewComment}
        onChange={(e) => setReviewComment(e.target.value)}
        placeholder="Begruendung (Pflicht)..."
        rows={2}
        aria-label="Review-Begruendung"
        className="w-full px-2.5 py-2 rounded-lg text-xs outline-none resize-none"
        style={{
          backgroundColor: "rgba(255, 255, 255, 0.03)",
          color: C.textPrimary,
          border: `1px solid ${C.border}`,
        }}
      />

      <div className="flex items-center gap-1.5">
        <button
          onClick={() => reviewComment.trim() && reviewMutation.mutate({ decision: "approve", comment: reviewComment.trim() })}
          disabled={reviewMutation.isPending || !reviewComment.trim()}
          className="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg cursor-pointer transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          style={{
            backgroundColor: `${C.online}1A`,
            color: C.online,
            border: `1px solid ${C.online}33`,
          }}
        >
          <CheckCircle size={12} /> Approve
        </button>
        <button
          onClick={() => reviewComment.trim() && reviewMutation.mutate({ decision: "request_changes", comment: reviewComment.trim() })}
          disabled={reviewMutation.isPending || !reviewComment.trim()}
          className="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg cursor-pointer transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          style={{
            backgroundColor: `${C.error}1A`,
            color: C.error,
            border: `1px solid ${C.error}33`,
          }}
        >
          <RotateCcw size={12} /> Changes
        </button>
        <button
          onClick={() => reviewComment.trim() && reviewMutation.mutate({ decision: "hold", comment: reviewComment.trim() })}
          disabled={reviewMutation.isPending || !reviewComment.trim()}
          className="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg cursor-pointer transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          style={{
            backgroundColor: `${C.warning}1A`,
            color: C.warning,
            border: `1px solid ${C.warning}33`,
          }}
        >
          <Pause size={12} /> Hold
        </button>
      </div>
    </div>
  );
}

// ── TaskActions ──────────────────────────────────────────────────────────────

interface TaskActionsProps {
  task: Task;
  boardId: string;
}

export function TaskActions({ task, boardId }: TaskActionsProps) {
  const qc = useQueryClient();

  const updateMutation = useMutation({
    mutationFn: (data: Partial<Task>) => api.tasks.update(boardId, task.id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
      qc.invalidateQueries({ queryKey: ["task", boardId, task.id] });
    },
  });

  const promoteMutation = useMutation({
    mutationFn: () => api.tasks.promote(boardId, task.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
    },
  });

  const [confirmingStop, setConfirmingStop] = useState(false);
  const confirmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (confirmTimerRef.current) clearTimeout(confirmTimerRef.current);
    };
  }, []);

  const stopRunMutation = useMutation({
    mutationFn: () => api.tasks.stop(boardId, task.id, "Manual stop"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
    },
    onSettled: () => {
      setConfirmingStop(false);
      if (confirmTimerRef.current) {
        clearTimeout(confirmTimerRef.current);
        confirmTimerRef.current = null;
      }
    },
  });

  const handleStopClick = () => {
    if (confirmingStop) {
      stopRunMutation.mutate();
      return;
    }
    setConfirmingStop(true);
    if (confirmTimerRef.current) clearTimeout(confirmTimerRef.current);
    confirmTimerRef.current = setTimeout(() => {
      setConfirmingStop(false);
      confirmTimerRef.current = null;
    }, 4000);
  };

  const resumeRunMutation = useMutation({
    mutationFn: () => api.tasks.resume(boardId, task.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
    },
  });

  const hasActiveRun =
    task.status === "in_progress" ||
    (task.status === "inbox" && task.dispatched_at != null) ||
    task.status === "review";
  const isStopped = task.run_control === "stopped" || task.run_control === "manual_hold";

  return (
    <div className="space-y-3">
      {/* Pre-Dispatch Gating: Promote */}
      {task.dispatch_phase === "planning" && task.parent_task_id && (
        <button
          onClick={() => promoteMutation.mutate()}
          disabled={promoteMutation.isPending}
          className="flex items-center gap-1.5 w-full px-3 py-2 rounded-lg text-xs font-medium transition-colors cursor-pointer"
          style={{
            backgroundColor: C.accentSubtle,
            color: C.accent,
            border: `1px solid ${C.borderAccent}`,
          }}
        >
          {promoteMutation.isPending ? "Freigeben..." : "Freigeben"}
        </button>
      )}

      {/* Run Control — 2-Stufen-Confirm gegen versehentliches Stoppen */}
      {hasActiveRun && !isStopped && (
        <button
          onClick={handleStopClick}
          disabled={stopRunMutation.isPending}
          aria-pressed={confirmingStop}
          className="flex items-center gap-1.5 w-full px-3 py-2 rounded-lg text-xs font-medium transition-colors cursor-pointer"
          style={{
            backgroundColor: confirmingStop
              ? `${C.error}2E`
              : `${C.error}0F`,
            color: confirmingStop ? C.textPrimary : C.error,
            border: `1px solid ${
              confirmingStop ? `${C.error}80` : `${C.error}26`
            }`,
          }}
        >
          {confirmingStop ? <AlertTriangle size={12} /> : <StopCircle size={12} />}
          {stopRunMutation.isPending
            ? "Stopping..."
            : confirmingStop
              ? "Wirklich stoppen? Erneut klicken"
              : "Stop Run"}
        </button>
      )}

      {isStopped && (
        <div className="space-y-1.5">
          <div
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs"
            style={{
              backgroundColor: `${C.error}0F`,
              color: C.error,
              border: `1px solid ${C.error}26`,
            }}
          >
            <StopCircle size={12} />
            <span className="font-medium">
              Run {task.run_control === "stopped" ? "gestoppt" : "gehalten"}
            </span>
          </div>
          <button
            onClick={() => resumeRunMutation.mutate()}
            disabled={resumeRunMutation.isPending}
            className="flex items-center gap-1.5 w-full px-3 py-2 rounded-lg text-xs font-medium transition-colors cursor-pointer"
            style={{
              backgroundColor: `${C.online}0F`,
              color: C.online,
              border: `1px solid ${C.online}26`,
            }}
          >
            <Play size={12} />
            {resumeRunMutation.isPending ? "..." : "Requeue"}
          </button>
          <div className="text-[10px]" style={{ color: C.textMuted }}>
            Task geht zurueck in die Queue und wird im naechsten Dispatch-Zyklus neu zugewiesen.
          </div>
        </div>
      )}

      {/* Review Section */}
      {task.status === "review" && (
        <ReviewDecisionSection task={task} boardId={boardId} />
      )}

      {/* Status Change Buttons */}
      <div>
        <div
          className="text-[10px] font-semibold uppercase tracking-[0.06em] mb-2"
          style={{ color: C.textMuted }}
        >
          Change Status
        </div>
        <div className="flex flex-wrap gap-1.5">
          {(
            ["inbox", "in_progress", "review", "user_test", "done", "blocked", "failed"] as TaskStatus[]
          ).map((s) => (
            <button
              key={s}
              onClick={() => updateMutation.mutate({ status: s })}
              disabled={task.status === s}
              className="px-2 py-1 rounded-lg text-xs transition-colors disabled:opacity-30 cursor-pointer disabled:cursor-not-allowed"
              style={{
                backgroundColor: task.status === s
                  ? C.accentSubtle
                  : "rgba(255, 255, 255, 0.03)",
                color: task.status === s ? C.accent : C.textSecondary,
                border: `1px solid ${task.status === s ? C.borderAccent : C.border}`,
              }}
            >
              {s.replace("_", " ")}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
