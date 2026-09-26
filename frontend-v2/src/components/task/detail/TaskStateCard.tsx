"use client";

/**
 * The top of the task detail below the title (DESIGN.md K12, variant A):
 *
 *   TaskStateLine   ● Blocked · alpha asked 42 min ago   — one line, one "·"
 *   TaskStateCard   the next step, only when there is one:
 *
 *   NEEDS YOU  blocked/waiting/user_test. With an open approval the SAME
 *              ApprovalCard as in the inbox is embedded and is the only way
 *              to act — no extra status buttons here, otherwise the approval
 *              would stay open in the inbox. Without one: a surface with the
 *              latest blocker comment + Reply (jumps to the comment field).
 *   RUNNING    the last step in plain words, no surface.
 *   RESULT     the resolution (2–3 lines) + the PR link — the PR shows here once.
 *   FAILED     a surface with the error in plain words + Open log.
 *   HEAD       HeadStateCard (the head's own next step + ONE main action).
 *
 * A surface only where the operator has to act (K8); the status colour sits
 * on the dot and the state word only (K6). Content (reasons, comments,
 * errors) is shown in its working language, only the labels are translated.
 */

import { useTranslations } from "next-intl";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, MessageSquareReply, ScrollText } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import { ApprovalCard } from "@/components/inbox/ApprovalCard";
import type { StateCard } from "@/lib/taskDetail/stateCard";
import type { StateLine, StateTone } from "@/lib/taskDetail/stateLine";
import type { Task } from "@/lib/types";
import { HeadStateCard } from "@/components/heads/HeadStateCard";
import { NEXT_TEXT, PRIMARY_BTN, PRIMARY_STYLE, RAISED } from "./nextStepStyle";

const DOT: Record<StateTone, string> = {
  error: C.error,
  info: C.info,
  online: C.online,
  warning: C.warning,
  accent: C.accent,
  muted: C.textMuted,
};

// Ocker and the status reds/greens are lifted for text (STATUS_TEXT).
const WORD: Record<StateTone, string> = {
  error: STATUS_TEXT.error,
  info: STATUS_TEXT.info,
  online: STATUS_TEXT.online,
  warning: STATUS_TEXT.warning,
  accent: C.textPrimary,
  muted: C.textSecondary,
};

/** Colour of the state dot — the compact bar repeats it next to the title. */
export const stateDotColor = (tone: StateTone) => DOT[tone];

export function TaskStateLine({ line }: { line: StateLine }) {
  const t = useTranslations();
  const word = t(`${line.word.ns}.${line.word.key}`);
  const detail = line.detail ? t(`tasks.detail.line.${line.detail.key}`, line.detail.values) : null;
  return (
    <p data-testid="task-state-line" data-tone={line.tone} className="mt-2 flex items-center gap-2 min-w-0 text-sm">
      <span aria-hidden className="w-2 h-2 rounded-full shrink-0" style={{ background: DOT[line.tone] }} />
      <span className="truncate">
        <span style={{ color: WORD[line.tone] }}>{word}</span>
        {detail && <span style={{ color: C.textMuted }}> · {detail}</span>}
      </span>
    </p>
  );
}

/** The header shows a clamped preview — blank lines and markdown line breaks
 *  would eat the few lines it has; the full text is in the tooltip and tabs. */
const preview = (text: string) => text.replace(/\s+/g, " ").trim();

/** The next-step block. One element for every kind, so a card that switches
 *  (e.g. once the approval has loaded) keeps its node. `raised` = the one
 *  surface of the header, only where the operator has to act. */
function Block({ kind, raised = false, children }: { kind: StateCard["kind"]; raised?: boolean; children: React.ReactNode }) {
  return (
    <section data-testid="task-state-card" data-kind={kind} className="mt-4">
      <div className={raised ? "rounded-lg p-4 space-y-4" : "space-y-2"} style={raised ? { background: RAISED } : undefined}>
        {children}
      </div>
    </section>
  );
}

