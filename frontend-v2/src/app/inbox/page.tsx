"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useQueryClient, useMutation } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
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
import { useInbox } from "@/hooks/useInbox";
import { BetweenAgents, useConversations } from "@/components/inbox/BetweenAgents";
import { chatEntryHref } from "@/lib/chatHistory";
import { resolveErrorKey } from "@/lib/inbox";

type InboxTab = "you" | "agents";

/** `?tab=agents&c=<thread>` — a link (e.g. from an approval card) opens a
 *  conversation directly, and the phone's back gesture walks back out. */
function readLocation(params: URLSearchParams | null): { tab: InboxTab; conv: string | null } {
  const conv = params?.get("c") || null;
  const tab: InboxTab = params?.get("tab") === "agents" || conv ? "agents" : "you";
  return { tab, conv: tab === "agents" ? conv : null };
}

function locationHref(tab: InboxTab, conv: string | null): string {
  if (tab === "you") return "/inbox";
  return conv ? `/inbox?tab=agents&c=${encodeURIComponent(conv)}` : "/inbox?tab=agents";
}

// useSearchParams needs a Suspense boundary in the app router (same as
// sessions/page.tsx).
export default function InboxPage() {
  return (
    <Suspense fallback={null}>
      <InboxView />
    </Suspense>
  );
}

function InboxTabs({
  tab,
  onChange,
  forYou,
  between,
}: {
  tab: InboxTab;
  onChange: (tab: InboxTab) => void;
  forYou: number;
  between: number | null;
}) {
  const t = useTranslations("inbox");
  const tabs: { key: InboxTab; label: string }[] = [
    { key: "you", label: t("tabForYou") },
    { key: "agents", label: t("tabBetweenAgents") },
  ];
  return (
    <div role="tablist" aria-label={t("tabsLabel")} className="flex items-center gap-1" data-testid="inbox-tabs">
      {tabs.map(({ key, label }) => {
        const active = tab === key;
        return (
          <button
            key={key}
            type="button"
            role="tab"
            id={`inbox-tab-${key}`}
            aria-selected={active}
            aria-controls={`inbox-panel-${key}`}
            onClick={() => onChange(key)}
            data-testid={`inbox-tab-${key}`}
            className="flex items-center gap-2 min-h-11 px-3 rounded-md text-sm font-medium cursor-pointer transition-colors"
            style={{
              background: active ? C.bgElevated : "transparent",
              color: active ? C.textPrimary : C.textMuted,
            }}
          >
            {label}
            {/* Only "For you" carries the badge look — the one tab that
                waits on the operator, the same number as the tab bar's
                badge (ADR-100 §4). The other count is plain text:
                something to read, not to answer. */}
            {key === "you" && forYou > 0 && (
              <span
                data-testid="inbox-tab-you-count"
                className="min-w-5 h-5 px-1 rounded-full flex items-center justify-center text-xs tabular-nums font-semibold"
                style={{ background: C.accent, color: C.onAccent }}
              >
                {forYou}
              </span>
            )}
            {key === "agents" && between != null && between > 0 && (
              <span data-testid="inbox-tab-agents-count" className="text-xs tabular-nums" style={{ color: C.textMuted }}>
                {between}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

function InboxView() {
  const t = useTranslations("inbox");
  const params = useSearchParams();
  const [{ tab, conv }, setLocation] = useState(() => readLocation(params));
  // Back/forward and links that change the query land here (keyed on the
  // query string: the params object itself is new on every render).
  const paramsKey = params?.toString() ?? "";
  useEffect(() => {
    setLocation(readLocation(new URLSearchParams(paramsKey)));
  }, [paramsKey]);
  const go = useCallback((next: InboxTab, nextConv: string | null = null) => {
    setLocation({ tab: next, conv: next === "agents" ? nextConv : null });
    window.history.pushState(null, "", locationHref(next, nextConv));
  }, []);
  const conversations = useConversations();
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
    onError: (err) => notify.error(t(resolveErrorKey(err))),
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
      {/* Header: the title and the two tabs. The eyebrow and the "N pending"
          line + pill left (K2/K3, K7): the "For you" tab carries that number
          now, exactly once. */}
      <div className="flex flex-col gap-3">
        <h1 className="display text-2xl font-semibold text-[var(--color-text-primary)]">
          {t("title")}
        </h1>
        <InboxTabs
          tab={tab}
          onChange={(next) => go(next)}
          forYou={totalCount}
          between={conversations.data ? conversations.data.length : null}
        />
      </div>

      {tab === "agents" ? (
        <div role="tabpanel" id="inbox-panel-agents" aria-labelledby="inbox-tab-agents">
          <BetweenAgents openThreadId={conv} onOpen={(id) => go("agents", id)} onGoForYou={() => go("you")} />
        </div>
      ) : (
      <div role="tabpanel" id="inbox-panel-you" aria-labelledby="inbox-tab-you" className="flex flex-col gap-6">

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
                href={chatEntryHref("head", run.run_id)}
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
      )}
    </div>
    </AppShell>
  );
}
