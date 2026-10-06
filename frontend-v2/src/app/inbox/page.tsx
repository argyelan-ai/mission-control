"use client";

import { useQueryClient, useMutation } from "@tanstack/react-query";
import Link from "next/link";
import AppShell from "@/components/layout/AppShell";
import { motion, AnimatePresence } from "framer-motion";
import { useTranslations } from "next-intl";
import { CheckCircle, Inbox, Clock, MessageCircleQuestion, ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { useApprovalStream } from "@/lib/sse";
import { useAppStore } from "@/lib/store";
import { notify } from "@/lib/notify";
import { C, alpha } from "@/lib/colors";
import { pairShort } from "@/lib/heads";
import { useHeadRuns } from "@/components/heads/useHeadRuns";
import { ApprovalCard } from "@/components/inbox/ApprovalCard";
import { ReviewTaskRow } from "@/components/inbox/ReviewTaskRow";
import { GlassCard } from "@/components/shared/GlassCard";
import { Pill } from "@/components/shared/Pill";
import { useInbox } from "@/hooks/useInbox";

export default function InboxPage() {
  const t = useTranslations("inbox");
  const tHeads = useTranslations("heads");
  const qc = useQueryClient();
  const { activeBoardId } = useAppStore();
  // One shared fetch (heads-sichtbar PR 3, bauplan §4: "ein Abruf für die
  // ganze Liste") — looks up which review card a head finished, without a
  // query per row. The head-questions bucket below has its own run objects
  // already (`useInbox`'s own `heads.list()` call, unbounded by recency —
  // an open question must never age out of sight) and needs no lookup.
  const { byTask: headByTask } = useHeadRuns();

  // ── Data ─────────────────────────────────────────────────────────────────────
  // One source for what waits on the operator (lib/inbox.ts): the phone tab
  // bar's badge reads the same hook, so the badge always matches this page.
  const {
    reviews,
    agentReviews,
    waitingForReview,
    approvals: pendingApprovals,
    headQuestions,
    count: totalCount,
    agentMap,
  } = useInbox();

  // SSE auto-refresh
  useApprovalStream(() => {
    qc.invalidateQueries({ queryKey: ["approvals"] });
    qc.invalidateQueries({ queryKey: ["review-tasks"] });
  });

  // ── Mutations ────────────────────────────────────────────────────────────────

  const resolveMutation = useMutation({
    mutationFn: ({ id, status, note }: { id: string; status: "approved" | "rejected"; note?: string }) =>
      api.approvals.resolve(id, status, note),
    onSuccess: (_, { status }) => {
      qc.invalidateQueries({ queryKey: ["approvals"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["review-tasks"] });
      notify.success(status === "approved" ? t("approvedNotify") : t("rejectedNotify"));
    },
    onError: () => notify.error(t("resolveFailed")),
  });

  const reviewMutation = useMutation({
    mutationFn: ({
      taskId,
      decision,
      comment,
    }: {
      taskId: string;
      decision: "approve" | "request_changes" | "hold";
      comment: string;
    }) => api.tasks.review(activeBoardId!, taskId, { decision, comment }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["review-tasks"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["pipeline"] });
      notify.success(t("reviewSaved"));
    },
    onError: () => notify.error(t("reviewSaveFailed")),
  });

  // ── Render ───────────────────────────────────────────────────────────────────

  return (
    <AppShell>
    <div className="flex flex-col gap-6 max-w-2xl">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="label-sys mb-2">{t("approvalsInbox")}</div>
          <h1 className="display text-2xl font-semibold text-[var(--color-text-primary)]">
            {t("title")}
          </h1>
          {totalCount > 0 && (
            <p className="text-[13px] mt-1">
              <span
                className="font-semibold"
                style={{
                  color: C.warning,
                }}
              >
                {totalCount}
              </span>
              <span className="text-[var(--color-text-secondary)]"> {t("pending")}</span>
            </p>
          )}
        </div>
        {totalCount > 0 && (
          <Pill color={C.warning} size="md">
            {t("openCount", { count: totalCount })}
          </Pill>
        )}
      </div>

      {/* Head questions — a head stopped and asks the operator. Answered on
          the task detail (HeadStateCard: answer field + continue). */}
      {headQuestions.length > 0 && (
        <section data-testid="inbox-head-questions">
          <div className="flex items-center gap-2 mb-3">
            <span className="text-[var(--color-accent)]">
              <MessageCircleQuestion size={14} />
            </span>
            <span className="text-xs uppercase tracking-wider font-semibold text-[var(--color-text-muted)]">
              {t("headQuestionsCount", { count: headQuestions.length })}
            </span>
          </div>
          <div className="flex flex-col gap-2">
            {headQuestions.map((run) => (
              <Link
                key={run.run_id}
                href={`/sessions?head=${encodeURIComponent(run.run_id)}`}
                className="block cursor-pointer"
                data-testid="inbox-head-question"
              >
                <GlassCard className="px-4 py-3 flex items-center gap-3 min-h-11">
                  <div className="flex-1 min-w-0">
                    <div className="text-sm truncate text-[var(--color-text-primary)]">
                      {run.title || t("headQuestionUntitled")}
                    </div>
                    {/* "Head · <pair>" (bauplan PR 3 §4) — the question text
                        below already fills the row; the state itself
                        ("needs you") is redundant with this whole section's
                        own heading, so only the pair is named here (K3). */}
                    <div className="text-xs truncate text-[var(--color-text-muted)] mt-1">
                      {tHeads("runs.fact")} · {pairShort(run)}
                    </div>
                    {run.question && (
                      <div className="text-xs line-clamp-2 text-[var(--color-text-secondary)] mt-1">
                        {run.question}
                      </div>
                    )}
                  </div>
                  <span className="text-xs shrink-0 text-[var(--color-text-muted)]">{t("headQuestionAnswer")}</span>
                  <ChevronRight size={16} className="shrink-0 text-[var(--color-text-muted)]" aria-hidden />
                </GlassCard>
              </Link>
            ))}
          </div>
        </section>
      )}

      {/* Review Tasks section */}
      {reviews.length > 0 && (
        <section>
          <div className="flex items-center gap-2 mb-3">
            <span className="text-[var(--color-accent)]">
              <Inbox size={14} />
            </span>
            <span className="text-[11px] uppercase tracking-wider font-semibold text-[var(--color-text-muted)]">
              {t("tasksForReview", { count: reviews.length })}
            </span>
          </div>
          <div className="flex flex-col gap-3">
            <AnimatePresence>
              {reviews.map((task) => (
                <ReviewTaskRow
                  key={task.id}
                  task={task}
                  boardId={activeBoardId!}
                  agent={task.assigned_agent_id ? agentMap[task.assigned_agent_id] : undefined}
                  agentMap={agentMap}
                  headRun={headByTask.get(task.id) ?? null}
                  onDecision={(decision, comment) =>
                    reviewMutation.mutate({ taskId: task.id, decision, comment })
                  }
                  loading={reviewMutation.isPending}
                />
              ))}
            </AnimatePresence>
          </div>
        </section>
      )}

      {/* Approvals section */}
      {pendingApprovals.length > 0 && (
        <section>
          <div className="flex items-center gap-2 mb-3">
            <span style={{ color: C.warning }}>
              <Clock size={14} />
            </span>
            <span className="text-[11px] uppercase tracking-wider font-semibold text-[var(--color-text-muted)]">
              {t("approvalsCount", { count: pendingApprovals.length })}
            </span>
          </div>
          <div className="flex flex-col gap-3">
            <AnimatePresence>
              {pendingApprovals.map((approval) => (
                <ApprovalCard
                  key={approval.id}
                  approval={approval}
                  onResolve={(status, note) =>
                    resolveMutation.mutate({ id: approval.id, status, note })
                  }
                  loading={resolveMutation.isPending}
                />
              ))}
            </AnimatePresence>
          </div>
        </section>
      )}

      {/* Waiting for review hint */}
      {/* Reviews held by a reviewer agent — read-only, no operator buttons */}
      {agentReviews.length > 0 && (
        <section>
          <div className="flex items-center gap-2 mb-3">
            <span className="text-[var(--color-text-muted)]">
              <Clock size={14} />
            </span>
            <span className="text-[11px] uppercase tracking-wider font-semibold text-[var(--color-text-muted)]">
              {t("agentReviewsCount", { count: agentReviews.length })}
            </span>
          </div>
          <div className="flex flex-col gap-2">
            {agentReviews.map((task) => (
              <GlassCard key={task.id} className="px-4 py-3" data-testid="agent-review-row">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-[13px] text-[var(--color-text-primary)] truncate">{task.title}</span>
                  <span className="text-[11px] shrink-0 text-[var(--color-text-muted)]">
                    {t("agentReviewing", {
                      agent: (task.assigned_agent_id && agentMap[task.assigned_agent_id]?.name) || "—",
                    })}
                    {task.review_decision === "hold" && (
                      <span className="ml-2" style={{ color: C.warning }} data-testid="agent-review-hold">
                        {t("agentReviewOnHold")}
                      </span>
                    )}
                  </span>
                </div>
              </GlassCard>
            ))}
          </div>
        </section>
      )}

      {waitingForReview > 0 && (
        <motion.div
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
        >
          <GlassCard className="px-4 py-3 flex items-center gap-2.5">
            <span className="text-base">&#9203;</span>
            <span className="text-[12px] text-[var(--color-text-muted)]">
              {t("awaitingReview", { count: waitingForReview })}
            </span>
          </GlassCard>
        </motion.div>
      )}

      {/* Empty state */}
      {totalCount === 0 && (
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0.1 }}
        >
          <GlassCard className="text-center py-20 flex flex-col items-center gap-4">
            <div
              className="w-16 h-16 rounded-2xl flex items-center justify-center"
              style={{
                backgroundColor: alpha(C.online, 0.08),
                border: `1px solid ${alpha(C.online, 0.15)}`,
              }}
            >
              <CheckCircle
                size={28}
                style={{ color: C.online }}
                className="opacity-60"
              />
            </div>
            <div>
              <p className="text-sm font-medium text-[var(--color-text-secondary)]">
                {t("allClear")}
              </p>
              <p className="text-[12px] text-[var(--color-text-muted)] mt-1">
                {t("noOpenItems")}
              </p>
            </div>
          </GlassCard>
        </motion.div>
      )}
    </div>
    </AppShell>
  );
}
