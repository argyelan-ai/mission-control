"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle, RotateCcw, Pause, StopCircle, Play } from "lucide-react";
import { api } from "@/lib/api";
import type { Task, ReviewDecision } from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";
import { PRIMARY_BTN, PRIMARY_STYLE, QUIET_BTN, RAISED } from "./detail/nextStepStyle";
import { isOperatorReview, isSelfReviewStall } from "@/lib/reviewRouting";
import { useTranslations } from "next-intl";

// ── Review owner gate ────────────────────────────────────────────────────────
// Incident 11.09.2026: the operator saw Approve/Reject on every task in
// `review`, including those already with the reviewer agent, and approved
// mid-review. Decision buttons appear only when the review is the operator's
// (see lib/reviewRouting.ts); otherwise an explicit "agent is reviewing" note
// with an opt-in override.

function ReviewOwnerGate({ task, boardId }: { task: Task; boardId: string }) {
  const t = useTranslations("inbox");
  const [override, setOverride] = useState(false);
  const { data: agents, isError } = useQuery({
    queryKey: ["agents", boardId],
    queryFn: () => api.agents.list(boardId),
    staleTime: 60_000,
  });
  // Explicit operator request: never gated behind the agents lookup, so a
  // broken/hanging /agents call can't hide the decision UI (Incident PR #514 B1).
  if (task.human_review_required) return <ReviewDecisionSection task={task} boardId={boardId} />;
  const agent = task.assigned_agent_id ? agents?.find((a) => a.id === task.assigned_agent_id) : null;
  // While agents are still loading, an assigned reviewer must not flash the buttons.
  // A failed lookup (isError) must not loop forever — fall through to isOperatorReview instead.
  const loading = !!task.assigned_agent_id && agents === undefined && !isError;
  if (loading) return null;
  // W2 (PR #514 Rex review): reviewer === developer of this card → backend
  // skipped the handoff, nobody is independently reviewing it. isOperatorReview
  // already routes this to the decision section below; this only decides the
  // wording above it ("wartet auf Lead" instead of pretending nothing changed).
  const selfReviewStall = !!agent && agent.role_canonical === "reviewer" && isSelfReviewStall(task);
  if (override || isOperatorReview(task, agent)) {
    return (
      <div className="space-y-2">
        {selfReviewStall && !override && (
          <p className="text-sm" data-testid="self-review-stall-note" style={{ color: STATUS_TEXT.warning }}>
            {t("selfReviewStall", { agent: agent?.name ?? "—" })}
          </p>
        )}
        <ReviewDecisionSection task={task} boardId={boardId} />
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 flex-wrap text-sm" data-testid="agent-review-note" style={{ color: C.textSecondary }}>
      <span>{t("agentReviewing", { agent: agent?.name ?? "—" })}</span>
      <button type="button" onClick={() => setOverride(true)} className={QUIET_BTN} style={{ color: C.textPrimary }}>
        {t("decideYourself")}
      </button>
    </div>
  );
}

// ── Review Decision Section ──────────────────────────────────────────────────
// The operator's own review: one surface (they have to act), quick reasons,
// the reason field and ONE primary action (DESIGN.md K8/K11).

const DECISION_KEY: Record<ReviewDecision, string> = {
  approved: "actions.decisionApproved",
  changes_requested: "actions.decisionChanges",
  hold: "actions.decisionHold",
};
const QUICK_REASONS = ["actions.reasonLooksGood", "actions.reasonTestsPassed", "actions.reasonEvidence"] as const;

function ReviewDecisionSection({
  task,
  boardId,
}: {
  task: Task;
  boardId: string;
}) {
  const t = useTranslations("tasks");
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
      <p className="text-sm" style={{ color: C.textSecondary }}>
        {task.run_control === "stopped" ? t("actions.reviewBlockedStopped") : t("actions.reviewBlockedHeld")}
      </p>
    );
  }

  const reason = reviewComment.trim();
  const decide = (decision: "approve" | "request_changes" | "hold") => reason && reviewMutation.mutate({ decision, comment: reason });
  const off = reviewMutation.isPending || !reason;

  return (
    <section className="rounded-lg p-4 space-y-4" style={{ background: RAISED }} data-testid="review-decision">
      {task.review_decision && (
        <p className="text-sm" style={{ color: C.textMuted }}>
          {t("actions.lastDecision", { decision: t(DECISION_KEY[task.review_decision]) })}
        </p>
      )}
      <div className="flex gap-2 flex-wrap">
        {QUICK_REASONS.map((key) => {
          const text = t(key);
          const on = reviewComment === text;
          return (
            <button
              key={key}
              type="button"
              aria-pressed={on}
              onClick={() => setReviewComment(text)}
              className="h-11 px-4 rounded-full text-sm cursor-pointer transition-colors"
              style={{
                background: on ? C.accentSubtle : "var(--detail-bg, var(--color-bg-surface))",
                color: C.textPrimary,
                border: `1px solid ${on ? C.borderAccent : C.border}`,
              }}
            >
              {text}
            </button>
          );
        })}
      </div>
      <textarea
        value={reviewComment}
        onChange={(e) => setReviewComment(e.target.value)}
        placeholder={t("actions.reviewPlaceholder")}
        rows={2}
        aria-label={t("actions.reviewReasonLabel")}
        className="w-full px-3 py-2 rounded-md text-base @min-[560px]:text-sm resize-y"
        style={{ background: "var(--detail-bg, var(--color-bg-deep))", color: C.textPrimary, border: `1px solid ${C.border}` }}
      />
      <div className="flex items-center gap-2 flex-wrap">
        <button type="button" onClick={() => decide("approve")} disabled={off} className={PRIMARY_BTN} style={PRIMARY_STYLE}>
          <CheckCircle size={16} aria-hidden />
          {t("actions.approve")}
        </button>
        <button type="button" onClick={() => decide("request_changes")} disabled={off} className={QUIET_BTN} style={{ color: C.textSecondary }}>
          <RotateCcw size={16} aria-hidden />
          {t("actions.requestChanges")}
        </button>
        <button type="button" onClick={() => decide("hold")} disabled={off} className={QUIET_BTN} style={{ color: C.textSecondary }}>
          <Pause size={16} aria-hidden />
          {t("actions.hold")}
        </button>
      </div>
    </section>
  );
}

