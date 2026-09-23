"use client";

/**
 * Head state card (docs/specs/head-launcher.md §8.2). Sits on top of the task
 * detail while the task has a head run and wins over the task-status card.
 * Layout rule: state + ONE main action on top, everything else under
 * "Details" (collapsed):
 *
 *   ● Running · omp · GLM local                      12 min
 *     Step: 4/7 sabotage probe
 *     Last output 40 s ago           (> 15 min: "Silent for 22 min", warn tone)
 *     [Stop]                                         Details ▾
 *       Restart with … · Open log · Run record · Copy tmux attach (desktop)
 *
 *   needs you → question + answer field + [Answer & continue] [Restart with …]
 *   passed    → "PR #712 open — your review decides the merge" [Open PR ↗]
 *   failed / stopped → reason in one sentence + branch  [Restart with …]
 *
 * Content (question, step, log) is shown as the head wrote it; only labels
 * are translated.
 */

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ExternalLink, RotateCcw, ScrollText, Terminal, FileText, Send } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import { formatDuration } from "@/lib/taskDetail/format";
import {
  failReasonKey,
  headErrorKey,
  headStateKey,
  prNumberFromUrl,
  runDurationSeconds,
  runPairLabel,
  type HeadPairsResponse,
  type HeadRun,
  type HeadState,
} from "@/lib/heads";
import type { HeadMainAction } from "@/lib/taskDetail/stateCard";
import { HeadRestartDialog } from "./HeadRestartDialog";
import { HeadStopButton } from "./HeadStopButton";

const STATE_TONE: Record<HeadState, string> = {
  starting: C.info,
  running: C.info,
  // Waiting on the operator = the brightest tone, not a hue (DESIGN.md).
  needs_you: C.accent,
  passed: C.online,
  failed: C.error,
  stopped: C.textMuted,
};

const STATE_TEXT: Record<HeadState, string> = {
  starting: STATUS_TEXT.info,
  running: STATUS_TEXT.info,
  needs_you: C.accent,
  passed: STATUS_TEXT.online,
  failed: STATUS_TEXT.error,
  stopped: C.textSecondary,
};

const STATE_GLYPH: Record<HeadState, string> = {
  starting: "●",
  running: "●",
  needs_you: "?",
  passed: "✓",
  failed: "✕",
  stopped: "■",
};

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

const primaryBtn =
  "inline-flex items-center justify-center gap-1.5 px-3.5 min-h-[36px] pointer-coarse:min-h-[44px] rounded-md text-xs font-semibold cursor-pointer transition-colors hover:bg-[var(--color-accent-light)] disabled:opacity-40 disabled:cursor-not-allowed";
