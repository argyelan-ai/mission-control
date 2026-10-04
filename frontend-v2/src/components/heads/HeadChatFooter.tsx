"use client";

/**
 * HeadChatFooter — the one footer the head chat view shows INSTEAD of a
 * composer (bauplan `heads-sichtbar` PR 2 §3.2): a translated step line +
 * Stop while it runs, the question + answer field while it needs the
 * operator, and "Continue" once it is done. No "Ask about the result" (not
 * built — Entscheid 4, answered by Boss 2.0, not a new head process) and
 * no native "resume the conversation" (out of scope, bauplan §7).
 *
 * Failed/stopped get the same reason-plus-restart treatment
 * `HeadStateCard` already gives them on the task detail — not named
 * explicitly in the bauplan's own 3-row table, but leaving a finished-but-
 * failed head with NO action here (just a dead-end chat) would be the
 * missing button the repo's own third reviewer question flags.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslations } from "next-intl";
import { RotateCcw, Send } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import { failReasonKey, headErrorKey, headStepKey, parseStep, type HeadRun } from "@/lib/heads";
import { HeadStopButton } from "./HeadStopButton";
import { HeadRestartDialog } from "./HeadRestartDialog";

const PRIMARY_BTN =
  "inline-flex items-center justify-center gap-2 px-4 min-h-[40px] pointer-coarse:min-h-[44px] rounded-md text-sm font-medium cursor-pointer transition-colors disabled:opacity-40 disabled:cursor-not-allowed";
const PRIMARY_STYLE = { background: C.accent, color: C.onAccent } as const;
const QUIET_BTN =
  "inline-flex items-center justify-center gap-2 px-3 min-h-[40px] pointer-coarse:min-h-[44px] rounded-md text-sm font-medium cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]";

export function HeadChatFooter({ run }: { run: HeadRun }) {
  const t = useTranslations("heads");
  const qc = useQueryClient();
  const [answer, setAnswer] = useState("");
  const [restartOpen, setRestartOpen] = useState(false);

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

  if (run.state === "running" || run.state === "starting") {
    const parsed = parseStep(run.step);
    const stepKey = parsed ? headStepKey(parsed.n) : null;
    const stepLine = parsed && stepKey ? t("chat.footer.step", { n: parsed.n, total: parsed.total, name: t(stepKey) }) : null;
    return (
      <div className="flex items-center gap-3 px-3 md:px-4 py-3 border-t shrink-0" style={{ borderColor: C.border }}>
        {stepLine && (
          <span className="flex-1 min-w-0 truncate text-xs" style={{ color: C.textMuted }} data-testid="head-footer-step">
            {stepLine}
          </span>
        )}
        <HeadStopButton
          onStop={() => stop.mutate()}
          pending={stop.isPending}
          done={stop.isSuccess}
          testId="head-footer-stop"
        />
      </div>
    );
  }

  if (run.state === "needs_you") {
    return (
      <div className="flex flex-col gap-2 px-3 md:px-4 py-3 border-t shrink-0" style={{ borderColor: C.border }}>
        <label htmlFor={`head-footer-answer-${run.run_id}`} className="sr-only">
          {t("card.answerLabel")}
        </label>
        <div className="flex items-end gap-2">
          <textarea
            id={`head-footer-answer-${run.run_id}`}
            data-testid="head-footer-answer"
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            rows={1}
            maxLength={8000}
            placeholder={t("card.answerPlaceholder")}
            className="flex-1 min-w-0 px-3 py-2 rounded-md text-sm resize-y"
            style={{ background: "var(--color-bg-hover)", color: C.textPrimary, border: `1px solid ${C.border}` }}
          />
          <button
            type="button"
            onClick={() => answerMutation.mutate()}
            disabled={!answer.trim() || answerMutation.isPending}
            data-testid="head-footer-answer-send"
            className={PRIMARY_BTN}
            style={PRIMARY_STYLE}
          >
            <Send size={15} aria-hidden />
            {t("card.answerContinue")}
          </button>
        </div>
        <button
          type="button"
          onClick={() => setRestartOpen(true)}
          data-testid="head-footer-restart"
          className={`${QUIET_BTN} self-start -ml-3`}
          style={{ color: C.textSecondary }}
        >
          <RotateCcw size={15} aria-hidden />
          {t("card.restartWith")}
        </button>
        <HeadRestartDialog open={restartOpen} onClose={() => setRestartOpen(false)} run={run} />
      </div>
    );
  }

  if (run.state === "passed") {
    return (
      <div className="flex items-center justify-end px-3 md:px-4 py-3 border-t shrink-0" style={{ borderColor: C.border }}>
        <button
          type="button"
          onClick={() => setRestartOpen(true)}
          data-testid="head-footer-continue"
          className={PRIMARY_BTN}
          style={PRIMARY_STYLE}
        >
          <RotateCcw size={15} aria-hidden />
          {t("chat.footer.continueRun")}
        </button>
        <HeadRestartDialog open={restartOpen} onClose={() => setRestartOpen(false)} run={run} />
      </div>
    );
  }

  // failed / stopped
  const fail = failReasonKey(run.reason);
  return (
    <div className="flex items-center gap-3 px-3 md:px-4 py-3 border-t shrink-0" style={{ borderColor: C.border }}>
      <span className="flex-1 min-w-0 truncate text-xs" style={{ color: C.textMuted }} data-testid="head-footer-reason">
        {t(fail.key, fail.values)}
      </span>
      <button
        type="button"
        onClick={() => setRestartOpen(true)}
        data-testid="head-footer-restart"
        className={QUIET_BTN}
        style={{ color: C.textSecondary }}
      >
        <RotateCcw size={15} aria-hidden />
        {t("card.restartWith")}
      </button>
      <HeadRestartDialog open={restartOpen} onClose={() => setRestartOpen(false)} run={run} />
    </div>
  );
}
