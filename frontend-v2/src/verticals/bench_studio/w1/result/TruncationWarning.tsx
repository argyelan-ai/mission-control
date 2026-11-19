"use client";

// PRIVATE truncation warning (operator decision 24.10.2026): a run that hit
// max_tokens is cut short, its numbers are too small. Viewers of the post
// do not care, so the public video, card and texts never mention it — but
// the operator must see it BEFORE he downloads the kit, with a one-tap
// re-run on a larger budget. Used by the result page (above the X-Kit
// button) and the X-Kit sheet (in place of the downloads).
//
// Until the operator says "Trotzdem weiter", the larger-budget re-run IS
// the page's one primary (his decision: the budget first, then the kit).
// One signal (K6): the warning icon on a quiet surface — no tinted fill, no
// coloured frame.
import { AlertTriangle, Loader2 } from "lucide-react";
import { C } from "@/lib/colors";
import type { ContestantResult, CostConfirmation, TruncationWarning as Truncation } from "../types";
import { formatInt, formatUsd } from "./format";
import type { BenchWords } from "./words";

/** The repeat numbers (1-based) of the cut runs, from the entry ids. */
export function cutRunNumbers(truncation: Truncation, contestants: ContestantResult[]): number[] {
  const ids = new Set(truncation.entry_ids);
  const out: number[] = [];
  for (const c of contestants) for (const r of c.runs) if (ids.has(r.entry_id)) out.push(r.repeat_idx);
  return [...new Set(out)].sort((a, b) => a - b);
}

function trimUsd(n: number): string {
  return Number.isInteger(n) ? String(n) : formatUsd(n);
}

export function TruncationWarning({
  truncation,
  contestants,
  words,
  canRerun,
  rerunPending,
  costAsk = null,
  primary = false,
  onRerun,
  onContinue,
}: {
  truncation: Truncation;
  contestants: ContestantResult[];
  words: BenchWords;
  canRerun: boolean;
  rerunPending: boolean;
  /** Set after a 409 CostConfirmation: the button asks for the price (D4). */
  costAsk?: CostConfirmation | null;
  /** The re-run is the screen's one accent button (K11). */
  primary?: boolean;
  onRerun: () => void;
  onContinue: () => void;
}) {
  const runs = cutRunNumbers(truncation, contestants);
  const runList =
    runs.length > 0
      ? new Intl.ListFormat(words.locale, { type: "conjunction" }).format(runs.map(String))
      : null;
  const max = formatInt(truncation.max_tokens, words.locale);
  const suggested = formatInt(truncation.suggested_max_tokens, words.locale);
  // The price question keeps what the button does (N2): "Mit 32'768 Tokens
  // nochmal · $0.42 — OK?".
  const rerunLabel = !costAsk
    ? words.tr("truncation.rerun", { suggested })
    : costAsk.price_unknown || costAsk.usd === null
      ? words.tr("truncation.rerunConfirmUnknown", { suggested })
      : costAsk.step === 2
        ? words.ts("cost.confirmAgain", { cap: trimUsd(costAsk.cap_usd), usd: formatUsd(costAsk.usd) })
        : words.tr("truncation.rerunConfirm", { suggested, usd: formatUsd(costAsk.usd) });
  const noPage = truncation.runs_without_page > 0 && truncation.runs_without_page >= truncation.runs_cut;
  const count = runs.length || truncation.runs_cut;
  // A local run had the whole context window (25.10.2026): no larger budget
  // exists, so the warning says so and offers no re-run.
  const larger = truncation.larger_budget !== false;
  const body = !larger
    ? runList
      ? words.tr("truncation.bodyContext", { runs: runList, count, max })
      : words.tr("truncation.bodyContextNoRuns", { max })
    : runList
      ? words.tr(noPage ? "truncation.bodyNoPage" : "truncation.body", { runs: runList, count, max, suggested })
      : words.tr(noPage ? "truncation.bodyNoPageNoRuns" : "truncation.bodyNoRuns", { max, suggested });
  return (
    <div
      data-testid="truncation-warning"
      data-region="result-truncation"
      role="status"
      className="rounded-lg p-4"
      style={{ backgroundColor: C.bgSurface, border: `1px solid ${C.border}` }}
    >
      <div className="flex items-start gap-2">
        <AlertTriangle size={16} aria-hidden className="shrink-0 mt-1" style={{ color: C.warning }} />
        <div className="flex flex-col gap-1">
          <p className="text-sm font-semibold" style={{ color: C.textPrimary }}>
            {words.ts("reason.runs_truncated", { runs_cut: truncation.runs_cut, n_runs: truncation.n_runs })}
          </p>
          <p className="text-sm" style={{ color: C.textSecondary }}>
            {body}
          </p>
          {truncation.video_run_cut && (
            <p data-testid="truncation-video" className="text-sm" style={{ color: C.textPrimary }}>
              {words.tr("truncation.videoCut")}
            </p>
          )}
          <p className="text-xs" style={{ color: C.textMuted }}>
            {words.tr("truncation.private")}
          </p>
          <div className="flex flex-wrap items-center gap-2 mt-2">
            {canRerun && larger && (
              <button
                type="button"
                onClick={onRerun}
                disabled={rerunPending}
                data-primary={primary ? "true" : undefined}
                className="flex items-center gap-2 min-h-11 px-4 rounded-md text-sm font-medium cursor-pointer disabled:cursor-wait"
                style={
                  primary
                    ? { backgroundColor: C.accent, color: C.onAccent }
                    : { color: C.textPrimary, border: `1px solid ${C.borderActive}` }
                }
              >
                {rerunPending && <Loader2 size={14} aria-hidden className="animate-spin" />}
                {rerunLabel}
              </button>
            )}
            <button
              type="button"
              onClick={onContinue}
              className="min-h-11 px-2 rounded-md text-sm cursor-pointer"
              style={{ color: C.textSecondary }}
            >
              {words.tr("truncation.continue")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
