"use client";

/**
 * Head next step (docs/specs/head-launcher.md §8.2, DESIGN.md K12). Sits under
 * the task title while the task has a head run and wins over the task-status
 * card. The state word and its one time ("Running · for 12 min") are in the
 * state sentence above; the pair (harness · runtime) is in the properties.
 * Here: the head's next step + ONE main action, everything else under
 * "Details" (collapsed):
 *
 *   Step: 4/7 sabotage probe
 *   [Stop]                                         Details ▾
 *     Last output 40 s ago · Restart with … · Open log · Run record · tmux (desktop)
 *
 *   needs you → surface: question + answer field + [Answer & continue] [Restart with …]
 *   passed    → "The pull request is open — your review decides" [Open PR #712 ↗]
 *               scratch repo with a local origin (no PR possible): the
 *               sentence + the branch with a copy button, no button
 *   failed / stopped → surface: reason in one sentence + branch  [Restart with …]
 *
 * Content (question, step, log) is shown as the head wrote it; only labels
 * are translated.
 */

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Copy, ExternalLink, RotateCcw, ScrollText, Terminal, FileText, Send } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import { formatDuration } from "@/lib/taskDetail/format";
import {
  failReasonKey,
  headErrorKey,
  headStateKey,
  prNumberFromUrl,
  type HeadPairsResponse,
  type HeadRun,
} from "@/lib/heads";
import type { HeadMainAction } from "@/lib/taskDetail/stateCard";
import { HeadRestartDialog } from "./HeadRestartDialog";
import { HeadStopButton } from "./HeadStopButton";
import { NEXT_TEXT, PRIMARY_BTN, PRIMARY_STYLE, QUIET_BTN, RAISED } from "@/components/task/detail/nextStepStyle";

/** Pairs for nicer runtime names — shared cache with the pickers. */
export function useHeadPairsForLabels(enabled = true) {
  const q = useQuery<HeadPairsResponse>({
    queryKey: ["heads", "pairs"],
    queryFn: () => api.heads.pairs(),
    enabled,
    retry: false,
    staleTime: 60_000,
  });
  return q.data?.pairs ?? null;
}

function secondsLabel(seconds: number | null | undefined, locale: string): string | null {
  if (seconds == null) return null;
  if (seconds < 60) return `${seconds} s`;
  return formatDuration(seconds, locale);
}