// ── TaskActions ──────────────────────────────────────────────────────────────

interface TaskActionsProps {
  task: Task;
  boardId: string;
  /** The header already shows a primary button (Reply, Open log, an
   *  approval) — then Requeue / Release step down to quiet (K11: one primary). */
  primaryTaken?: boolean;
}

export function TaskActions({ task, boardId, primaryTaken = false }: TaskActionsProps) {
  const t = useTranslations("tasks");
  const qc = useQueryClient();

  const promoteMutation = useMutation({
    mutationFn: () => api.tasks.promote(boardId, task.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
    },
  });

  // Stop asks first, inline — a mistap must not end a long run.
  const [confirmingStop, setConfirmingStop] = useState(false);

  const stopRunMutation = useMutation({
    mutationFn: () => api.tasks.stop(boardId, task.id, "Manual stop"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
    },
    onSettled: () => setConfirmingStop(false),
  });

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
  const mainBtn = primaryTaken ? QUIET_BTN : PRIMARY_BTN;
  const mainStyle = primaryTaken ? { color: C.textPrimary } : PRIMARY_STYLE;

  return (
    <div className="space-y-4">
      {/* Pre-dispatch gating: release a planned subtask */}
      {task.dispatch_phase === "planning" && task.parent_task_id && (
        <button type="button" onClick={() => promoteMutation.mutate()} disabled={promoteMutation.isPending} className={mainBtn} style={mainStyle}>
          {promoteMutation.isPending ? t("actions.promoting") : t("actions.promote")}
        </button>
      )}

      {/* Run control — quiet, asks first */}
      {hasActiveRun && !isStopped && (
        confirmingStop ? (
          <div className="flex items-center gap-2 flex-wrap" role="group" data-testid="stop-confirm">
            <span className="text-sm" style={{ color: C.textPrimary }}>{t("actions.stopConfirm")}</span>
            <button
              type="button"
              onClick={() => stopRunMutation.mutate()}
              disabled={stopRunMutation.isPending}
              className={QUIET_BTN}
              style={{ color: STATUS_TEXT.error }}
            >
              {stopRunMutation.isPending ? t("actions.stopping") : t("actions.stopConfirmYes")}
            </button>
            <button type="button" onClick={() => setConfirmingStop(false)} className={QUIET_BTN} style={{ color: C.textSecondary }}>
              {t("cancel")}
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setConfirmingStop(true)}
            className={`-ml-3 ${QUIET_BTN}`}
            style={{ color: C.textSecondary }}
            data-testid="stop-run"
          >
            <StopCircle size={16} aria-hidden />
            {t("actions.stopRun")}
          </button>
        )
      )}

      {isStopped && (
        <div className="space-y-2" data-testid="run-held">
          <p className="text-sm" style={{ color: C.textSecondary }}>
            {task.run_control === "stopped" ? t("actions.runStopped") : t("actions.runHeld")} {t("actions.requeueHint")}
          </p>
          <button type="button" onClick={() => resumeRunMutation.mutate()} disabled={resumeRunMutation.isPending} className={mainBtn} style={mainStyle}>
            <Play size={16} aria-hidden />
            {resumeRunMutation.isPending ? "…" : t("actions.requeue")}
          </button>
        </div>
      )}

      {/* Review — only the operator's own reviews get decision buttons; a
          review held by a reviewer agent is shown as such. */}
      {task.status === "review" && (
        <ReviewOwnerGate task={task} boardId={boardId} />
      )}
    </div>
  );
}