const ghostBtn =
  "inline-flex items-center justify-center gap-1.5 px-3 min-h-[36px] pointer-coarse:min-h-[44px] rounded-md text-xs font-medium cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)] disabled:opacity-40 disabled:cursor-not-allowed";

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
  const pairs = useHeadPairsForLabels();
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [restartOpen, setRestartOpen] = useState(false);
  const [logOpen, setLogOpen] = useState(false);
  const [recordOpen, setRecordOpen] = useState(false);
  const [answer, setAnswer] = useState("");

  const pair = runPairLabel(run, pairs);
  const tone = silentWarn ? C.warning : STATE_TONE[run.state];
  const textTone = silentWarn ? STATUS_TEXT.warning : STATE_TEXT[run.state];
  const duration = formatDuration(runDurationSeconds(run), locale);

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
        className={primaryBtn}
        style={{ background: C.accent, color: C.onAccent }}
      >
        {prNumber != null ? t("card.openPr", { number: prNumber }) : t("card.openPrNoNumber")}
        <ExternalLink size={12} aria-hidden />
      </a>
    );
  } else if (mainAction === "answer") {
    main = (
      <button
        type="button"
        onClick={() => answerMutation.mutate()}
        disabled={!answer.trim() || answerMutation.isPending}
        data-testid="head-main-answer"
        className={primaryBtn}
        style={{ background: C.accent, color: C.onAccent }}
      >
        <Send size={12} aria-hidden />
        {t("card.answerContinue")}
      </button>
    );
  } else {
    main = (
      <button
        type="button"
        onClick={() => setRestartOpen(true)}
        data-testid="head-main-restart"
        className={primaryBtn}
        style={{ background: C.accent, color: C.onAccent }}
      >
        <RotateCcw size={12} aria-hidden />
        {t("card.restartWith")}
      </button>
    );
  }

  return (
    <section
      data-testid="task-state-card"
      data-kind="head"
      data-head-state={run.state}
      data-tone={silentWarn ? "warn" : run.state}
      aria-label={t(headStateKey(run.state))}
      className="rounded-lg px-3.5 py-3 space-y-2"
      style={{ background: `${tone}0F`, border: `1px solid ${tone}40` }}
    >
      <div className="flex items-start gap-2">
        <div className="label-sys min-w-0 flex-1 truncate" style={{ color: textTone }} data-testid="head-card-kicker">
          <span aria-hidden>{STATE_GLYPH[run.state]} </span>
          {t("card.kicker", { state: t(headStateKey(run.state)), pair })}
        </div>
        {duration && (
          <span className="font-mono text-[11px] shrink-0" style={{ color: C.textMuted }}>{duration}</span>
        )}
      </div>

      {(run.state === "running" || run.state === "starting") && (
        <>
          <p className="text-[13px] leading-relaxed line-clamp-2" style={{ color: run.step ? C.textPrimary : C.textSecondary }}>
            {run.step ? t("card.step", { step: run.step }) : t("card.noStep")}
          </p>
          <div className="text-[11px] font-mono" style={{ color: silentWarn || run.heartbeat_stale ? STATUS_TEXT.warning : C.textMuted }} data-testid="head-card-sign">
            {silentWarn
              ? t("card.silentWarn", { age: secondsLabel(run.silent_s, locale) ?? "—" })
              : run.silent_s != null
                ? t("card.lastSign", { age: secondsLabel(run.silent_s, locale) ?? "—" })
                : t("card.noSign")}
            {run.heartbeat_stale && ` · ${t("card.heartbeatStale")}`}
          </div>
        </>
      )}

      {run.state === "needs_you" && (
        <>
          {run.question?.trim() ? (
            <p className="text-[13px] leading-relaxed whitespace-pre-line line-clamp-6" style={{ color: C.textPrimary }} data-testid="head-card-question">
              {run.question.trim()}
            </p>
          ) : (
            <p className="text-[13px]" style={{ color: C.textSecondary }}>{t("card.noQuestion")}</p>
          )}
          <label htmlFor={`head-answer-${run.run_id}`} className="sr-only">{t("card.answerLabel")}</label>
          <textarea
            id={`head-answer-${run.run_id}`}
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            rows={2}
            maxLength={8000}
            placeholder={t("card.answerPlaceholder")}
            className="w-full px-3 py-2 rounded-md text-base sm:text-xs resize-y"
            style={{ background: C.bgDeep, color: C.textPrimary, border: `1px solid ${C.border}` }}
          />
        </>
      )}

      {run.state === "passed" && (
        <p className="text-[13px]" style={{ color: C.textPrimary }}>
          {prNumber != null ? t("card.prOpen", { number: prNumber }) : t("card.prOpenNoNumber")}
        </p>
      )}

      {(run.state === "failed" || run.state === "stopped") && (
        <p className="text-[13px]" style={{ color: C.textPrimary }} data-testid="head-card-reason">
          {t(fail.key, fail.values)}
          {run.branch && (
            <span className="block text-[11px] font-mono mt-0.5 truncate" style={{ color: C.textMuted }}>
              {t("card.branchKept", { branch: run.branch })}
            </span>
          )}
        </p>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        {main}
        {mainAction === "answer" && (
          <button type="button" onClick={() => setRestartOpen(true)} className={ghostBtn} style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}>
            <RotateCcw size={12} aria-hidden />
            {t("card.restartWith")}
          </button>
        )}
        <button
          type="button"
          onClick={() => setDetailsOpen((o) => !o)}
          aria-expanded={detailsOpen}
          data-testid="head-details-toggle"
          className="ml-auto inline-flex items-center gap-1 px-2 min-h-[36px] pointer-coarse:min-h-[44px] text-[11px] cursor-pointer hover:underline"
          style={{ color: C.textSecondary }}
        >
          {t("card.details")}
          <ChevronDown size={11} aria-hidden style={{ transform: detailsOpen ? "rotate(180deg)" : "none", transition: "transform 0.15s" }} />
        </button>
      </div>

      {detailsOpen && (
        <div className="space-y-2 pt-1" data-testid="head-details">
          <div className="flex items-center gap-2 flex-wrap">
            {mainAction === "stop" && (
              <button type="button" onClick={() => setRestartOpen(true)} className={ghostBtn} style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }} data-testid="head-details-restart">
                <RotateCcw size={12} aria-hidden />
                {t("card.restartWith")}
              </button>
            )}
            <button type="button" onClick={() => setLogOpen((o) => !o)} aria-expanded={logOpen} className={ghostBtn} style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}>
              <ScrollText size={12} aria-hidden />
              {logOpen ? t("card.hideLog") : t("card.openLog")}
            </button>
            {run.run_record && (
              <button type="button" onClick={() => setRecordOpen((o) => !o)} aria-expanded={recordOpen} className={ghostBtn} style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}>
                <FileText size={12} aria-hidden />
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
                className={`hidden md:inline-flex ${ghostBtn}`}
                style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}
              >
                <Terminal size={12} aria-hidden />
                {t("card.copyTmux")}
              </button>
            )}
          </div>
          {logOpen && (
            <pre
              className="rounded-dense p-2.5 text-[11px] leading-snug font-mono overflow-auto max-h-[320px] whitespace-pre-wrap break-words"
              style={{ background: C.bgDeep, color: C.textSecondary, border: `1px solid ${C.border}` }}
              data-testid="head-log"
            >
              {logQuery.isError ? t("card.logFailed") : logQuery.data === undefined ? "…" : logQuery.data.trim() ? logQuery.data : t("card.logEmpty")}
            </pre>
          )}
          {recordOpen && (
            <pre
              className="rounded-dense p-2.5 text-[11px] leading-snug font-mono overflow-auto max-h-[420px] whitespace-pre-wrap break-words"
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
