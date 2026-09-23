"use client";

/**
 * State card — the first thing under the title. Answers "what is the state,
 * do I need to act, what came out" in one glance. What it shows comes from
 * deriveStateCard() (lib/taskDetail/stateCard.ts):
 *
 *   NEEDS YOU  blocked/waiting/user_test. With an open approval the SAME
 *              ApprovalCard as in the inbox is embedded and is the only way
 *              to act — no extra status buttons here, otherwise the approval
 *              would stay open in the inbox. Without one: latest blocker
 *              comment + Reply (jumps to the comment field).
 *   RUNNING    last step in plain words, runtime, heartbeat age.
 *   RESULT     resolution comment (2–3 lines), PR chip, evidence, duration.
 *   FAILED     the error in plain words + Open log.
 *
 * Content (reasons, comments, errors) is shown in its working language, only
 * the labels are translated.
 */

import { useLocale, useTranslations } from "next-intl";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { MessageSquareReply, ScrollText } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, LANE, STATUS_TEXT } from "@/lib/colors";
import { ApprovalCard } from "@/components/inbox/ApprovalCard";
import { formatAge, formatDuration, secondsBetween } from "@/lib/taskDetail/format";
import { STATUS_LABEL_KEY } from "@/lib/taskDetail/statusLabels";
import type { StateCard } from "@/lib/taskDetail/stateCard";
import type { Agent, Task } from "@/lib/types";
import { PrChip } from "./PrChip";

const KIND_COLOR: Record<StateCard["kind"], string> = {
  needs_you: C.error,
  running: LANE.in_progress,
  result: LANE.done,
  failed: C.error,
};

const KIND_TEXT: Record<StateCard["kind"], string> = {
  needs_you: STATUS_TEXT.error,
  running: STATUS_TEXT.info,
  result: STATUS_TEXT.online,
  failed: STATUS_TEXT.error,
};

function Shell({
  kind,
  tone,
  kicker,
  children,
}: {
  kind: StateCard["kind"];
  tone?: string;
  kicker: React.ReactNode;
  children: React.ReactNode;
}) {
  const color = tone ?? KIND_COLOR[kind];
  // Ocker is lifted for text (STATUS_TEXT); the other status tones read as-is.
  const textColor = tone ? (tone === C.warning ? STATUS_TEXT.warning : tone) : KIND_TEXT[kind];
  return (
    <section
      data-testid="task-state-card"
      data-kind={kind}
      aria-label={typeof kicker === "string" ? kicker : undefined}
      className="rounded-lg px-3.5 py-3 space-y-2"
      style={{ background: `${color}0F`, border: `1px solid ${color}40` }}
    >
      <div className="label-sys" style={{ color: textColor }}>
        {kicker}
      </div>
      {children}
    </section>
  );
}

function Quote({ text, lines = 3 }: { text: string; lines?: 2 | 3 }) {
  return (
    <p
      className={`text-[13px] leading-relaxed whitespace-pre-line ${lines === 2 ? "line-clamp-2" : "line-clamp-3"}`}
      style={{ color: C.textPrimary }}
      title={text}
    >
      {text}
    </p>
  );
}

function ActionButton({ onClick, icon: Icon, children }: { onClick: () => void; icon: typeof ScrollText; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1.5 px-3 min-h-[36px] rounded-md text-xs font-medium cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
      style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}
    >
      <Icon size={13} aria-hidden />
      {children}
    </button>
  );
}

export function TaskStateCard({
  card,
  task,
  agents,
  onReply,
  onOpenLog,
}: {
  card: StateCard;
  task: Task;
  agents: Agent[];
  onReply: () => void;
  onOpenLog: () => void;
}) {
  const t = useTranslations("tasks");
  const tInbox = useTranslations("inbox");
  const locale = useLocale();
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

  const statusLabel = t(STATUS_LABEL_KEY[task.status]);

  if (card.kind === "needs_you") {
    const since = formatAge(card.since, locale);
    const asker = card.askerAgentId ? agents.find((a) => a.id === card.askerAgentId)?.name : undefined;
    return (
      <Shell
        kind="needs_you"
        tone={LANE[task.status]}
        kicker={`${t("detail.stateNeedsYou")} · ${since ? t("detail.stateSince", { status: statusLabel, duration: since }) : statusLabel}`}
      >
        <div className="text-xs" style={{ color: C.textMuted }}>
          {asker ? t("detail.asks", { name: asker }) : t("detail.someoneAsks")}
        </div>
        {card.reason ? <Quote text={card.reason} /> : (
          <p className="text-[13px]" style={{ color: C.textSecondary }}>{t("detail.noReason")}</p>
        )}
        {card.approval ? (
          <div data-testid="state-card-approval">
            <ApprovalCard
              approval={card.approval}
              loading={resolveMutation.isPending}
              onResolve={(status, note) => resolveMutation.mutate({ id: card.approval!.id, status, note })}
            />
          </div>
        ) : (
          <ActionButton onClick={onReply} icon={MessageSquareReply}>
            {t("detail.reply")}
          </ActionButton>
        )}
      </Shell>
    );
  }

  if (card.kind === "running") {
    const runtime = formatDuration(secondsBetween(card.startedAt, null), locale);
    const heartbeat = formatAge(card.heartbeatAt, locale);
    return (
      <Shell
        kind="running"
        kicker={runtime ? `${t("detail.stateRunning")} · ${t("detail.runningFor", { duration: runtime })}` : t("detail.stateRunning")}
      >
        <div className="text-xs" style={{ color: C.textMuted }}>{t("detail.lastStep")}</div>
        {card.lastStep ? <Quote text={card.lastStep} lines={2} /> : (
          <p className="text-[13px]" style={{ color: C.textSecondary }}>{t("detail.noLastStep")}</p>
        )}
        <div className="text-[11px] font-mono" style={{ color: C.textMuted }}>
          {heartbeat ? t("detail.heartbeat", { age: heartbeat }) : t("detail.noHeartbeat")}
        </div>
      </Shell>
    );
  }

  if (card.kind === "result") {
    const took = formatDuration(card.durationSeconds, locale);
    return (
      <Shell
        kind="result"
        kicker={took ? `${t("detail.stateResult")} · ${t("detail.tookDuration", { duration: took })}` : t("detail.stateResult")}
      >
        {card.resolution ? <Quote text={card.resolution} /> : (
          <p className="text-[13px]" style={{ color: C.textSecondary }}>{t("detail.noResolution")}</p>
        )}
        {(card.prUrl || card.evidenceCount != null) && (
          <div className="flex items-center gap-2 flex-wrap">
            {card.prUrl && <PrChip url={card.prUrl} number={card.prNumber} />}
            {card.evidenceCount != null && (
              <span className="text-[11px] font-mono" style={{ color: C.textMuted }}>
                {t("detail.evidenceCount", { count: card.evidenceCount })}
              </span>
            )}
          </div>
        )}
      </Shell>
    );
  }

  return (
    <Shell
      kind="failed"
      tone={LANE[task.status]}
      kicker={task.status === "aborted" ? t("detail.stateAborted") : t("detail.stateFailed")}
    >
      {card.error ? <Quote text={card.error} /> : (
        <p className="text-[13px]" style={{ color: C.textSecondary }}>{t("detail.noError")}</p>
      )}
      <ActionButton onClick={onOpenLog} icon={ScrollText}>
        {t("detail.openLog")}
      </ActionButton>
    </Shell>
  );
}