export function TaskStateCard({
  card,
  task,
  onReply,
  onOpenLog,
}: {
  card: StateCard;
  task: Task;
  onReply: () => void;
  onOpenLog: () => void;
}) {
  const t = useTranslations("tasks");
  const tInbox = useTranslations("inbox");
  const qc = useQueryClient();

  // Same resolve path as the inbox page — one approval, one way to answer it.
  const resolveMutation = useMutation({
    mutationFn: ({ id, status, note }: { id: string; status: "approved" | "rejected"; note?: string }) =>
      api.approvals.resolve(id, status, note),
    onSuccess: (_, { status }) => {
      qc.invalidateQueries({ queryKey: ["approvals"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["pipeline"] });
      qc.invalidateQueries({ queryKey: ["run-record", task.id] });
      notify.success(status === "approved" ? tInbox("approvedNotify") : tInbox("rejectedNotify"));
    },
    onError: () => notify.error(tInbox("resolveFailed")),
  });

  // A head run owns the card (head-launcher §8.2) — its own next step + one action.
  if (card.kind === "head") {
    return <HeadStateCard run={card.run} mainAction={card.mainAction} silentWarn={card.silentWarn} />;
  }

  if (card.kind === "needs_you") {
    if (card.approval) {
      // The approval carries the reason itself (clamped, "Show full") — no
      // own quote above it, or the same sentence shows up three times. It is
      // the one surface of the header.
      return (
        <Block kind="needs_you">
          <div data-testid="state-card-approval">
            <ApprovalCard
              compact
              approval={card.approval}
              loading={resolveMutation.isPending}
              onResolve={(status, note) => resolveMutation.mutate({ id: card.approval!.id, status, note })}
            />
          </div>
        </Block>
      );
    }
    return (
      <Block kind="needs_you" raised>
        <p className={`${NEXT_TEXT} line-clamp-4`} style={{ color: card.reason ? C.textPrimary : C.textSecondary }} title={card.reason ?? undefined}>
          {card.reason ? preview(card.reason) : t("detail.noReason")}
        </p>
        <button type="button" onClick={onReply} className={PRIMARY_BTN} style={PRIMARY_STYLE}>
          <MessageSquareReply size={16} aria-hidden />
          {t("detail.reply")}
        </button>
      </Block>
    );
  }

  if (card.kind === "running") {
    // Nothing reported yet → no line at all; the state sentence says enough.
    if (!card.lastStep) return null;
    return (
      <Block kind="running">
        <p className={`${NEXT_TEXT} line-clamp-2`} style={{ color: C.textSecondary }} title={card.lastStep}>
          {preview(card.lastStep)}
        </p>
      </Block>
    );
  }

  if (card.kind === "result") {
    if (!card.resolution && !card.prUrl) return null;
    return (
      <Block kind="result">
        {card.resolution && (
          <p className={`${NEXT_TEXT} line-clamp-3`} style={{ color: C.textSecondary }} title={card.resolution}>
            {card.resolutionIsFallback && <span style={{ color: C.textMuted }}>{t("detail.resultFallbackShort")} </span>}
            {preview(card.resolution)}
          </p>
        )}
        {card.prUrl && (
          <a
            href={card.prUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 min-h-11 text-sm underline underline-offset-4 cursor-pointer"
            style={{ color: C.textPrimary, textDecorationColor: C.borderActive }}
          >
            {card.prNumber ? t("prChipNumber", { number: card.prNumber }) : t("prChipOpen")}
            <ExternalLink size={14} aria-hidden />
          </a>
        )}
      </Block>
    );
  }

  return (
    <Block kind="failed" raised>
      <p className={`${NEXT_TEXT} line-clamp-4`} style={{ color: card.error ? C.textPrimary : C.textSecondary }} title={card.error ?? undefined}>
        {card.error ? preview(card.error) : t("detail.noError")}
      </p>
      <button type="button" onClick={onOpenLog} className={PRIMARY_BTN} style={PRIMARY_STYLE}>
        <ScrollText size={16} aria-hidden />
        {t("detail.openLog")}
      </button>
    </Block>
  );
}
