"use client";

// What each run of a stopped test did (25.10.): a stopped test has no video
// and no numbers, so the main column says run by run what happened — time,
// tokens thought vs. written, and the outcome in plain words ("stopped at the
// token limit before any page"). From the detail's runs, nothing new asked.
// Phone: one short block per run; from 1024 px a table on the column's width.
import { ExternalLink } from "lucide-react";
import { C } from "@/lib/colors";
import type { BenchTestDetail, ContestantResult, RunView } from "../types";
import { formatTime, formatTokens } from "./format";
import type { BenchWords } from "./words";

interface Row {
  key: string;
  label: string;
  time: string | null;
  thought: string | null;
  written: string | null;
  outcome: string | null;
  failed: boolean;
  /** A page kept when the time limit stopped the run (never ranked). */
  incomplete: boolean;
}

function rowsOf(test: BenchTestDetail, words: BenchWords): { contestant: ContestantResult; rows: Row[] }[] {
  const t = words.tr;
  const groups = test.blocks.flatMap((b) => b.contestants);
  return groups.map((c) => {
    const kind = c.summary.time_kind;
    const rows = [...c.runs]
      .sort((a, b) => a.repeat_idx - b.repeat_idx || a.attempt_no - b.attempt_no)
      .map((r: RunView): Row => {
        const out = r.tokens_out;
        const thought = r.tokens_reasoning;
        const written = out !== null && thought !== null ? Math.max(0, out - thought) : null;
        const outcome =
          r.state === "failed"
            ? words.reason(r.error)
            : r.state === "done"
              ? t("runs.page")
              : r.state === "waiting"
                ? t("runs.notStarted")
                : null;
        return {
          key: r.entry_id,
          label: t("runs.run", { i: r.repeat_idx }),
          time: r.elapsed_ms !== null && r.state !== "waiting" ? formatTime(r.elapsed_ms, kind) : null,
          // Without the engine's split, the whole output stands as "written".
          thought: thought !== null ? formatTokens(thought, words.locale) : null,
          written: written !== null ? formatTokens(written, words.locale) : out !== null ? formatTokens(out, words.locale) : null,
          outcome: r.state === "failed" && r.incomplete_page && outcome ? `${outcome} · ${t("runs.incomplete")}` : outcome,
          failed: r.state === "failed",
          incomplete: r.state === "failed" && r.incomplete_page,
        };
      });
    return { contestant: c, rows };
  });
}

/** "Open incomplete page" — quiet, beside the outcome it belongs to. */
function OpenIncomplete({ entryId, label, onOpen }: { entryId: string; label: string; onOpen: (id: string) => void }) {
  return (
    <button
      type="button"
      data-testid="result-run-open-incomplete"
      onClick={() => onOpen(entryId)}
      className="inline-flex items-center gap-2 min-h-11 -ml-2 px-2 rounded-md text-sm cursor-pointer self-start"
      style={{ color: C.textSecondary }}
    >
      <ExternalLink size={14} aria-hidden />
      {label}
    </button>
  );
}

export function RunsBreakdown({
  test,
  words,
  onOpenPage,
}: {
  test: BenchTestDetail;
  words: BenchWords;
  onOpenPage?: (entryId: string) => void;
}) {
  const t = words.tr;
  const groups = rowsOf(test, words).filter((g) => g.rows.length > 0);
  if (groups.length === 0) return null;
  const named = groups.length > 1;
  return (
    <div data-testid="result-runs" className="flex flex-col gap-2">
      <h2 className="label-sys">{t("runs.label")}</h2>
      {groups.map(({ contestant, rows }) => (
        <div key={contestant.key} className="flex flex-col">
          {named && (
            <p className="text-sm font-medium pt-2" style={{ color: C.textPrimary }}>
              {contestant.display_name}
            </p>
          )}
          {/* Phone: one block per run. */}
          <ul className="lg:hidden flex flex-col">
            {rows.map((r) => (
              <li
                key={r.key}
                data-testid="result-run"
                className="flex flex-col gap-1 py-3"
                style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
              >
                <div className="flex items-baseline justify-between gap-3">
                  <span className="text-sm font-medium" style={{ color: C.textPrimary }}>
                    {r.label}
                  </span>
                  {r.time && (
                    <span className="text-sm tabular-nums" style={{ color: C.textSecondary }}>
                      {r.time}
                    </span>
                  )}
                </div>
                {(r.thought || r.written) && (
                  <span className="text-sm tabular-nums" style={{ color: C.textSecondary }}>
                    {[
                      r.thought ? t("runs.thoughtLine", { v: r.thought }) : null,
                      r.written ? t("runs.writtenLine", { v: r.written }) : null,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                )}
                {r.outcome && (
                  <span className="text-sm first-letter:uppercase" style={{ color: r.failed ? C.textMuted : C.textSecondary }}>
                    {r.outcome}
                  </span>
                )}
                {r.incomplete && onOpenPage && (
                  <OpenIncomplete entryId={r.key} label={t("runs.openIncomplete")} onOpen={onOpenPage} />
                )}
              </li>
            ))}
          </ul>
          {/* From 1024 px: a table across the main column. */}
          <table className="hidden lg:table w-full text-sm">
            <thead>
              <tr style={{ borderBottom: `1px solid ${C.border}` }}>
                {[t("runs.runHead"), t("runs.time"), t("runs.thought"), t("runs.written"), t("runs.outcome")].map(
                  (h, i) => (
                    <th
                      key={i}
                      scope="col"
                      className={`py-2 pr-6 font-normal text-left ${i >= 1 && i <= 3 ? "text-right" : ""}`}
                      style={{ color: C.textMuted }}
                    >
                      {h}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.key} data-testid="result-run-row" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
                  <th scope="row" className="py-3 pr-6 font-medium text-left whitespace-nowrap" style={{ color: C.textPrimary }}>
                    {r.label}
                  </th>
                  <td className="py-3 pr-6 text-right tabular-nums whitespace-nowrap" style={{ color: C.textSecondary }}>
                    {r.time}
                  </td>
                  <td className="py-3 pr-6 text-right tabular-nums whitespace-nowrap" style={{ color: C.textSecondary }}>
                    {r.thought}
                  </td>
                  <td className="py-3 pr-6 text-right tabular-nums whitespace-nowrap" style={{ color: C.textSecondary }}>
                    {r.written}
                  </td>
                  <td className="py-3 first-letter:uppercase" style={{ color: r.failed ? C.textMuted : C.textSecondary }}>
                    {r.outcome}
                    {r.incomplete && onOpenPage && (
                      <div>
                        <OpenIncomplete entryId={r.key} label={t("runs.openIncomplete")} onOpen={onOpenPage} />
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}
