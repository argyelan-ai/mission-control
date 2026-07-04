"use client";

import { useQuery, useQueries, useQueryClient, useMutation } from "@tanstack/react-query";
import AppShell from "@/components/layout/AppShell";
import { motion, AnimatePresence } from "framer-motion";
import { CheckCircle, Inbox, Clock } from "lucide-react";
import { api } from "@/lib/api";
import { useApprovalStream } from "@/lib/sse";
import { useAppStore } from "@/lib/store";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import { ApprovalCard } from "@/components/inbox/ApprovalCard";
import { ReviewTaskRow } from "@/components/inbox/ReviewTaskRow";
import { GlassCard } from "@/components/shared/GlassCard";
import { Pill } from "@/components/shared/Pill";
import type { Approval, Task, Agent } from "@/lib/types";

export default function InboxPage() {
  const qc = useQueryClient();
  const { activeBoardId } = useAppStore();

  // ── Data ─────────────────────────────────────────────────────────────────────

  const { data: approvals } = useQuery({
    queryKey: ["approvals"],
    queryFn: api.approvals.list,
    refetchInterval: 15_000,
  });

  const { data: reviewTasks } = useQuery({
    queryKey: ["review-tasks", activeBoardId],
    queryFn: () => api.tasks.list(activeBoardId!, { status: "review" }),
    enabled: !!activeBoardId,
    refetchInterval: 15_000,
  });

  const { data: agents } = useQuery({
    queryKey: ["agents", activeBoardId],
    queryFn: () => api.agents.list(activeBoardId ?? undefined),
    enabled: !!activeBoardId,
  });

  // SSE auto-refresh
  useApprovalStream(() => {
    qc.invalidateQueries({ queryKey: ["approvals"] });
    qc.invalidateQueries({ queryKey: ["review-tasks"] });
  });

  // Load comments for each review task to filter correctly
  const allReviews = reviewTasks ?? [];
  const commentQueries = useQueries({
    queries: allReviews.map((task) => ({
      queryKey: ["task-comments", activeBoardId, task.id],
      queryFn: () => api.tasks.comments.list(activeBoardId!, task.id),
      enabled: !!activeBoardId,
      staleTime: 30_000,
    })),
  });

  // ── Mutations ────────────────────────────────────────────────────────────────

  const resolveMutation = useMutation({
    mutationFn: ({ id, status, note }: { id: string; status: "approved" | "rejected"; note?: string }) =>
      api.approvals.resolve(id, status, note),
    onSuccess: (_, { status }) => {
      qc.invalidateQueries({ queryKey: ["approvals"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["review-tasks"] });
      notify.success(status === "approved" ? "Approved" : "Rejected");
    },
    onError: () => notify.error("Failed to resolve approval"),
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
      notify.success("Review decision saved");
    },
    onError: () => notify.error("Failed to save review decision"),
  });

  // ── Derived Data ─────────────────────────────────────────────────────────────

  const pendingApprovals = approvals ?? [];
  const agentMap = Object.fromEntries((agents ?? []).map((a) => [a.id, a]));

  // Filter: Only show tasks that are ready for the operator's review
  const reviews = allReviews.filter((task, i) => {
    if (!task.assigned_agent_id) return true;
    const comments = commentQueries[i]?.data;
    if (!comments) return false;
    return comments.some((c) => c.author_agent_id === task.assigned_agent_id);
  });

  const waitingForReview = allReviews.length - reviews.length;
  const totalCount = pendingApprovals.length + reviews.length;

  // ── Render ───────────────────────────────────────────────────────────────────

  return (
    <AppShell>
    <div className="flex flex-col gap-6 max-w-2xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-[var(--color-text-primary)]">
            Inbox
          </h1>
          {totalCount > 0 && (
            <p className="text-sm mt-1">
              <span
                className="font-semibold"
                style={{
                  color: C.warning,
                }}
              >
                {totalCount}
              </span>
              <span className="text-[var(--color-text-muted)]"> pending</span>
            </p>
          )}
        </div>
        {totalCount > 0 && (
          <Pill color={C.warning} size="md">
            {totalCount} open
          </Pill>
        )}
      </div>

      {/* Review Tasks section */}
      {reviews.length > 0 && (
        <section>
          <div className="flex items-center gap-2 mb-3">
            <span className="text-[var(--color-accent)]">
              <Inbox size={14} />
            </span>
            <span className="text-[11px] uppercase tracking-wider font-semibold text-[var(--color-text-muted)]">
              Tasks for review ({reviews.length})
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
              Approvals ({pendingApprovals.length})
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
      {waitingForReview > 0 && (
        <motion.div
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
        >
          <GlassCard className="px-4 py-3 flex items-center gap-2.5">
            <span className="text-base">&#9203;</span>
            <span className="text-[12px] text-[var(--color-text-muted)]">
              {waitingForReview === 1
                ? "1 task still awaiting review"
                : `${waitingForReview} tasks still awaiting review`}
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
                backgroundColor: `${C.online}14`,
                border: `1px solid ${C.online}26`,
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
                All clear
              </p>
              <p className="text-[12px] text-[var(--color-text-muted)] mt-1">
                No open approvals or reviews
              </p>
            </div>
          </GlassCard>
        </motion.div>
      )}
    </div>
    </AppShell>
  );
}
