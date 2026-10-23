"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle, RotateCcw, Pause, StopCircle, Play } from "lucide-react";
import { api } from "@/lib/api";
import type { Task, ReviewDecision } from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";
import { PRIMARY_BTN, PRIMARY_STYLE, QUIET_BTN, RAISED } from "./detail/nextStepStyle";
import { isOperatorReview, isSelfReviewStall } from "@/lib/reviewRouting";
import { isAgentWorking } from "@/lib/agentStatus";
import { useTranslations } from "next-intl";
import { useKeyboardOpen } from "@/hooks/useKeyboardOpen";

// ── Review owner gate ────────────────────────────────────────────────────────
// Incident 11.09.2026: the operator saw Approve/Reject on every task in
// `review`, including those already with the reviewer agent, and approved
// mid-review. Decision buttons appear only when the review is the operator's
// (see lib/reviewRouting.ts); otherwise an explicit "agent is reviewing" note
// with an opt-in override.

function ReviewOwnerGate({ task, boardId, mirroredOnPhone }: { task: Task; boardId: string; mirroredOnPhone: boolean }) {
  const t = useTranslations("inbox");
  const [override, setOverride] = useState(false);
  const { data: agents, isError } = useQuery({
    queryKey: ["agents", boardId],
    queryFn: () => api.agents.list(boardId),
    staleTime: 60_000,
  });
  // Explicit operator request: never gated behind the agents lookup, so a
  // broken/hanging /agents call can't hide the decision UI (Incident PR #514 B1).
  if (task.human_review_required) {
    return <ReviewDecisionSection task={task} boardId={boardId} mirroredOnPhone={mirroredOnPhone} />;
  }
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
        <ReviewDecisionSection task={task} boardId={boardId} mirroredOnPhone={mirroredOnPhone} />
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
  mirroredOnPhone,
}: {
  task: Task;
  boardId: string;
  /** The phone action bar mirrors Approve (TaskDetailBody's DetailActionBar):
   *  then the bar's is the one Approve below md (K11, round-2 Q5 — two lit
   *  "Approve" on one screen). It comes back while the keyboard is up,
   *  because the bar steps aside then. */
  mirroredOnPhone: boolean;
}) {
  const t = useTranslations("tasks");
  const qc = useQueryClient();
  const [reviewComment, setReviewComment] = useState("");
  const keyboardOpen = useKeyboardOpen();
  const approveClass = mirroredOnPhone && !keyboardOpen ? `${PRIMARY_BTN} max-md:hidden` : PRIMARY_BTN;

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
        <button type="button" onClick={() => decide("approve")} disabled={off} className={approveClass} style={PRIMARY_STYLE}>
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
  /** The phone action bar mirrors this screen's primary (detail with onBack). */
  mirroredOnPhone?: boolean;
}

export function TaskActions({ task, boardId, primaryTaken = false, mirroredOnPhone = false }: TaskActionsProps) {
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

  // A review has a run only while an agent works on it. The operator's own
  // review (explicit request, or a card nobody holds) has nothing to stop —
  // "Stop run" there was a button without an effect (round-2 Q5, t4
  // N-neu-6). Neither has a card whose agent sits idle (round-3 t4 Q5 rest:
  // "in review · with <agent>", the agent idle) or works on another task.
  // A waiting review is quieted with Hold in the decision panel instead
  // (review_decision=hold stops the reminders like run_control does).
  //
  // rule: R-stop-only-own-run - Stop on review card B stopped run A
  // The run must be THIS card's run, by name. A worker's backend never points
  // current_task_id at a review card (it is set only for in_progress, the
  // heartbeat clears it for anything else), so "working, pointer empty" says
  // nothing about which card: with two review cards delivered to the same
  // reviewer, card B offered Stop and stopping it ended run A (round-4 K5,
  // r1 N2). Proof is the pointer on this card, or the bridge's run record
  // (run_task_id) naming it. Anything else — no button.
  const reviewByAgent =
    task.status === "review" && !!task.assigned_agent_id && !task.human_review_required && task.dispatched_at != null;
  const { data: agents } = useQuery({
    queryKey: ["agents", boardId],
    queryFn: () => api.agents.list(boardId),
    staleTime: 60_000,
    // The run starts and ends without the card changing: look again while
    // the button depends on it, so it does not stay a minute out of date.
    refetchInterval: reviewByAgent ? 15_000 : false,
    enabled: reviewByAgent,
  });
  // Unknown while the agents load: no button that may vanish a moment later.
  const reviewer = reviewByAgent ? agents?.find((a) => a.id === task.assigned_agent_id) : undefined;
  const agentHoldsReview =
    !!reviewer &&
    (isAgentWorking(reviewer.status) || reviewer.run_state === "running") &&
    (reviewer.current_task_id === task.id || reviewer.run_task_id === task.id);
  const hasActiveRun =
    task.status === "in_progress" ||
    (task.status === "inbox" && task.dispatched_at != null) ||
    agentHoldsReview;
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
        <ReviewOwnerGate task={task} boardId={boardId} mirroredOnPhone={mirroredOnPhone} />
      )}
    </div>
  );
}
