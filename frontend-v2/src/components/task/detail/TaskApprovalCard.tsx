"use client";

/**
 * The open approval inside the task detail's next step (DESIGN.md K8/K11).
 * Same approval, same resolve path and the same words as the inbox card
 * (inbox.* strings), in the calm header language:
 *
 *   What the agent asks — clamped, markdown          (one surface, no nested boxes)
 *   Show full ▾   → details: blocker kind, report, options, screenshots
 *   [Instruction for the agent …]                     (blocker / question only)
 *   [Unblock]  Cancel task                            (one primary, one quiet)
 *
 * The type badge, the autonomy pill and the time are not repeated here: the
 * state sentence above already says who asks since when. Install requests and
 * X posts keep their own inbox cards.
 */

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import { useTranslations } from "next-intl";
import { ChevronDown, ExternalLink } from "lucide-react";
import { C } from "@/lib/colors";
import { ApprovalCard, BLOCKER_TYPE_LABELS, INSTALL_ACTION_TYPES, leadText } from "@/components/inbox/ApprovalCard";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import type { Approval } from "@/lib/types";
import { useOverflows } from "@/hooks/useOverflows";
import { FADE, NEXT_TEXT, PRIMARY_BTN, PRIMARY_STYLE, QUIET_BTN, RAISED } from "./nextStepStyle";

type BlockerPayload = {
  blocked_agent_name?: string;
  blocker_type?: string;
  description?: string;
  question?: string;
  blocker_comment?: string;
};
type QuestionPayload = { question?: string; options?: string[] };
type VisualPayload = { screenshots?: string[]; preview_url?: string };