export function HeadStateCard({
  run,
  mainAction,
  silentWarn,
}: {
  run: HeadRun;
  mainAction: HeadMainAction;
  silentWarn: boolean;
}) {
  const t = useTranslations("heads");
  const locale = useLocale();
  const qc = useQueryClient();
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [restartOpen, setRestartOpen] = useState(false);
  const [logOpen, setLogOpen] = useState(false);
  const [recordOpen, setRecordOpen] = useState(false);
  const [answer, setAnswer] = useState("");

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["heads"] });
    qc.invalidateQueries({ queryKey: ["tasks"] });
    qc.invalidateQueries({ queryKey: ["pipeline"] });
  };

  const stop = useMutation({
    mutationFn: () => api.heads.stop(run.run_id),
    onSuccess: () => {
      notify.success(t("card.stopRequested"));
      invalidate();
    },
    onError: (err) => notify.error(t(headErrorKey(err))),
  });

  // Answer & continue = restart on the SAME pair, same branch, with the answer.
  const answerMutation = useMutation({
    mutationFn: () =>
      api.heads.restart(run.run_id, {
        harness: run.harness ?? "",
        runtime_slug: run.runtime_slug ?? "",
        mode: "continue",
        answer: answer.trim(),
      }),
    onSuccess: () => {
      setAnswer("");
      notify.success(t("restart.done"));
      invalidate();
    },
    onError: (err) => notify.error(t(headErrorKey(err))),
  });

  const logQuery = useQuery({
    queryKey: ["heads", run.run_id, "log"],
    queryFn: () => api.heads.log(run.run_id, 200),
    enabled: logOpen,
    retry: false,
    refetchInterval: logOpen && (run.state === "running" || run.state === "starting") ? 10_000 : false,
  });
  const recordQuery = useQuery({
    queryKey: ["heads", run.run_id, "run-record"],
    queryFn: () => api.heads.runRecord(run.run_id),
    enabled: recordOpen,
    retry: false,
  });

  const prNumber = prNumberFromUrl(run.pr_url);
  const fail = failReasonKey(run.reason);

  // ── Main action (exactly one) ──
  let main: React.ReactNode = null;
  if (mainAction === "stop") {
    main = (
      <HeadStopButton
        onStop={() => stop.mutate()}
        pending={stop.isPending}
        done={stop.isSuccess}
        testId="head-main-stop"
      />
    );
  } else if (mainAction === "open_pr" && run.pr_url) {
    main = (
      <a
        href={run.pr_url}
        target="_blank"
        rel="noopener noreferrer"
        data-testid="head-main-open-pr"
        className={PRIMARY_BTN}
        style={PRIMARY_STYLE}
      >
        {prNumber != null ? t("card.openPr", { number: prNumber }) : t("card.openPrNoNumber")}
        <ExternalLink size={16} aria-hidden />
      </a>
    );
  } else if (mainAction === "answer") {
    main = (
      <button
        type="button"
        onClick={() => answerMutation.mutate()}
        disabled={!answer.trim() || answerMutation.isPending}
        data-testid="head-main-answer"
        className={PRIMARY_BTN}
        style={PRIMARY_STYLE}
      >
        <Send size={16} aria-hidden />
        {t("card.answerContinue")}
      </button>
    );
  } else if (mainAction === "restart") {
    main = (
      <button
        type="button"
        onClick={() => setRestartOpen(true)}
        data-testid="head-main-restart"
        className={PRIMARY_BTN}
        style={PRIMARY_STYLE}
      >
        <RotateCcw size={16} aria-hidden />
        {t("card.restartWith")}
      </button>
    );
  }
  // branch_pushed: no button — there is nothing to open on GitHub; the
  // sentence below says where the result is.

  // A surface only when the operator has to act (DESIGN.md K8).
  const acts = run.state === "needs_you" || run.state === "failed" || run.state === "stopped";

  const branchLine = run.branch ? (
    <span className="mt-2 flex items-start gap-1 min-w-0 font-mono text-sm" style={{ color: C.textMuted }}>
      <span className="break-all pt-1">{t("card.branchKept", { branch: run.branch })}</span>
      <button
        type="button"
        aria-label={t("card.copyBranch")}
        data-testid="head-copy-branch"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(run.branch ?? "");
            notify.success(t("card.branchCopied"));
          } catch {
            notify.error(t("errors.unknown"));
          }
        }}
        className="shrink-0 -my-2 w-11 h-11 rounded-md flex items-center justify-center cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
        style={{ color: C.textSecondary }}
      >
        <Copy size={16} aria-hidden />
      </button>
    </span>
  ) : null;

  const signTone = silentWarn || run.heartbeat_stale ? STATUS_TEXT.warning : C.textMuted;

  return (
    <section
      data-testid="task-state-card"
      data-kind="head"
      data-head-state={run.state}
      data-tone={silentWarn ? "warn" : run.state}
      aria-label={t(headStateKey(run.state))}
      className={`mt-4 space-y-4 ${acts ? "rounded-lg p-4" : ""}`}
      style={acts ? { background: RAISED } : undefined}
    >
      {(run.state === "running" || run.state === "starting") && (
        <div>
          <p className={`${NEXT_TEXT} line-clamp-2`} style={{ color: C.textSecondary }}>
            {run.step ? t("card.step", { step: run.step }) : t("card.noStep")}
          </p>
          {/* Overdue sign of life is a warning, so it stays in view; the
              normal "last output" age lives under Details (one time per header). */}
          {run.heartbeat_stale && (
            <p className="mt-1 text-sm" style={{ color: STATUS_TEXT.warning }}>{t("card.heartbeatStale")}</p>
          )}
        </div>
      )}

      {run.state === "needs_you" && (
        <div className="space-y-4">
          {run.question?.trim() ? (
            <p className={`${NEXT_TEXT} whitespace-pre-line line-clamp-6`} style={{ color: C.textPrimary }} data-testid="head-card-question">
              {run.question.trim()}
            </p>
          ) : (
            <p className={NEXT_TEXT} style={{ color: C.textSecondary }}>{t("card.noQuestion")}</p>
          )}
          <label htmlFor={`head-answer-${run.run_id}`} className="sr-only">{t("card.answerLabel")}</label>
          <textarea
            id={`head-answer-${run.run_id}`}
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            rows={2}
            maxLength={8000}
            placeholder={t("card.answerPlaceholder")}
            className="w-full px-3 py-2 rounded-md text-base @min-[560px]:text-sm resize-y"
            style={{ background: "var(--detail-bg, var(--color-bg-deep))", color: C.textPrimary, border: `1px solid ${C.border}` }}
          />
        </div>
      )}

      {run.state === "passed" && mainAction === "branch_pushed" && (
        <div data-testid="head-card-scratch-branch">
          <p className={NEXT_TEXT} style={{ color: C.textSecondary }}>{t("card.scratchBranchResult")}</p>
          {branchLine}
        </div>
      )}
      {run.state === "passed" && mainAction !== "branch_pushed" && (
        <p className={NEXT_TEXT} style={{ color: C.textSecondary }}>
          {run.pr_url ? t("card.reviewDecides") : t("card.prOpenNoNumber")}
        </p>
      )}

      {(run.state === "failed" || run.state === "stopped") && (
        <div data-testid="head-card-reason">
          <p className={NEXT_TEXT} style={{ color: C.textPrimary }}>{t(fail.key, fail.values)}</p>
          {branchLine}
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        {main}
        {mainAction === "answer" && (
          <button type="button" onClick={() => setRestartOpen(true)} className={QUIET_BTN} style={{ color: C.textSecondary }}>
            <RotateCcw size={16} aria-hidden />
            {t("card.restartWith")}
          </button>
        )}
        <button
          type="button"
          onClick={() => setDetailsOpen((o) => !o)}
          aria-expanded={detailsOpen}
          data-testid="head-details-toggle"
          // Alone (no main action) it lines up with the text instead of floating right.
          className={`${main ? "ml-auto" : "-ml-3"} ${QUIET_BTN}`}
          style={{ color: C.textSecondary }}
        >
          {t("card.details")}
          <ChevronDown size={16} aria-hidden style={{ transform: detailsOpen ? "rotate(180deg)" : "none", transition: "transform 0.15s" }} />
        </button>
      </div>

      {detailsOpen && (
        <div className="space-y-2" data-testid="head-details">
          {(run.state === "running" || run.state === "starting") && (
            <p className="text-sm" style={{ color: signTone }} data-testid="head-card-sign">
              {silentWarn
                ? t("card.silentWarn", { age: secondsLabel(run.silent_s, locale) ?? "—" })
                : run.silent_s != null
                  ? t("card.lastSign", { age: secondsLabel(run.silent_s, locale) ?? "—" })
                  : t("card.noSign")}
            </p>
          )}
          <div className="flex items-center gap-1 flex-wrap -ml-3">
            {mainAction === "stop" && (
              <button type="button" onClick={() => setRestartOpen(true)} className={QUIET_BTN} style={{ color: C.textSecondary }} data-testid="head-details-restart">
                <RotateCcw size={16} aria-hidden />
                {t("card.restartWith")}
              </button>
            )}
            <button type="button" onClick={() => setLogOpen((o) => !o)} aria-expanded={logOpen} className={QUIET_BTN} style={{ color: C.textSecondary }}>
              <ScrollText size={16} aria-hidden />
              {logOpen ? t("card.hideLog") : t("card.openLog")}
            </button>
            {run.run_record && (
              <button type="button" onClick={() => setRecordOpen((o) => !o)} aria-expanded={recordOpen} className={QUIET_BTN} style={{ color: C.textSecondary }}>
                <FileText size={16} aria-hidden />
                {recordOpen ? t("card.hideRunRecord") : t("card.runRecord")}
              </button>
            )}
            {run.tmux && (
              // Desktop only — on the phone there is no terminal to paste into.
              <button
                type="button"
                data-testid="head-copy-tmux"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(`tmux attach -t ${run.tmux}`);
                    notify.success(t("card.tmuxCopied"));
                  } catch {
                    notify.error(t("errors.unknown"));
                  }
                }}
                className={`hidden md:inline-flex ${QUIET_BTN}`}
                style={{ color: C.textSecondary }}
              >
                <Terminal size={16} aria-hidden />
                {t("card.copyTmux")}
              </button>
            )}
          </div>
          {logOpen && (
            <pre
              className="rounded-dense p-3 text-xs leading-snug font-mono overflow-auto max-h-[320px] whitespace-pre-wrap break-words"
              style={{ background: C.bgDeep, color: C.textSecondary, border: `1px solid ${C.border}` }}
              data-testid="head-log"
            >
              {logQuery.isError ? t("card.logFailed") : logQuery.data === undefined ? "…" : logQuery.data.trim() ? logQuery.data : t("card.logEmpty")}
            </pre>
          )}
          {recordOpen && (
            <pre
              className="rounded-dense p-3 text-xs leading-snug font-mono overflow-auto max-h-[420px] whitespace-pre-wrap break-words"
              style={{ background: C.bgDeep, color: C.textSecondary, border: `1px solid ${C.border}` }}
              data-testid="head-run-record"
            >
              {recordQuery.isError ? t("card.runRecordFailed") : recordQuery.data ?? "…"}
            </pre>
          )}
        </div>
      )}

      <HeadRestartDialog open={restartOpen} onClose={() => setRestartOpen(false)} run={run} />
    </section>
  );
}
