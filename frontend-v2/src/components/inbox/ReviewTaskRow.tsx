"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import {
  CheckCircle, XCircle, ChevronDown, ChevronRight,
  ExternalLink, MessageSquare, Pause, UserCheck,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import { useLocale, useTranslations } from "next-intl";
import { cn, timeAgo } from "@/lib/utils";
import { api } from "@/lib/api";
import { GlassCard } from "@/components/shared/GlassCard";
import { Pill } from "@/components/shared/Pill";
import { CommentCard } from "@/components/task/CommentCard";
import { C } from "@/lib/colors";
import Link from "next/link";
import type { Task, Agent } from "@/lib/types";
import { EntityIcon } from "@/components/shared/EntityIcon";

// ── Review Task Row ──────────────────────────────────────────────────────────

interface ReviewTaskRowProps {
  task: Task;
  boardId: string;
  agent?: Agent;
  agentMap: Record<string, Agent>;
  onDecision: (decision: "approve" | "request_changes" | "hold", comment: string) => void;
  loading?: boolean;
}

export function ReviewTaskRow({
  task,
  boardId,
  agent,
  agentMap,
  onDecision,
  loading,
}: ReviewTaskRowProps) {
  const t = useTranslations("inbox");
  const locale = useLocale();
  const [expanded, setExpanded] = useState(false);
  const [showRejectInput, setShowRejectInput] = useState(false);
  const [rejectComment, setRejectComment] = useState("");
  const [showApproveInput, setShowApproveInput] = useState(false);
  const [approveComment, setApproveComment] = useState("");

  const { data: comments } = useQuery({
    queryKey: ["task-comments", boardId, task.id],
    queryFn: () => api.tasks.comments.list(boardId, task.id),
    enabled: expanded,
  });

  const priorityColor =
    task.priority === "critical" ? C.error :
    task.priority === "high" ? C.warning :
    C.textMuted;

  return (
    <motion.div
      layout
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 8, height: 0 }}
    >
      <GlassCard className="p-4">
        {/* Header */}
        <div
          className="flex items-start justify-between gap-4 cursor-pointer"
          onClick={() => setExpanded(!expanded)}
        >
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <Pill color={C.accent} size="sm">review</Pill>
              {task.human_review_required && (
                <span
                  className="text-[10px] font-medium px-2 py-1 rounded-sm inline-flex items-center gap-1"
                  style={{
                    color: C.accent,
                    backgroundColor: `${C.accent}1A`,
                    border: `1px solid ${C.accent}40`,
                  }}
                >
                  <UserCheck size={10} /> {t("yourReview")}
                </span>
              )}
              <span
                className="text-[10px] uppercase font-semibold"
                style={{ color: priorityColor }}
              >
                {task.priority}
              </span>
              {agent && (
                <span className="text-[11px] text-[var(--color-text-muted)]">
                  <EntityIcon value={agent.emoji} size={14} className="inline-block align-[-2px] mr-1" />{agent.name}
                </span>
              )}
              <span className="text-[10px] ml-auto text-[var(--color-text-muted)]">
                {timeAgo(task.updated_at, locale)}
              </span>
            </div>
            <div className="flex items-center gap-2 mt-2">
              {expanded ? (
                <ChevronDown size={14} className="text-[var(--color-text-muted)] shrink-0" />
              ) : (
                <ChevronRight size={14} className="text-[var(--color-text-muted)] shrink-0" />
              )}
              <p className="text-sm font-medium text-[var(--color-text-primary)]">
                {task.title}
              </p>
            </div>
            {!expanded && task.description && (
              <p className="text-[11px] mt-1 ml-5 line-clamp-2 text-[var(--color-text-secondary)]">
                {task.description}
              </p>
            )}
          </div>
        </div>

        {/* Expanded */}
        <AnimatePresence>
          {expanded && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              {/* Description */}
              {task.description && (
                <div
                  className="mt-3 ml-5 p-3 rounded-xl prose-description"
                  style={{
                    backgroundColor: "var(--color-bg-surface)",
                    border: "1px solid var(--color-border-subtle)",
                  }}
                >
                  <ReactMarkdown>{task.description}</ReactMarkdown>
                </div>
              )}

              {/* Comments */}
              {comments && comments.length > 0 && (
                <div className="mt-3 ml-5 flex flex-col gap-2">
                  <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-muted)] mb-1">
                    {t("historyCount", { count: comments.length })}
                  </div>
                  {comments.map((c) => (
                    <CommentCard key={c.id} comment={c} agentMap={agentMap} />
                  ))}
                </div>
              )}

              {comments && comments.length === 0 && (
                <div className="mt-3 ml-5 text-[11px] text-[var(--color-text-muted)]">
                  {t("noCommentsYet")}
                </div>
              )}

              {/* Link to tasks */}
              <div className="mt-3 ml-5">
                <Link
                  href="/tasks"
                  className="inline-flex items-center gap-1 text-[11px] transition-colors"
                  style={{ color: C.accent }}
                >
                  <ExternalLink size={11} /> {t("viewInTasks")}
                </Link>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Actions */}
        <div
          className="flex flex-col gap-2 mt-3 pt-3 border-t"
          style={{ borderColor: "var(--color-border)" }}
        >
          {/* Review Decision Badge */}
          {task.review_decision && (
            <div
              className="text-[10px] font-medium px-2 py-1 rounded-lg inline-flex items-center gap-1 self-start"
              style={{
                color:
                  task.review_decision === "approved" ? C.online :
                  task.review_decision === "hold" ? C.warning :
                  C.error,
                backgroundColor:
                  task.review_decision === "approved" ? `${C.online}1A` :
                  task.review_decision === "hold" ? `${C.warning}1A` :
                  `${C.error}1A`,
              }}
            >
              {task.review_decision === "approved" ? t("decisionApproved") :
               task.review_decision === "hold" ? t("decisionHold") :
               t("decisionChanges")}
            </div>
          )}

          <div className="flex items-center gap-2">
            <button
              onClick={() => {
                setShowApproveInput(!showApproveInput);
                setShowRejectInput(false);
              }}
              disabled={loading}
              className="flex items-center gap-1.5 text-[12px] px-3.5 py-2 rounded-xl cursor-pointer transition-all disabled:opacity-50"
              style={{
                backgroundColor: `${C.online}1F`,
                color: C.online,
                border: `1px solid ${C.online}40`,
              }}
            >
              <CheckCircle size={13} /> {t("approve")}
            </button>
            <button
              onClick={() => {
                setShowRejectInput(!showRejectInput);
                setShowApproveInput(false);
              }}
              disabled={loading}
              className="flex items-center gap-1.5 text-[12px] px-3.5 py-2 rounded-xl cursor-pointer transition-all disabled:opacity-50"
              style={{
                backgroundColor: `${C.error}1F`,
                color: C.error,
                border: `1px solid ${C.error}40`,
              }}
            >
              <XCircle size={13} /> {t("reject")}
            </button>
            <button
              onClick={() => onDecision("hold", "On hold -- waiting for clarification")}
              disabled={loading}
              className="flex items-center gap-1.5 text-[12px] px-3.5 py-2 rounded-xl cursor-pointer transition-all disabled:opacity-50"
              style={{
                backgroundColor: `${C.warning}1F`,
                color: C.warning,
                border: `1px solid ${C.warning}40`,
              }}
            >
              <Pause size={13} /> {t("hold")}
            </button>
          </div>

          {/* Approve input */}
          {showApproveInput && (
            <div className="flex gap-2">
              <input
                value={approveComment}
                onChange={(e) => setApproveComment(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && approveComment.trim()) {
                    onDecision("approve", approveComment.trim());
                    setApproveComment("");
                    setShowApproveInput(false);
                  }
                }}
                placeholder={t("approveReasonPlaceholder")}
                aria-label={t("approveComment")}
                className="flex-1 px-3 py-2 rounded-xl text-[12px] outline-none"
                style={{
                  backgroundColor: "var(--color-bg-surface)",
                  color: "var(--color-text-primary)",
                  border: "1px solid var(--color-border)",
                }}
                autoFocus
              />
              <button
                onClick={() => {
                  if (approveComment.trim()) {
                    onDecision("approve", approveComment.trim());
                    setApproveComment("");
                    setShowApproveInput(false);
                  }
                }}
                className="p-2 rounded-xl cursor-pointer"
                style={{ backgroundColor: C.online, color: C.onAccent }}
              >
                <CheckCircle size={14} />
              </button>
            </div>
          )}

          {/* Reject input */}
          {showRejectInput && (
            <div className="flex gap-2">
              <input
                value={rejectComment}
                onChange={(e) => setRejectComment(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && rejectComment.trim()) {
                    onDecision("request_changes", rejectComment.trim());
                    setRejectComment("");
                    setShowRejectInput(false);
                  }
                }}
                placeholder={t("rejectFeedbackPlaceholder")}
                aria-label={t("rejectComment")}
                className="flex-1 px-3 py-2 rounded-xl text-[12px] outline-none"
                style={{
                  backgroundColor: "var(--color-bg-surface)",
                  color: "var(--color-text-primary)",
                  border: "1px solid var(--color-border)",
                }}
                autoFocus
              />
              <button
                onClick={() => {
                  if (rejectComment.trim()) {
                    onDecision("request_changes", rejectComment.trim());
                    setRejectComment("");
                    setShowRejectInput(false);
                  }
                }}
                className="p-2 rounded-xl cursor-pointer"
                style={{ backgroundColor: C.error, color: C.onAccent }}
              >
                <MessageSquare size={14} />
              </button>
            </div>
          )}
        </div>
      </GlassCard>
    </motion.div>
  );
}