export function TaskApprovalCard({
  approval,
  onResolve,
  loading,
}: {
  approval: Approval;
  onResolve: (status: "approved" | "rejected", note?: string) => void;
  loading?: boolean;
}) {
  const t = useTranslations("inbox");
  const [note, setNote] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const { ref: leadRef, overflows } = useOverflows<HTMLDivElement>(expanded);

  // Special kinds keep the inbox card — they carry their own previews.
  if (INSTALL_ACTION_TYPES.has(approval.action_type) || approval.action_type === "x_post") {
    return <ApprovalCard approval={approval} onResolve={onResolve} loading={loading} />;
  }

  const isBlocker = approval.action_type === "blocker_decision";
  const isQuestion = approval.action_type === "clarification_question";
  const lead = leadText(approval);
  const blocker = isBlocker ? ((approval.payload ?? {}) as BlockerPayload) : null;
  const question = isQuestion ? ((approval.payload ?? {}) as QuestionPayload) : null;
  const visual = approval.action_type === "visual_review" ? ((approval.payload ?? {}) as VisualPayload) : null;
  const blockerKind = blocker ? (BLOCKER_TYPE_LABELS[blocker.blocker_type ?? "other"] ?? BLOCKER_TYPE_LABELS.other) : null;

  // What "Show full" adds — without repeating the lead.
  const extra: string[] = [];
  if (approval.description && approval.description.trim() !== lead) extra.push(approval.description.trim());
  if (blocker?.description && blocker.description.trim() !== lead) extra.push(blocker.description.trim());
  if (blocker && !blocker.description && blocker.blocker_comment) extra.push(blocker.blocker_comment.trim());
  const hasMore = extra.length > 0 || !!blockerKind || !!visual?.screenshots?.length || !!visual?.preview_url;

  const primaryLabel = isQuestion ? t("reply") : isBlocker ? t("unblock") : t("approve");
  const secondaryLabel = isBlocker ? t("cancelTask") : t("reject");

  return (
    <div className="rounded-lg p-4 space-y-4" style={{ background: RAISED }} data-testid="task-approval">
      <div>
        <div
          ref={leadRef}
          data-testid="approval-lead"
          data-clamped={!expanded}
          className={`${NEXT_TEXT} prose-comment ${expanded ? "" : "max-h-[8.2em] overflow-hidden"} ${!expanded && overflows ? FADE : ""}`}
          style={{ color: C.textPrimary }}
        >
          <ReactMarkdown remarkPlugins={[remarkBreaks]}>{lead || approval.description}</ReactMarkdown>
        </div>

        {expanded && (
          <div className="mt-3 space-y-3 text-sm" data-testid="approval-details">
            {blockerKind && <p style={{ color: C.textMuted }}>{t(blockerKind.labelKey)}</p>}
            {extra.map((text, i) => (
              <div key={i} className="prose-comment" style={{ color: C.textSecondary }}>
                <ReactMarkdown remarkPlugins={[remarkBreaks]}>{text}</ReactMarkdown>
              </div>
            ))}
            {visual?.screenshots && visual.screenshots.length > 0 && (
              <div className="flex gap-2 flex-wrap">
                {visual.screenshots.map((src, i) => (
                  <a key={i} href={src} target="_blank" rel="noopener noreferrer" className="block w-40 h-24 rounded-md overflow-hidden" style={{ border: `1px solid ${C.border}` }}>
                    <img src={src} alt={t("screenshotAlt", { num: i + 1 })} className="w-full h-full object-cover" />
                  </a>
                ))}
              </div>
            )}
            {visual?.preview_url && (
              <a href={visual.preview_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 min-h-11 underline underline-offset-4" style={{ color: C.textPrimary, textDecorationColor: C.borderActive }}>
                {t("openPreview")}
                <ExternalLink size={14} aria-hidden />
              </a>
            )}
          </div>
        )}

        {(hasMore || overflows || expanded) && (
          <button
            type="button"
            onClick={() => setExpanded((o) => !o)}
            aria-expanded={expanded}
            className={`-ml-3 mt-1 ${QUIET_BTN}`}
            style={{ color: C.textSecondary }}
          >
            {expanded ? t("showLess") : t("showFull")}
            <ChevronDown size={16} aria-hidden style={{ transform: expanded ? "rotate(180deg)" : "none", transition: "transform 0.15s" }} />
          </button>
        )}
      </div>

      {question?.options && question.options.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {question.options.map((opt) => (
            <button
              key={opt}
              type="button"
              aria-pressed={note === opt}
              onClick={() => setNote(opt)}
              className="h-11 px-4 rounded-full text-sm cursor-pointer transition-colors"
              style={{
                background: note === opt ? C.accentSubtle : "var(--detail-bg, var(--color-bg-surface))",
                color: C.textPrimary,
                border: `1px solid ${note === opt ? C.borderAccent : C.border}`,
              }}
            >
              {opt}
            </button>
          ))}
        </div>
      )}

      {(isBlocker || isQuestion) && (
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder={t("agentInstructionPlaceholder")}
          aria-label={t("agentInstructionPlaceholder")}
          rows={2}
          className="w-full px-3 py-2 rounded-md text-base @min-[560px]:text-sm resize-y"
          style={{ background: "var(--detail-bg, var(--color-bg-deep))", color: C.textPrimary, border: `1px solid ${C.border}` }}
        />
      )}

      <div className="flex items-center gap-2 flex-wrap">
        <button
          type="button"
          onClick={() => onResolve("approved", note || undefined)}
          disabled={loading}
          className={PRIMARY_BTN}
          style={PRIMARY_STYLE}
        >
          {primaryLabel}
        </button>
        {!isQuestion && (
          <button
            type="button"
            onClick={() => (isBlocker ? setConfirmCancel(true) : onResolve("rejected", note || undefined))}
            disabled={loading}
            className={QUIET_BTN}
            style={{ color: C.textSecondary }}
          >
            {secondaryLabel}
          </button>
        )}
      </div>

      <ConfirmDialog
        open={confirmCancel}
        kicker={t("cancelTaskKicker")}
        title={t("cancelTaskTitle")}
        body={blocker?.blocked_agent_name ? t("cancelTaskBodyNamed", { agent: blocker.blocked_agent_name }) : t("cancelTaskBody")}
        confirmLabel={t("cancelTaskConfirm")}
        cancelLabel={t("cancelTaskKeep")}
        loading={loading}
        onConfirm={() => {
          setConfirmCancel(false);
          onResolve("rejected", note || undefined);
        }}
        onCancel={() => setConfirmCancel(false)}
      />
    </div>
  );
}
