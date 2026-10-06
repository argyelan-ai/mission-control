"use client";

/**
 * HeadContinueSheet — "Continue" (bauplan `heads-sichtbar` PR 4 §5): the
 * focused sheet behind a PASSED head's "Continue" button, replacing the
 * full `HeadRestartDialog` PR 2 used there as a stand-in (see that
 * component's own `fertig: "Weitermachen"` note — "öffnet vorerst
 * bestehenden HeadRestartDialog im continue-Modus; PR 4 ersetzt durch das
 * Blatt"). Always `mode: "continue"` — no fresh/continue toggle. A
 * finished head's own branch IS what "Continue" means here; "Restart
 * with …" on a failed/stopped run still offers the full choice, for that
 * different situation (still `HeadRestartDialog`, unchanged by this PR).
 *
 * No native "resume the conversation" (`--resume`/`--continue`, out of
 * scope — bauplan §3.2/§7, unproven after a worktree move). The new run
 * gets a fresh CLI session, seeded through `job.md` with the run record
 * plus a deterministic, model-free summary of the previous run's
 * transcript (`services/heads/transcript.summarize`) — never the old
 * conversation itself.
 */
import { useState } from "react";
import { useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import {
  canContinueRun,
  chooseRestartPair,
  headErrorKey,
  pairKey,
  pairsForRestart,
  saveRememberedPair,
  type HeadPairsResponse,
  type HeadRun,
} from "@/lib/heads";
import { HeadPairPicker } from "./HeadPairPicker";

export function HeadContinueSheet({
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
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  // A passed run normally always has a branch; this guards the rare edge
  // case (crash before the branch existed) the same way HeadRestartDialog
  // does for its own "continue" radio.
  const continueAllowed = canContinueRun(run);

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

  const continueMutation = useMutation({
    mutationFn: () =>
      api.heads.restart(run.run_id, {
        harness: selected!.harness,
        runtime_slug: selected!.runtime_slug,
        mode: "continue",
        ...(note.trim() ? { answer: note.trim() } : {}),
      }),
    onMutate: () => setError(null),
    onSuccess: (res) => {
      if (selected) saveRememberedPair(pairKey(selected));
      qc.invalidateQueries({ queryKey: ["heads"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["pipeline"] });
      notify.success(t("restart.done"));
      setNote("");
      setPickedKey(null);
      onRestarted?.(res.run_id);
      onClose();
    },
    onError: (err) => setError(t(headErrorKey(err))),
  });

  const canSubmit = continueAllowed && !!selected && selected.startable && !continueMutation.isPending;

  return (
    <ResponsiveModal open={open} onClose={onClose} dismissOnOutside={false} aria-labelledby={`head-continue-title-${run.run_id}`}>
      <div className="flex flex-col min-h-0" data-testid="head-continue-sheet">
        <div className="px-4 py-3 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
          <h2 id={`head-continue-title-${run.run_id}`} className="text-sm font-semibold" style={{ color: C.textPrimary }}>
            {t("continueSheet.title")}
          </h2>
        </div>
        <div className="p-4 space-y-4 overflow-y-auto">
          <p className="text-xs" style={{ color: C.textMuted }} data-testid="head-continue-explain">
            {t("continueSheet.explain")}
          </p>
          {!continueAllowed && (
            <p role="alert" className="text-xs" style={{ color: C.error }} data-testid="head-continue-no-branch">
              {t("restart.noBranchYet")}
            </p>
          )}
          {pairsQuery.isError ? (
            <p role="alert" className="text-xs" style={{ color: C.error }}>{t(headErrorKey(pairsQuery.error))}</p>
          ) : (
            <HeadPairPicker
              pairs={pairs}
              selected={selected}
              defaultKey={pairsQuery.data?.default_pair ? pairKey(pairsQuery.data.default_pair) : null}
              onSelect={(p) => setPickedKey(pairKey(p))}
              disabled={continueMutation.isPending || !pairsQuery.data}
            />
          )}
          <div className="space-y-1">
            <label htmlFor={`head-continue-note-${run.run_id}`} className="label-sys">
              {t("continueSheet.noteLabel")}
            </label>
            <textarea
              id={`head-continue-note-${run.run_id}`}
              data-testid="head-continue-note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={3}
              maxLength={8000}
              placeholder={t("continueSheet.notePlaceholder")}
              className="w-full px-3 py-2 rounded-md text-base sm:text-xs resize-y"
              style={{ background: C.bgDeep, color: C.textPrimary, border: `1px solid ${C.border}` }}
            />
          </div>
          {error && <p role="alert" className="text-xs" style={{ color: C.error }} data-testid="head-continue-error">{error}</p>}
        </div>
        <div
          className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2 px-4 py-3 shrink-0"
          style={{ borderTop: `1px solid ${C.borderSubtle}`, paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}
        >
          <button
            type="button"
            onClick={onClose}
            className="inline-flex items-center justify-center min-h-[44px] sm:min-h-0 px-4 py-2 text-xs rounded-md cursor-pointer hover:bg-[var(--color-bg-hover)]"
            style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
          >
            {t("restart.cancel")}
          </button>
          <button
            type="button"
            onClick={() => continueMutation.mutate()}
            disabled={!canSubmit}
            data-testid="head-continue-submit"
            className="inline-flex items-center justify-center min-h-[44px] sm:min-h-0 px-4 py-2 text-xs font-semibold rounded-md cursor-pointer hover:bg-[var(--color-accent-light)] disabled:opacity-30 disabled:cursor-not-allowed"
            style={{ background: C.accent, color: C.onAccent }}
          >
            {continueMutation.isPending ? t("continueSheet.submitting") : t("continueSheet.submit")}
          </button>
        </div>
      </div>
    </ResponsiveModal>
  );
}
