"use client";

/**
 * Runs list on the Summary tab (docs/specs/head-launcher.md §8.2) — makes
 * the crosswise switch visible:
 *
 *   RUNS
 *   1  omp · GLM local            failed  · time limit      2 h 00
 *   2  Claude Code · GLM local    passed  · PR #712         16 min
 *
 * Oldest first (run 1 = the first attempt). Renders nothing without runs.
 */

import { useLocale, useTranslations } from "next-intl";
import { C, STATUS_TEXT } from "@/lib/colors";
import { formatDuration } from "@/lib/taskDetail/format";
import {
  failReasonKey,
  headStateKey,
  prNumberFromUrl,
  runDurationSeconds,
  runPairLabel,
  sortRunsNewestFirst,
  type HeadPair,
  type HeadRun,
  type HeadState,
} from "@/lib/heads";

const STATE_COLOR: Record<HeadState, string> = {
  starting: STATUS_TEXT.info,
  running: STATUS_TEXT.info,
  needs_you: C.accent,
  passed: STATUS_TEXT.online,
  failed: STATUS_TEXT.error,
  stopped: C.textSecondary,
};

export function HeadRunsList({ runs, pairs }: { runs: HeadRun[]; pairs: HeadPair[] | null }) {
  const t = useTranslations("heads");
  const locale = useLocale();
  if (runs.length === 0) return null;
  const ordered = sortRunsNewestFirst(runs).reverse();

  const detail = (run: HeadRun): string | null => {
    if (run.state === "passed") {
      const n = prNumberFromUrl(run.pr_url);
      return n != null ? `PR #${n}` : null;
    }
    if (run.state === "failed" || run.state === "stopped") {
      const f = failReasonKey(run.reason);
      return t(f.key, f.values);
    }
    return null;
  };

  return (
    <div className="pb-3" data-testid="head-runs">
      <div className="label-sys label-sys--dim py-2">{t("runs.title")}</div>
      <ol className="space-y-px">
        {ordered.map((run, i) => {
          const extra = detail(run);
          const duration = formatDuration(runDurationSeconds(run), locale);
          return (
            <li
              key={run.run_id}
              data-testid="head-run-row"
              data-state={run.state}
              className="grid grid-cols-[20px_1fr_auto] gap-x-2 gap-y-0.5 py-1.5 text-xs items-baseline"
              style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
            >
              <span className="font-mono text-[11px]" style={{ color: C.textMuted }}>{i + 1}</span>
              <span className="min-w-0">
                <span className="block truncate" style={{ color: C.textPrimary }}>{runPairLabel(run, pairs)}</span>
                <span className="block truncate text-[11px]" style={{ color: C.textMuted }}>
                  <span style={{ color: STATE_COLOR[run.state] }}>{t(headStateKey(run.state))}</span>
                  {extra ? ` · ${extra}` : ""}
                </span>
              </span>
              <span className="font-mono text-[11px] shrink-0" style={{ color: C.textMuted }}>{duration ?? "—"}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
