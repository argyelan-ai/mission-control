"use client";

/**
 * Restart with … (docs/specs/head-launcher.md §8.2) — the crosswise switch.
 * Same picker as "New task"; centered dialog on desktop, bottom sheet on the
 * phone (ResponsiveModal).
 *
 *   Pair  [ ● Claude Code · GLM local                  ▾ ]
 *   ◉ Continue on this branch   keeps the commits so far
 *   ○ Start fresh from main     new branch from the base
 *   (needs you only)  Your answer [ … ]
 *                                        [Cancel] [Restart]
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import {
  canContinueRun,
  chooseRestartPair,
  defaultRestartMode,
  headErrorKey,
  pairKey,
  pairsForRestart,
  saveRememberedPair,
  type HeadPairsResponse,
  type HeadRun,
} from "@/lib/heads";
import { HeadPairPicker } from "./HeadPairPicker";

export function HeadRestartDialog({
  open,
  onClose,
  run,
  onRestarted,
}: {
  open: boolean;
  onClose: () => void;
  run: HeadRun;
  onRestarted?: (newRunId: string) => void;
}) {
  const t = useTranslations("heads");
  const qc = useQueryClient();
  const [pickedKey, setPickedKey] = useState<string | null>(null);
  // A run that ended before its branch existed cannot continue (mc-head
  // would try `git worktree add` on a missing branch) → fresh, continue off.
  const continueAllowed = canContinueRun(run);
  const [mode, setMode] = useState<"continue" | "fresh">(() => defaultRestartMode(run));
  // The dialog stays mounted under the card while the run moves on — pick
  // the mode from the run as it is when the dialog opens.
  useEffect(() => {
    if (open) setMode(defaultRestartMode(run));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, run.run_id, run.reason, run.started_at]);
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);

  const pairsQuery = useQuery<HeadPairsResponse>({
    queryKey: ["heads", "pairs"],
    queryFn: () => api.heads.pairs(),
    enabled: open,
    retry: false,
    staleTime: 10_000,
  });
  const pairs = pairsQuery.data ? pairsForRestart(pairsQuery.data.pairs, run.run_id) : [];
  const selected =
    (pickedKey ? pairs.find((p) => pairKey(p) === pickedKey) : undefined) ??
    (pairsQuery.data ? chooseRestartPair(pairs, pairsQuery.data.default_pair, run) : null);

  const restart = useMutation({
    mutationFn: () =>
      api.heads.restart(run.run_id, {
        harness: selected!.harness,
        runtime_slug: selected!.runtime_slug,
        mode,
        ...(answer.trim() ? { answer: answer.trim() } : {}),
      }),
    onMutate: () => setError(null),
    onSuccess: (res) => {
      if (selected) saveRememberedPair(pairKey(selected));
      qc.invalidateQueries({ queryKey: ["heads"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["pipeline"] });
      notify.success(t("restart.done"));
      setAnswer("");
      setPickedKey(null);
      onRestarted?.(res.run_id);
      onClose();
    },
    onError: (err) => setError(t(headErrorKey(err))),
  });

  const canSubmit = !!selected && selected.startable && !restart.isPending;

  const radio = (value: "continue" | "fresh", label: string, hint: string, disabled = false) => (
    <label
      className={`flex items-start gap-2.5 px-3 py-2 min-h-[44px] rounded-md ${disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"}`}
      style={{ background: mode === value ? C.accentSubtle : "transparent", border: `1px solid ${mode === value ? C.borderAccent : C.border}` }}
    >
      <input
        type="radio"
        name={`head-restart-mode-${run.run_id}`}
        value={value}
        checked={mode === value}
        disabled={disabled}
        onChange={() => setMode(value)}
        className="mt-0.5 accent-[var(--color-accent)]"
        data-testid={`head-restart-mode-${value}`}
      />
      <span className="flex flex-col">
        <span className="text-xs" style={{ color: C.textPrimary }}>{label}</span>
        <span className="text-[11px]" style={{ color: C.textMuted }}>{hint}</span>
      </span>
    </label>
  );

  return (
    <ResponsiveModal open={open} onClose={onClose} dismissOnOutside={false} aria-labelledby={`head-restart-title-${run.run_id}`}>
      <div className="flex flex-col min-h-0" data-testid="head-restart-dialog">
        <div className="px-5 py-3.5 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
          <h2 id={`head-restart-title-${run.run_id}`} className="text-sm font-semibold" style={{ color: C.textPrimary }}>
            {t("restart.title")}
          </h2>
        </div>
        <div className="p-5 space-y-4 overflow-y-auto">
          {pairsQuery.isError ? (
            <p role="alert" className="text-xs" style={{ color: C.error }}>{t(headErrorKey(pairsQuery.error))}</p>
          ) : (
            <HeadPairPicker
              pairs={pairs}
              selected={selected}
              defaultKey={pairsQuery.data?.default_pair ? pairKey(pairsQuery.data.default_pair) : null}
              onSelect={(p) => setPickedKey(pairKey(p))}
              disabled={restart.isPending || !pairsQuery.data}
            />
          )}
          <fieldset className="space-y-2">
            <legend className="sr-only">{t("restart.title")}</legend>
            {radio("continue", t("restart.continue"), continueAllowed ? t("restart.continueHint") : t("restart.noBranchYet"), !continueAllowed)}
            {radio("fresh", t("restart.fresh"), t("restart.freshHint"))}
          </fieldset>
          {run.state === "needs_you" && (
            <div className="space-y-1.5">
              <label htmlFor={`head-restart-answer-${run.run_id}`} className="label-sys">{t("card.answerLabel")}</label>
              <textarea
                id={`head-restart-answer-${run.run_id}`}
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                rows={3}
                maxLength={8000}
                placeholder={t("card.answerPlaceholder")}
                className="w-full px-3 py-2 rounded-md text-base sm:text-xs resize-y"
                style={{ background: C.bgDeep, color: C.textPrimary, border: `1px solid ${C.border}` }}
              />
            </div>
          )}
          {error && <p role="alert" className="text-xs" style={{ color: C.error }} data-testid="head-restart-error">{error}</p>}
        </div>
        <div
          className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2 px-5 py-3.5 shrink-0"
          style={{ borderTop: `1px solid ${C.borderSubtle}`, paddingBottom: "max(0.875rem, env(safe-area-inset-bottom))" }}
        >
          <button
            type="button"
            onClick={onClose}
            className="inline-flex items-center justify-center min-h-[44px] sm:min-h-0 px-3.5 py-1.5 text-[11px] rounded-md cursor-pointer hover:bg-[var(--color-bg-hover)]"
            style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
          >
            {t("restart.cancel")}
          </button>
          <button
            type="button"
            onClick={() => restart.mutate()}
            disabled={!canSubmit}
            data-testid="head-restart-submit"
            className="inline-flex items-center justify-center min-h-[44px] sm:min-h-0 px-3.5 py-1.5 text-[11px] font-semibold rounded-md cursor-pointer hover:bg-[var(--color-accent-light)] disabled:opacity-30 disabled:cursor-not-allowed"
            style={{ background: C.accent, color: C.onAccent }}
          >
            {restart.isPending ? t("restart.submitting") : t("restart.submit")}
          </button>
        </div>
      </div>
    </ResponsiveModal>
  );
}
